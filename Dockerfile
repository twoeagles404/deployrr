# =============================================================================
# ArrHub — WebUI Dockerfile
# =============================================================================
# Builds the FastAPI monitoring and management dashboard.
# Serves on port 9999 via uvicorn (single worker, async).
#
# Build:  docker build -t arrhub-webui:local .
# Run:    docker run -d -p 9999:9999 \
#           -v /var/run/docker.sock:/var/run/docker.sock \
#           -v /opt/arrhub/data:/data \
#           -v /opt/arrhub/arrhub-webui/app.py:/app/app.py:ro \
#           --pid=host \
#           arrhub-webui:local
# =============================================================================

# ── Stage 1: grab the docker CLI binary (no daemon, just the client) ─────────
FROM docker:27-cli AS docker-cli

# ── Stage 2: the actual application image ─────────────────────────────────────
FROM python:3.12-slim

# ── Labels ────────────────────────────────────────────────────────────────────
LABEL maintainer="twoeagles404"
LABEL version="3.17.16"
LABEL description="ArrHub — Server monitoring and Docker management dashboard"
LABEL org.opencontainers.image.source="https://github.com/twoeagles404/arrhub"

# ── Copy docker CLI from stage 1 ─────────────────────────────────────────────
# Gives us `docker` and `docker compose` inside the container so the app
# can run compose commands against the mounted Docker socket.
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app

# ── System packages ───────────────────────────────────────────────────────────
# These provide host hardware info (dmidecode, lsblk, lspci, lsusb)
# gcc + python3-dev are needed for psutil compilation on arm64 under QEMU
RUN apt-get update && apt-get install -y --no-install-recommends \
    util-linux \
    dmidecode \
    pciutils \
    usbutils \
    iproute2 \
    procps \
    curl \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ───────────────────────────────────────────────────────
# --prefer-binary tells pip to prefer pre-built wheels over source compilation.
# This avoids QEMU cross-compilation failures for arm64 when native wheels exist.
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --prefer-binary \
    fastapi==0.115.12 \
    uvicorn[standard]==0.34.0 \
    docker==7.1.0 \
    psutil==6.1.1 \
    requests==2.32.3 \
    pyyaml==6.0.2

# ── Copy application ──────────────────────────────────────────────────────────
# NOTE: app.py is also mountable at runtime via -v for live updates
# without rebuilding the image.
COPY app.py .

# ── Expose port ───────────────────────────────────────────────────────────────
EXPOSE 9999

# ── Health check ──────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:9999/ || exit 1

# ── Volume documentation ─────────────────────────────────────────────────────
# Volume: -v /opt/arrhub/data:/data  (SQLite DB + settings)
# Volume: -v /opt/arrhub/arrhub-webui/app.py:/app/app.py:ro  (live app updates)
# Volume: -v /var/run/docker.sock:/var/run/docker.sock  (Docker access)
# Env: ARRHUB_TOKEN=your-secret-token  (optional auth token)
# Env: ARRHUB_NO_AUTH=true  (disable auth for LAN-only use)

# ── Launch command ────────────────────────────────────────────────────────────
# uvicorn with single async worker — FastAPI is async-native, no gunicorn needed.
CMD ["uvicorn", "app:app", \
     "--host",    "0.0.0.0", \
     "--port",    "9999",    \
     "--workers", "1"]
