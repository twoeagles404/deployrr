# =============================================================================
# Deployrr — WebUI Dockerfile
# =============================================================================
# Builds the lightweight Flask monitoring and management dashboard.
# Serves on port 9999 via gunicorn (4 workers, threaded).
#
# Build:  docker build -t deployrr-webui:local .
# Run:    docker run -d -p 9999:9999 \
#           -v /var/run/docker.sock:/var/run/docker.sock \
#           --pid=host \
#           deployrr-webui:local
# =============================================================================

FROM python:3.12-slim

# ── Labels ────────────────────────────────────────────────────────────────────
LABEL maintainer="twoeagles404"
LABEL version="3.1.0"
LABEL description="Deployrr — Server monitoring and Docker management dashboard"
LABEL org.opencontainers.image.source="https://github.com/twoeagles404/deployrr"

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
# Versions bumped to latest stable as of 2026-03 for compatibility.
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --prefer-binary \
    flask==3.1.0 \
    docker==7.1.0 \
    psutil==6.1.1 \
    gunicorn==23.0.0 \
    flask-sock==0.7.0 \
    requests==2.32.3

# ── Copy application ──────────────────────────────────────────────────────────
COPY app.py .
# NOTE: apps/catalog.json is mounted at runtime via -v, not baked into the image.
# This keeps the image generic and allows catalog updates without rebuilding.

# ── Expose port ───────────────────────────────────────────────────────────────
EXPOSE 9999

# ── Health check ──────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:9999/ || exit 1

# ── Volume documentation ─────────────────────────────────────────────────────
# Volume: -v /opt/deployrr/data:/data  (SQLite DB + settings)
# Volume: -v /var/run/docker.sock:/var/run/docker.sock  (Docker access)
# Env: DEPLOYRR_TOKEN=your-secret-token  (optional auth token)
# Env: DEPLOYRR_NO_AUTH=true  (disable auth for LAN-only use)

# ── Launch command ────────────────────────────────────────────────────────────
# gunicorn with 4 workers, 2 threads each = 8 concurrent requests
# gthread worker class supports SSE streaming (needed for update endpoint)
CMD ["gunicorn", "app:app", \
     "--bind",         "0.0.0.0:9999", \
     "--workers",      "4",            \
     "--threads",      "2",            \
     "--worker-class", "gthread",      \
     "--timeout",      "120",          \
     "--keep-alive",   "5",            \
     "--log-level",    "warning",      \
     "--access-logfile", "-"]
