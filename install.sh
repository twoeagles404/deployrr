#!/bin/bash
# =============================================================================
# ArrHub — Master Installer v3.15.16
# =============================================================================
#
# ONE-COMMAND INSTALL (dev branch — always uses latest dev code):
#   curl -fsSL https://raw.githubusercontent.com/twoeagles404/arrhub/dev/install.sh | sudo bash
#
# STABLE (main branch):
#   curl -fsSL https://raw.githubusercontent.com/twoeagles404/arrhub/main/install.sh | sudo bash
#
# LOCAL INSTALL (from cloned repo):
#   sudo bash install.sh
#
# After install:
#   Type  media            → open the TUI menu
#   Open  http://<ip>:9999 → web dashboard
#   Run   media update     → self-update ArrHub
#
# =============================================================================

set -euo pipefail

# ── GitHub source — update to match your fork ────────────────────────────────
GITHUB_USER="twoeagles404"
GITHUB_REPO="arrhub"
# GITHUB_BRANCH is set for the branch this file lives on (dev/main).
# CI validates this plain line — keep it matching the actual branch name.
GITHUB_BRANCH="main"
# Allow env-var override for testing:  ARRHUB_BRANCH=main sudo bash install.sh
GITHUB_BRANCH="${ARRHUB_BRANCH:-${GITHUB_BRANCH}}"
GITHUB_RAW="https://raw.githubusercontent.com/${GITHUB_USER}/${GITHUB_REPO}/${GITHUB_BRANCH}"

# ── Version ───────────────────────────────────────────────────────────────────
VERSION="3.15.16"
INSTALL_DATE="$(date '+%Y-%m-%d %H:%M:%S')"

# ── Install paths ─────────────────────────────────────────────────────────────
DEST="/opt/arrhub"
LOG="/tmp/arrhub-install.log"
MEDIA_CMD="/usr/local/bin/media"

# ── Colours ───────────────────────────────────────────────────────────────────
R='\033[1;31m'; G='\033[1;32m'; C='\033[1;36m'; Y='\033[1;33m'
P='\033[1;35m'; B='\033[1m'; N='\033[0m'

ok()   { printf "${G}  ✓  ${N}%s\n"   "$*"; }
info() { printf "${C}  →  ${N}%s\n"   "$*"; }
warn() { printf "${Y}  ⚠  ${N}%s\n"   "$*"; }
die()  { printf "${R}  ✗  %s${N}\n"   "$*" >&2; exit 1; }
step() { printf "\n${B}${C}── %s ${N}\n" "$*"; }

hdr() {
    clear
    echo
    printf "${C}${B}%s${N}\n" "$(printf '═%.0s' {1..64})"
    printf "\n"
    printf "${P}${B}             █████╗ ██████╗ ██████╗ ██╗  ██╗██╗   ██╗██████╗ ${N}\n"
    printf "${P}${B}            ██╔══██╗██╔══██╗██╔══██╗██║  ██║██║   ██║██╔══██╗${N}\n"
    printf "${C}${B}            ███████║██████╔╝██████╔╝███████║██║   ██║██████╔╝${N}\n"
    printf "${C}${B}            ██╔══██║██╔══██╗██╔══██╗██╔══██║██║   ██║██╔══██╗${N}\n"
    printf "${C}${B}            ██║  ██║██║  ██║██║  ██║██║  ██║╚██████╔╝██████╔╝${N}\n"
    printf "${C}${B}            ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ${N}\n"
    printf "\n"
    printf "${G}${B}                             v${VERSION}${N}\n"
    printf "${C}                        101 apps · Pure Bash · MIT License${N}\n"
    printf "\n"
    printf "${C}${B}%s${N}\n" "$(printf '═%.0s' {1..64})"
    echo
}

# ── Must be root ──────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run as root:  sudo bash $0"

: > "${LOG}"
hdr

info "Install destination : ${DEST}"
info "GitHub source       : ${GITHUB_RAW}"
info "Install log         : ${LOG}"
echo

# =============================================================================
# STEP 1 — System detection
# =============================================================================
step "Detecting system"

# Load OS info
OS_ID="linux"
PRETTY_NAME="Linux"
if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    OS_ID="${ID:-linux}"
    PRETTY_NAME="${PRETTY_NAME:-Linux}"
fi
ok "OS: ${PRETTY_NAME} (${OS_ID})"
ok "Arch: $(uname -m)  |  Kernel: $(uname -r)"

# Detect package manager
if   command -v apt-get &>/dev/null; then PKG_MGR="apt-get"; PKG_INSTALL="apt-get install -y -q"
elif command -v dnf     &>/dev/null; then PKG_MGR="dnf";     PKG_INSTALL="dnf install -y"
elif command -v yum     &>/dev/null; then PKG_MGR="yum";     PKG_INSTALL="yum install -y"
elif command -v pacman  &>/dev/null; then PKG_MGR="pacman";  PKG_INSTALL="pacman -S --noconfirm"
elif command -v apk     &>/dev/null; then PKG_MGR="apk";     PKG_INSTALL="apk add --quiet"
else
    warn "Unknown package manager — manual installs may be required"
    PKG_MGR="unknown"; PKG_INSTALL="echo MANUAL_INSTALL:"
fi
ok "Package manager: ${PKG_MGR}"

# =============================================================================
# STEP 2 — Install system dependencies
# =============================================================================
step "Installing system dependencies"

# Update package lists (apt only, silent)
if [[ "${PKG_MGR}" == "apt-get" ]]; then
    info "Updating apt package lists..."
    apt-get update -qq >> "${LOG}" 2>&1 && ok "Package lists updated" || warn "apt-get update had warnings"
fi

install_pkg() {
    local pkg="$1"
    local check_cmd="${2:-$1}"  # command to check (may differ from pkg name)
    if command -v "${check_cmd}" &>/dev/null; then
        ok "${pkg} already present"
        return 0
    fi
    info "Installing ${pkg}..."
    if ${PKG_INSTALL} "${pkg}" >> "${LOG}" 2>&1; then
        ok "${pkg} installed"
    else
        warn "Could not auto-install ${pkg} — please install it manually"
    fi
}

install_pkg curl     curl
install_pkg dialog   dialog
install_pkg git      git

# =============================================================================
# STEP 3 — Docker detection and optional install
# =============================================================================
step "Checking Docker"

DOCKER_OK=false
COMPOSE_OK=false

if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    DOCKER_VER="$(docker --version 2>/dev/null | head -1)"
    ok "Docker running: ${DOCKER_VER}"
    DOCKER_OK=true
else
    warn "Docker not found or not running"
    echo
    printf "  ${C}Docker is required for ArrHub to function.${N}\n"
    printf "  ${B}Auto-install Docker now?${N} [Y/n]: "
    read -r answer </dev/tty 2>/dev/null || answer="y"
    echo

    if [[ "${answer,,}" != "n" ]]; then
        info "Installing Docker via get.docker.com (this may take a minute)..."
        if curl -fsSL https://get.docker.com | sh >> "${LOG}" 2>&1; then
            ok "Docker installed"
            # Enable and start
            if command -v systemctl &>/dev/null; then
                systemctl enable docker >> "${LOG}" 2>&1 || true
                systemctl start  docker >> "${LOG}" 2>&1 || true
            fi
            sleep 3
            if docker info &>/dev/null 2>&1; then
                ok "Docker daemon is running"
                DOCKER_OK=true
            else
                warn "Docker installed but daemon not running. Try: systemctl start docker"
            fi
        else
            warn "Docker auto-install failed"
            warn "Manual install: https://docs.docker.com/engine/install/"
            warn "Then re-run: sudo bash install.sh"
        fi
    else
        warn "Skipping Docker install — WebUI and deploy features will not work"
        warn "Install later: curl -fsSL https://get.docker.com | sh"
    fi
fi

# Check Docker Compose v2 plugin
if docker compose version &>/dev/null 2>&1; then
    COMPOSE_VER="$(docker compose version 2>/dev/null | head -1)"
    ok "Docker Compose: ${COMPOSE_VER}"
    COMPOSE_OK=true
else
    warn "Docker Compose v2 plugin not found"
    if [[ "${PKG_MGR}" == "apt-get" ]]; then
        info "Attempting to install docker-compose-plugin..."
        if apt-get install -y docker-compose-plugin >> "${LOG}" 2>&1; then
            ok "docker-compose-plugin installed"
            COMPOSE_OK=true
        else
            warn "Could not install docker-compose-plugin"
            warn "Manual: apt-get install docker-compose-plugin"
        fi
    else
        warn "Install Compose v2: https://docs.docker.com/compose/install/"
    fi
fi

# =============================================================================
# STEP 4 — Create directory structure
# =============================================================================
step "Setting up directories"

mkdir -p "${DEST}/arrhub-webui"
mkdir -p "${DEST}/apps"
ok "Created: ${DEST}"

# Create standard media/config dirs (non-fatal — skip if already exist)
for dir in /docker /mnt/media /mnt/media/movies /mnt/media/tv \
           /mnt/media/downloads /mnt/media/music /mnt/media/books \
           /mnt/media/podcasts /mnt/media/audiobooks /mnt/media/comics; do
    if [[ ! -d "${dir}" ]]; then
        mkdir -p "${dir}" 2>/dev/null && info "Created: ${dir}" || true
    fi
done
ok "Media directory structure ready"

# =============================================================================
# STEP 5 — Download ArrHub files
# =============================================================================
step "Downloading ArrHub files"

# Detect local source directory (supports local install from cloned repo)
SELF_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
    SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || echo "")"
fi

# Helper: prefer local copy, then GitHub download
download_file() {
    local label="$1"
    local remote_path="$2"     # path in the GitHub repo
    local dest_file="$3"
    local local_copy="${4:-}"  # optional local path

    if [[ -n "${local_copy}" && -f "${local_copy}" ]]; then
        cp "${local_copy}" "${dest_file}"
        ok "${label} (local copy)"
        return 0
    fi
    info "Downloading ${label}..."
    if curl -fsSL "${GITHUB_RAW}/${remote_path}" -o "${dest_file}" 2>>"${LOG}"; then
        ok "${label} downloaded"
    else
        die "Failed to download ${label} from: ${GITHUB_RAW}/${remote_path}"
    fi
}

# ── arrhub.sh — main TUI script
download_file "arrhub.sh" \
    "arrhub.sh" \
    "${DEST}/arrhub.sh" \
    "$([[ -f "${SELF_DIR}/arrhub.sh" ]] && echo "${SELF_DIR}/arrhub.sh" || echo "")"
chmod +x "${DEST}/arrhub.sh"

# ── app.py — Flask WebUI backend
download_file "app.py" \
    "app.py" \
    "${DEST}/arrhub-webui/app.py" \
    "$([[ -f "${SELF_DIR}/app.py" ]] && echo "${SELF_DIR}/app.py" || echo "")"

# ── Dockerfile — for the WebUI container
download_file "Dockerfile" \
    "Dockerfile" \
    "${DEST}/arrhub-webui/Dockerfile" \
    "$([[ -f "${SELF_DIR}/Dockerfile" ]] && echo "${SELF_DIR}/Dockerfile" || echo "")"

# ── catalog.json — app catalog (used by WebUI)
download_file "apps/catalog.json" \
    "apps/catalog.json" \
    "${DEST}/apps/catalog.json" \
    "$([[ -f "${SELF_DIR}/apps/catalog.json" ]] && echo "${SELF_DIR}/apps/catalog.json" || echo "")"

# =============================================================================
# STEP 6 — Install `media` CLI command
# =============================================================================
step "Installing 'media' CLI command"

cat > "${MEDIA_CMD}" << 'MEDIA_EOF'
#!/bin/bash
# ArrHub — 'media' CLI shortcut
# Installed automatically by install.sh
# To update ArrHub: media update
exec bash /opt/arrhub/arrhub.sh "$@"
MEDIA_EOF
chmod +x "${MEDIA_CMD}"
ok "'media' command installed → ${MEDIA_CMD}"
ok "Usage: media [update|help]"

# =============================================================================
# STEP 7 — Get & start the WebUI Docker container
# =============================================================================
# Strategy: always build locally from the downloaded source files.
# This guarantees the running container matches the installed branch exactly.
# A stale pre-built image on ghcr.io would otherwise silently override new code.
# The catalog.json is ALWAYS mounted at runtime — never baked into the image.

WEBUI_IMAGE="ghcr.io/${GITHUB_USER}/${GITHUB_REPO}:latest"
WEBUI_LOCAL_TAG="arrhub-webui:local"

if [[ "${DOCKER_OK}" == "true" ]]; then

    IMAGE_READY=false

    # ── Build locally from downloaded source (always current) ──
    step "Building ArrHub WebUI image"
    info "Building from ${DEST}/arrhub-webui (may take 60-90 seconds)..."
    if docker build -q -t "${WEBUI_LOCAL_TAG}" "${DEST}/arrhub-webui" >> "${LOG}" 2>&1; then
        ok "Image built: ${WEBUI_LOCAL_TAG}"
        IMAGE_READY=true
    else
        # ── Fallback: try ghcr.io if local build failed ──
        warn "Local build failed — trying ghcr.io fallback"
        info "Trying: docker pull ${WEBUI_IMAGE}"
        if docker pull "${WEBUI_IMAGE}" >> "${LOG}" 2>&1; then
            docker tag "${WEBUI_IMAGE}" "${WEBUI_LOCAL_TAG}" >> "${LOG}" 2>&1 || true
            ok "Fallback image pulled from ghcr.io (may not include latest fixes)"
            IMAGE_READY=true
        else
            warn "Image build and pull both failed — check ${LOG}"
            warn "Retry: arrhub → WebUI Control → Rebuild image"
        fi
    fi

    # ── Start the container ──
    if [[ "${IMAGE_READY}" == "true" ]]; then
        # Remove any stale container (prevents port conflict on reinstall)
        docker rm -f arrhub_webui >> "${LOG}" 2>&1 || true

        step "Starting ArrHub WebUI"
        info "Starting container on port 9999..."
        if docker run -d \
            --name arrhub_webui \
            --restart unless-stopped \
            -p 9999:9999 \
            -v /var/run/docker.sock:/var/run/docker.sock \
            -v "${DEST}/apps:/opt/arrhub/apps:ro" \
            -v "${DEST}/data:/data" \
            -v "${DEST}/arrhub-webui/app.py:/app/app.py:ro" \
            --pid=host \
            "${WEBUI_LOCAL_TAG}" >> "${LOG}" 2>&1
        then
            SERVER_IP="$(hostname -I | awk '{print $1}' 2>/dev/null || echo 'your-server-ip')"
            ok "WebUI started → http://${SERVER_IP}:9999"
        else
            warn "WebUI failed to start — check ${LOG}"
            warn "Retry from TUI: media → WebUI Control → Start WebUI"
        fi
    fi
else
    warn "Skipping WebUI (Docker not available)"
    warn "After installing Docker: media → WebUI Control → Start WebUI"
fi

# =============================================================================
# STEP 8 — Write version/install metadata
# =============================================================================
cat > "${DEST}/.version" << VEREOF
VERSION=${VERSION}
INSTALL_DATE=${INSTALL_DATE}
GITHUB_USER=${GITHUB_USER}
GITHUB_REPO=${GITHUB_REPO}
GITHUB_BRANCH=${GITHUB_BRANCH}
VEREOF
ok "Version file written: ${DEST}/.version"

# =============================================================================
# DONE — Print summary
# =============================================================================
SERVER_IP="$(hostname -I | awk '{print $1}' 2>/dev/null || echo 'your-server-ip')"

echo
printf "${C}${B}%s${N}\n" "$(printf '═%.0s' {1..64})"
printf "${G}${B}\n  ✓  ArrHub v${VERSION} installed successfully!\n${N}"
printf "${C}${B}%s${N}\n\n" "$(printf '═%.0s' {1..64})"

printf "  ${B}Install path   ${N}:  ${DEST}\n"
printf "  ${B}TUI command    ${N}:  ${G}${B}media${N}\n"
printf "  ${B}Self-update    ${N}:  ${G}${B}media update${N}\n"

if [[ "${DOCKER_OK}" == "true" ]]; then
    printf "  ${B}Web dashboard  ${N}:  ${C}${B}http://${SERVER_IP}:9999${N}\n"
fi

printf "  ${B}Install log    ${N}:  ${LOG}\n"
printf "\n"
printf "  ${Y}${B}▶  Type ${G}media${Y} and press Enter to open the TUI menu${N}\n"
echo

# Warn if Docker is still missing
if [[ "${DOCKER_OK}" != "true" ]]; then
    printf "  ${R}${B}⚠  Docker is not running!${N}\n"
    printf "  Install it: curl -fsSL https://get.docker.com | sh\n"
    printf "  Then start: systemctl start docker\n"
    printf "  Then open : sudo media\n\n"
fi
