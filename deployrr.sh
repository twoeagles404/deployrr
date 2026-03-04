#!/bin/bash
# =============================================================================
# Deployrr v3.3.0 — Production-Ready ARR Suite Deployment TUI
# Self-contained. Requires: dialog, docker (compose v2), bash 4+, root.
# GitHub: https://github.com/twoeagles404/deployrr
# =============================================================================

set -uo pipefail
# NOTE: -e (errexit) intentionally removed — deploy pipeline handles errors explicitly
# to prevent silent exit on failed docker pulls or compose commands.

# ---------------------------------------------------------------------------
# Version & GitHub Configuration
# ---------------------------------------------------------------------------
VERSION="3.3.0"
GITHUB_USER="twoeagles404"
GITHUB_REPO="deployrr"
GITHUB_BRANCH="main"
GITHUB_RAW="https://raw.githubusercontent.com/${GITHUB_USER}/${GITHUB_REPO}/${GITHUB_BRANCH}"

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
BACKTITLE="Deployrr v${VERSION} — ARR Suite Deployment Tool"
CONFIG_DIR="${CONFIG_DIR:-/docker}"
MEDIA_DIR="${MEDIA_DIR:-/mnt/media}"
TZ_VAL="${TZ:-America/New_York}"
PUID_VAL="0"
PGID_VAL="0"
COMPOSE_FILE="${CONFIG_DIR}/docker-compose.yml"

# Per-app compose — each app gets its own /docker/<appname>/docker-compose.yml
# Use: app_compose <id>   to get the path
# (COMPOSE_FILE kept as backwards-compat alias for dashboard ops on existing stacks)
app_compose() { echo "${CONFIG_DIR}/${1}/docker-compose.yml"; }

LOG_FILE="/var/log/deployrr.log"
ERR_FILE="/var/log/deployrr-errors.log"

TMP_DIR="$(mktemp -d /tmp/deployrr.XXXXXX)"
trap 'rm -rf "${TMP_DIR}"' EXIT

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_ts() { date '+%Y-%m-%d %H:%M:%S'; }

log() {
    local level="$1"; shift
    printf '[%s] [%-5s] %s\n' "$(_ts)" "${level}" "$*" >> "${LOG_FILE}" 2>/dev/null || true
}

log_err() {
    local ctx="$1"; shift
    printf '[%s] [ERROR] [%s] %s\n' "$(_ts)" "${ctx}" "$*" \
        | tee -a "${LOG_FILE}" >> "${ERR_FILE}" 2>/dev/null || true
}

log_raw() {
    local label="$1" file="$2"
    {
        printf '[%s] [RAW  ] --- begin: %s ---\n' "$(_ts)" "${label}"
        cat "${file}" 2>/dev/null || true
        printf '[%s] [RAW  ] --- end:   %s ---\n' "$(_ts)" "${label}"
    } >> "${LOG_FILE}" 2>/dev/null || true
}

log_raw_err() {
    local label="$1" file="$2"
    {
        printf '[%s] [ERROR] [RAW] --- begin: %s ---\n' "$(_ts)" "${label}"
        cat "${file}" 2>/dev/null || true
        printf '[%s] [ERROR] [RAW] --- end:   %s ---\n\n' "$(_ts)" "${label}"
    } >> "${ERR_FILE}" 2>/dev/null || true
}

log_section() {
    printf '\n%s\n[%s]  DEPLOY RUN\n%s\n' \
        "$(printf '=%.0s' {1..70})" "$(_ts)" \
        "$(printf '=%.0s' {1..70})" >> "${ERR_FILE}" 2>/dev/null || true
}

init_logs() {
    for f in "${LOG_FILE}" "${ERR_FILE}"; do
        touch "${f}" 2>/dev/null || { echo "WARNING: cannot write to ${f}" >&2; }
    done
    log INFO "Session started (PID $$)"
    log INFO "LOG=${LOG_FILE}  ERR=${ERR_FILE}"
}

# ---------------------------------------------------------------------------
# dialog helpers
# ---------------------------------------------------------------------------
d_menu() {
    local title="$1" prompt="$2"; shift 2
    dialog --clear --backtitle "${BACKTITLE}" \
           --title "${title}" --menu "${prompt}" 22 72 14 "$@" \
           3>&1 1>&2 2>&3
}

d_checklist() {
    local title="$1" prompt="$2"; shift 2
    dialog --clear --backtitle "${BACKTITLE}" \
           --title "${title}" --checklist "${prompt}" 28 76 20 "$@" \
           3>&1 1>&2 2>&3
}

d_msgbox() {
    dialog --clear --backtitle "${BACKTITLE}" \
           --title "$1" --msgbox "$2" 14 64
}

d_yesno() {
    dialog --clear --backtitle "${BACKTITLE}" \
           --title "$1" --yesno "$2" 10 58
}

d_inputbox() {
    local title="$1" prompt="$2" default="${3:-}"
    dialog --clear --backtitle "${BACKTITLE}" \
           --title "${title}" --inputbox "${prompt}" 10 62 "${default}" \
           3>&1 1>&2 2>&3
}

d_infobox() {
    dialog --clear --backtitle "${BACKTITLE}" \
           --title "$1" --infobox "$2" 10 62
}

d_textbox() {
    dialog --clear --backtitle "${BACKTITLE}" \
           --title "$1" --textbox "$2" 24 82
}

# ---------------------------------------------------------------------------
# Live terminal helpers
# ---------------------------------------------------------------------------
_live_header() {
    local title="$1" subtitle="${2:-}"
    clear
    printf '\033[1;36m'
    printf '=%.0s' {1..70}
    printf '\033[0m\n'
    printf ' \033[1;37m%s\033[0m\n' "${title}"
    [[ -n "${subtitle}" ]] && printf ' \033[0;37m%s\033[0m\n' "${subtitle}"
    printf '\033[1;36m'
    printf '=%.0s' {1..70}
    printf '\033[0m\n\n'
}

_live_wait_return() {
    printf '\n\033[0;37mPress Enter to return to menu...\033[0m\n'
    read -r
}

# ---------------------------------------------------------------------------
# App catalog (110+ apps)
# ---------------------------------------------------------------------------
declare -A APP_NAME APP_IMAGE APP_PORTS APP_PRIV APP_CAT APP_CUSTOM_SVC

define_app() {
    local id="$1" name="$2" image="$3" cat="$4" ports="${5:-}" priv="${6:-false}"
    APP_NAME[$id]="$name"
    APP_IMAGE[$id]="$image"
    APP_CAT[$id]="$cat"
    APP_PORTS[$id]="$ports"
    APP_PRIV[$id]="$priv"
    APP_CUSTOM_SVC[$id]=""
}

load_catalog() {
    # -- Downloaders --
    define_app qbittorrent  "qBittorrent"  "lscr.io/linuxserver/qbittorrent:latest"  "Downloaders"  "8080:8080 6881:6881 6881:6881/udp"
    APP_CUSTOM_SVC[qbittorrent]="yes"
    define_app transmission "Transmission" "lscr.io/linuxserver/transmission:latest" "Downloaders"  "9091:9091 51413:51413 51413:51413/udp"
    define_app deluge       "Deluge"       "lscr.io/linuxserver/deluge:latest"       "Downloaders"  "8112:8112 6881:6881 6881:6881/udp"
    define_app sabnzbd      "SABnzbd"      "lscr.io/linuxserver/sabnzbd:latest"      "Downloaders"  "8090:8080"
    define_app nzbget       "NZBget"       "lscr.io/linuxserver/nzbget:latest"       "Downloaders"  "6789:6789"
    define_app jdownloader2 "JDownloader2" "jlesage/jdownloader-2:latest"            "Downloaders"  "5800:5800"
    define_app pyload       "pyLoad"       "ghcr.io/pyload/pyload:latest"            "Downloaders"  "8000:8000"
    define_app aria2        "Aria2"        "p3terx/aria2-pro:latest"                 "Downloaders"  "6800:6800 6888:6888 6888:6888/udp"
    define_app pinchflat    "Pinchflat"    "ghcr.io/kieraneglin/pinchflat:latest"    "Downloaders"  "8945:8945"
    define_app qbitrr       "qbitrr"       "feramance/qbitrr:latest"                 "Downloaders"  "6969:6969"
    APP_CUSTOM_SVC[qbitrr]="yes"

    # -- ARR Suite --
    define_app prowlarr   "Prowlarr"    "lscr.io/linuxserver/prowlarr:latest"   "ARR Suite"  "9696:9696"
    define_app radarr     "Radarr"      "lscr.io/linuxserver/radarr:latest"     "ARR Suite"  "7878:7878"
    define_app sonarr     "Sonarr"      "lscr.io/linuxserver/sonarr:latest"     "ARR Suite"  "8989:8989"
    define_app lidarr     "Lidarr"      "lscr.io/linuxserver/lidarr:latest"     "ARR Suite"  "8686:8686"
    define_app bazarr     "Bazarr"      "lscr.io/linuxserver/bazarr:latest"     "ARR Suite"  "6767:6767"
    define_app whisparr   "Whisparr"    "ghcr.io/hotio/whisparr:nightly"        "ARR Suite"  "6969:6969"
    define_app readarr    "Readarr"     "ghcr.io/hotio/readarr:nightly"   "ARR Suite"  "8787:8787"
    define_app mylar3     "Mylar3"      "lscr.io/linuxserver/mylar3:latest"     "ARR Suite"  "8090:8090"
    define_app doplarr    "Doplarr"     "lscr.io/linuxserver/doplarr:latest"     "ARR Suite"  ""
    APP_CUSTOM_SVC[doplarr]="yes"
    define_app boxarr     "Boxarr"      "ghcr.io/iongpt/boxarr:latest"           "ARR Suite"  "8888:8888"
    APP_CUSTOM_SVC[boxarr]="yes"
    define_app recyclarr  "Recyclarr"   "ghcr.io/recyclarr/recyclarr:latest"    "ARR Suite"  ""
    define_app unpackerr  "Unpackerr"   "golift/unpackerr:latest"               "ARR Suite"  ""
    define_app notifiarr  "Notifiarr"   "golift/notifiarr:latest"               "ARR Suite"  "5454:5454"

    # -- Media Servers --
    define_app jellyfin "Jellyfin" "jellyfin/jellyfin:latest"   "Media Servers" "8096:8096 8920:8920"
    define_app plex     "Plex"     "plexinc/pms-docker:latest"  "Media Servers" "32400:32400"
    define_app emby     "Emby"     "emby/embyserver:latest"     "Media Servers" "8096:8096"
    define_app navidrome "Navidrome" "deluan/navidrome:latest"   "Media Servers" "4533:4533"
    define_app kavita   "Kavita"   "kizaing/kavita:latest"      "Media Servers" "5000:5000"
    define_app komga    "Komga"    "gotson/komga:latest"        "Media Servers" "8081:8080"
    define_app audiobookshelf "AudiobookShelf" "ghcr.io/advplyr/audiobookshelf:latest" "Media Servers" "13378:80"

    # -- Media Tools --
    define_app tdarr    "Tdarr"      "ghcr.io/haveagitgat/tdarr:latest"  "Media Tools"  "8265:8265 8266:8266"
    define_app fileflows "FileFlows" "revenz/fileflows:latest"             "Media Tools"  "19200:5000"
    define_app handbrake "HandBrake" "jlesage/handbrake:latest"            "Media Tools"  "5800:5800"
    define_app kometa   "Kometa"     "kometateam/kometa:latest"            "Media Tools"  ""
    define_app wizarr   "Wizarr"     "ghcr.io/wizarrrr/wizarr:latest"      "Media Tools"  "5690:5690"
    define_app jellystat "Jellystat" "cyfershepard/jellystat:latest"      "Media Tools"  "3005:3000"

    # -- Request & Monitoring --
    # Seerr — unified media request manager for Jellyfin, Plex & Emby
    # Successor to Jellyseerr and Overseerr. GitHub: seerr-team/seerr
    # Seerr replaces Overseerr and Jellyseerr (both phased out)
    define_app seerr      "Seerr"      "ghcr.io/seerr-team/seerr:latest"      "Request & Tools"  "5055:5055"
    define_app ombi       "Ombi"       "lscr.io/linuxserver/ombi:latest"      "Request & Tools"  "3579:3579"
    define_app requestrr  "Requestrr"  "lscr.io/linuxserver/requestrr:latest" "Request & Tools"  "4545:4545"
    define_app tautulli   "Tautulli"   "lscr.io/linuxserver/tautulli:latest"  "Request & Tools"  "8181:8181"
    define_app flaresolverr "FlareSolverr" "ghcr.io/flaresolverr/flaresolverr:latest" "Request & Tools" "8191:8191"

    # -- Monitoring --
    define_app grafana    "Grafana"     "grafana/grafana:latest"              "Monitoring"  "3000:3000"
    APP_CUSTOM_SVC[grafana]="yes"
    define_app prometheus "Prometheus"  "prom/prometheus:latest"              "Monitoring"  "9090:9090"
    APP_CUSTOM_SVC[prometheus]="yes"
    define_app uptime_kuma "Uptime Kuma" "louislam/uptime-kuma:latest"       "Monitoring"  "3001:3001"
    define_app netdata    "Netdata"     "netdata/netdata:latest"              "Monitoring"  "19999:19999"  "true"
    define_app glances    "Glances"     "nicolargo/glances:latest"            "Monitoring"  "61208:61208"
    define_app dozzle     "Dozzle"      "amir20/dozzle:latest"                "Monitoring"  "8888:8080"
    APP_CUSTOM_SVC[dozzle]="yes"
    define_app portainer  "Portainer"   "portainer/portainer-ce:latest"       "Monitoring"  "9000:9000 9443:9443"
    define_app watchtower "Watchtower"  "containrrr/watchtower:latest"        "Monitoring"  ""
    APP_CUSTOM_SVC[watchtower]="yes"
    define_app scrutiny   "Scrutiny"    "ghcr.io/analogj/scrutiny:master-omnibus" "Monitoring" "8080:8080" "true"
    define_app speedtest  "Speedtest"   "lscr.io/linuxserver/speedtest-tracker:latest" "Monitoring" "8765:80"

    # -- Dashboards --
    define_app homer     "Homer"      "ghcr.io/bastienwirtz/homer:latest"     "Dashboards"  "8085:8080"
    APP_CUSTOM_SVC[homer]="yes"
    define_app homarr    "Homarr"     "ghcr.io/ajnart/homarr:latest"          "Dashboards"  "7575:7575"
    define_app dasherr   "Dasherr"    "ghcr.io/erwin-kok/dasherr:latest"      "Dashboards"  "3080:3080"
    define_app flame     "Flame"      "pawelmalak/flame:latest"               "Dashboards"  "5005:5005"
    define_app heimdall  "Heimdall"   "lscr.io/linuxserver/heimdall:latest"   "Dashboards"  "8086:80 8443:443"
    define_app organizr  "Organizr"   "organizr/organizr:latest"              "Dashboards"  "8089:80"

    # -- Reverse Proxies --
    define_app traefik "Traefik"   "traefik:latest"                      "Reverse Proxies"  "80:80 443:443 8888:8080"
    define_app npm     "NPM"       "jc21/nginx-proxy-manager:latest"     "Reverse Proxies"  "80:80 443:443 81:81"
    define_app caddy   "Caddy"     "caddy:latest"                        "Reverse Proxies"  "80:80 443:443"
    define_app swag    "SWAG"      "lscr.io/linuxserver/swag:latest"     "Reverse Proxies"  "443:443 80:80"

    # -- VPN & Network --
    define_app wireguard "WireGuard"  "lscr.io/linuxserver/wireguard:latest"  "VPN & Network"  "51820:51820/udp"  "true"
    define_app tailscale "Tailscale"  "tailscale/tailscale:latest"            "VPN & Network"  ""  "true"
    define_app gluetun   "Gluetun"    "qmcgaw/gluetun:latest"                 "VPN & Network"  "8888:8888"  "true"
    define_app wg_easy   "WG-Easy"    "ghcr.io/wg-easy/wg-easy:latest"        "VPN & Network"  "51820:51820/udp 51821:51821"  "true"
    define_app adguardhome "AdGuard Home" "adguard/adguardhome:latest"        "VPN & Network"  "53:53 53:53/udp 8082:3000"
    define_app pihole   "Pi-hole"    "pihole/pihole:latest"                 "VPN & Network"  "53:53 53:53/udp 8083:80"
    APP_CUSTOM_SVC[pihole]="yes"
    define_app technitium "Technitium" "technitium/dns-server:latest"        "VPN & Network"  "5380:5380 53:53 53:53/udp"

    # -- Automation --
    define_app n8n       "n8n"           "docker.n8n.io/n8nio/n8n:latest"            "Automation"  "5678:5678"
    define_app huginn    "Huginn"        "ghcr.io/huginn/huginn:latest"              "Automation"  "3007:3000"
    define_app changedetection "Changedetection" "ghcr.io/dgtlmoon/changedetection.io:latest" "Automation" "5000:5000"
    define_app node_red  "Node-RED"     "nodered/node-red:latest"                   "Automation"  "1880:1880"
    define_app activepieces "Activepieces" "activepieces/activepieces:latest"       "Automation"  "8096:80"

    # -- File & Cloud --
    define_app nextcloud "Nextcloud"    "nextcloud:latest"                        "File & Cloud"  "8093:80"
    APP_CUSTOM_SVC[nextcloud]="yes"
    define_app filebrowser "FileBrowser" "filebrowser/filebrowser:latest"         "File & Cloud"  "8084:80"
    define_app syncthing "Syncthing"    "lscr.io/linuxserver/syncthing:latest"    "File & Cloud"  "8384:8384 22000:22000"
    define_app paperless_ngx "Paperless-ngx" "ghcr.io/paperless-ngx/paperless-ngx:latest" "File & Cloud" "8010:8000"
    define_app immich    "Immich"       "ghcr.io/immich-app/immich-server:release" "File & Cloud"  "2283:3001"
    APP_CUSTOM_SVC[immich]="yes"
    define_app photoprism "Photoprism"  "photoprism/photoprism:latest"            "File & Cloud"  "2342:2342"
    define_app stirling_pdf "Stirling PDF" "frooodle/s-pdf:latest"                "File & Cloud"  "8094:8080"

    # -- Security --
    define_app vaultwarden "Vaultwarden" "vaultwarden/server:latest"               "Security"  "8087:80"
    define_app authentik    "Authentik"  "ghcr.io/goauthentik/server:latest"      "Security"  "9000:9000 9443:9443"
    APP_CUSTOM_SVC[authentik]="yes"
    define_app authelia     "Authelia"   "authelia/authelia:latest"                "Security"  "9091:9091"
    define_app crowdsec     "CrowdSec"   "crowdsecurity/crowdsec:latest"           "Security"  ""

    # -- Communication --
    define_app ntfy         "ntfy"       "binwiederhier/ntfy:latest"              "Communication"  "2586:80"
    define_app gotify       "Gotify"     "gotify/server:latest"                   "Communication"  "2587:80"
    define_app matrix_synapse "Matrix Synapse" "matrixdotorg/synapse:latest"     "Communication"  "8448:8448"

    # -- Development --
    define_app gitea       "Gitea"      "gitea/gitea:latest"                    "Development"  "3030:3000 2222:22"
    define_app code_server "Code-Server" "lscr.io/linuxserver/code-server:latest" "Development" "8888:8443"
    define_app drone       "Drone"      "drone/drone:latest"                    "Development"  "8097:80"

    # -- Databases --
    define_app mariadb   "MariaDB"  "lscr.io/linuxserver/mariadb:latest" "Databases"  ""
    APP_CUSTOM_SVC[mariadb]="yes"
    define_app postgres  "PostgreSQL" "postgres:latest"                   "Databases"  "5432:5432"
    APP_CUSTOM_SVC[postgres]="yes"
    define_app redis     "Redis"    "redis:alpine"                       "Databases"  "6379:6379"
    APP_CUSTOM_SVC[redis]="yes"
    define_app mongodb   "MongoDB"  "mongo:latest"                       "Databases"  "27017:27017"
    APP_CUSTOM_SVC[mongodb]="yes"

    # -- Home & Misc --
    define_app mealie          "Mealie"        "hkotel/mealie:latest"                      "Home & Misc"  "9003:9000"
    define_app grocy           "Grocy"         "lscr.io/linuxserver/grocy:latest"          "Home & Misc"  "9283:80"
    define_app freshrss        "FreshRSS"      "freshrss/freshrss:latest"                  "Home & Misc"  "8092:80"
    define_app wallabag        "Wallabag"      "wallabag/wallabag:latest"                  "Home & Misc"  "8095:80"
    define_app linkding        "Linkding"      "sissbruecker/linkding:latest"              "Home & Misc"  "9999:9090"
    define_app calibre_web     "Calibre-Web"   "lscr.io/linuxserver/calibre-web:latest"    "Home & Misc"  "8083:8083"
    define_app actual_budget   "Actual Budget"  "actualbudget/actual-server:latest"        "Home & Misc"  "5006:5006"
    define_app cyberchef       "CyberChef"     "mpepping/cyberchef:latest"                 "Home & Misc"  "8098:8000"

    # -- Deployrr --
    define_app deployrr_webui "Deployrr WebUI" "deployrr-webui:local" "Deployrr" "9999:9999"
    APP_CUSTOM_SVC[deployrr_webui]="yes"
}

ALL_APPS=(
    # Downloaders
    qbittorrent transmission deluge sabnzbd nzbget jdownloader2 pyload aria2 pinchflat qbitrr
    # ARR Suite
    prowlarr radarr sonarr lidarr bazarr whisparr readarr mylar3 doplarr boxarr recyclarr unpackerr notifiarr
    # Media Servers
    jellyfin plex emby navidrome kavita komga audiobookshelf
    # Media Tools
    tdarr fileflows handbrake kometa wizarr jellystat
    # Request & Monitoring
    seerr ombi requestrr tautulli flaresolverr
    grafana prometheus uptime_kuma netdata glances dozzle portainer watchtower scrutiny speedtest
    # Dashboards
    homer homarr dasherr flame heimdall organizr
    # Reverse Proxies
    traefik npm caddy swag
    # VPN & Network
    wireguard tailscale gluetun wg_easy adguardhome pihole technitium
    # Automation
    n8n huginn changedetection node_red activepieces
    # File & Cloud
    nextcloud filebrowser syncthing paperless_ngx immich photoprism stirling_pdf
    # Security
    vaultwarden authentik authelia crowdsec
    # Communication
    ntfy gotify matrix_synapse
    # Development
    gitea code_server drone
    # Databases
    mariadb postgres redis mongodb
    # Home & Misc
    mealie grocy freshrss wallabag linkding calibre_web actual_budget cyberchef
    # Deployrr
    deployrr_webui
)

LOCAL_IMAGE_APPS=(deployrr_webui)

# Full Stack Presets
MINIMAL_STACK=(jellyfin qbittorrent prowlarr sonarr radarr deployrr_webui)
ARR_ONLY_STACK=(prowlarr radarr sonarr lidarr bazarr whisparr readarr qbittorrent seerr boxarr deployrr_webui)
MEDIA_ARR_STACK=(jellyfin qbittorrent prowlarr radarr sonarr lidarr bazarr whisparr readarr seerr boxarr tautulli homer deployrr_webui)
FULL_STACK_ARR=(prowlarr radarr sonarr lidarr bazarr whisparr readarr doplarr boxarr seerr recyclarr unpackerr notifiarr)
FULL_STACK_TOOLS=(seerr tautulli flaresolverr homer homarr deployrr_webui)
FULL_STACK_MONITORING=(grafana prometheus uptime_kuma dozzle watchtower scrutiny speedtest)

# ---------------------------------------------------------------------------
# Requirements check
# ---------------------------------------------------------------------------
check_requirements() {
    log INFO "Checking requirements"

    if ! command -v dialog &>/dev/null; then
        echo "Error: 'dialog' not found.  apt-get install dialog" >&2
        exit 1
    fi

    if [[ $EUID -ne 0 ]]; then
        d_msgbox "Error" "This script must be run as root.\n\nUse: sudo bash $0"
        exit 1
    fi

    if ! command -v docker &>/dev/null; then
        d_msgbox "Docker Not Found" \
"Docker is not installed.\n\nInstall:\n  curl -fsSL https://get.docker.com | sh\n\nThen re-run."
        exit 1
    fi

    if ! docker info &>/dev/null 2>&1; then
        d_msgbox "Docker Error" \
"Docker daemon is not running.\n\nStart it:\n  systemctl start docker"
        exit 1
    fi

    if ! docker compose version &>/dev/null 2>&1; then
        d_msgbox "Compose Not Found" \
"Docker Compose v2 plugin not found.\n\nInstall:\n  apt-get install docker-compose-plugin\n\nThen re-run."
        exit 1
    fi

    log INFO "Requirements OK"
}

# ---------------------------------------------------------------------------
# Directory setup
# ---------------------------------------------------------------------------
ensure_dirs() {
    local missing=()
    [[ ! -d "${MEDIA_DIR}" ]] && missing+=("${MEDIA_DIR}")
    [[ ! -d "${CONFIG_DIR}" ]] && missing+=("${CONFIG_DIR}")
    [[ ${#missing[@]} -eq 0 ]] && return

    if d_yesno "Create Directories" \
"Missing directories:\n\n$(printf '  %s\n' "${missing[@]}")\n\nCreate them now?"; then
        mkdir -p "${MEDIA_DIR}"/{movies,tv,downloads,music,books} "${CONFIG_DIR}" 2>/dev/null || true
        log INFO "Created: ${missing[*]}"
    fi
}

# ---------------------------------------------------------------------------
# Docker Compose file builder
# ---------------------------------------------------------------------------
init_compose() {
    mkdir -p "${CONFIG_DIR}"
    {
        echo "# Generated by Deployrr v${VERSION} on $(date)"
        echo "services:"
    } > "${COMPOSE_FILE}"
    log INFO "Compose initialised: ${COMPOSE_FILE}"
}

# Per-app compose initialiser — creates /docker/<id>/docker-compose.yml
init_app_compose() {
    local id="$1"
    local f; f="$(app_compose "${id}")"
    mkdir -p "${CONFIG_DIR}/${id}"
    {
        echo "# Generated by Deployrr v${VERSION} on $(date)"
        echo "# App: ${APP_NAME[$id]:-$id}"
        echo "services:"
    } > "${f}"
    log INFO "Per-app compose initialised: ${f}"
}

# Generic service block
add_service() {
    local id="$1"

    if [[ -n "${APP_CUSTOM_SVC[$id]:-}" ]]; then
        "add_service_${id}" "${id}"
        return
    fi

    local f; f="$(app_compose "${id}")"
    local image="${APP_IMAGE[$id]}"
    local ports="${APP_PORTS[$id]:-}"
    local priv="${APP_PRIV[$id]:-false}"

    {
        echo ""
        echo "  ${id}:"
        echo "    image: ${image}"
        echo "    platform: linux/amd64"
        echo "    container_name: ${id}"
        echo "    restart: unless-stopped"
        echo "    environment:"
        echo "      - PUID=${PUID_VAL}"
        echo "      - PGID=${PGID_VAL}"
        echo "      - TZ=${TZ_VAL}"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/${id}:/config"
        echo "      - ${MEDIA_DIR}:/media"
    } >> "${f}"

    if [[ "${priv}" == "true" ]]; then
        echo "    privileged: true"   >> "${f}"
        echo "    network_mode: host" >> "${f}"
    fi

    if [[ -n "${ports}" ]]; then
        echo "    ports:" >> "${f}"
        for p in ${ports}; do
            echo "      - \"${p}\"" >> "${f}"
        done
    fi
}

# ---------------------------------------------------------------------------
# Custom service writers
# ---------------------------------------------------------------------------

add_service_doplarr() {
    local id="${1:-doplarr}"
    local f; f="$(app_compose "${id}")"
    {
        echo ""
        echo "  doplarr:"
        echo "    image: lscr.io/linuxserver/doplarr:latest"
        echo "    container_name: doplarr"
        echo "    restart: unless-stopped"
        echo "    environment:"
        echo "      - PUID=${PUID_VAL}"
        echo "      - PGID=${PGID_VAL}"
        echo "      - TZ=${TZ_VAL}"
        echo "      - DISCORD__TOKEN=           # <-- Set your Discord bot token"
        echo "      - OVERSEERR__API=           # <-- Set your Seerr (or Overseerr-compatible) API key"
        echo "      - OVERSEERR__URL=http://localhost:5055"
        echo "      - RADARR__API=              # <-- Set your Radarr API key"
        echo "      - RADARR__URL=http://localhost:7878"
        echo "      - SONARR__API=              # <-- Set your Sonarr API key"
        echo "      - SONARR__URL=http://localhost:8989"
        echo "      - DISCORD__MAX_RESULTS=25"
        echo "      - DISCORD__REQUESTED_MSG_STYLE=:plain"
        echo "      - SONARR__QUALITY_PROFILE="
        echo "      - RADARR__QUALITY_PROFILE="
        echo "      - SONARR__ROOTFOLDER="
        echo "      - RADARR__ROOTFOLDER="
        echo "      - SONARR__LANGUAGE_PROFILE="
        echo "      - OVERSEERR__DEFAULT_ID="
        echo "      - PARTIAL_SEASONS=true"
        echo "      - LOG_LEVEL=:info"
        echo "      - JAVA_OPTS="
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/doplarr:/config"
    } >> "${f}"
}

add_service_boxarr() {
    local id="${1:-boxarr}"
    local f; f="$(app_compose "${id}")"
    mkdir -p "${CONFIG_DIR}/boxarr/config" 2>/dev/null || true
    {
        echo ""
        echo "  boxarr:"
        echo "    image: ghcr.io/iongpt/boxarr:latest"
        echo "    container_name: boxarr"
        echo "    restart: unless-stopped"
        echo "    environment:"
        echo "      - TZ=${TZ_VAL}"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/boxarr/config:/config"
        echo "    ports:"
        echo "      - \"8888:8888\""
    } >> "${f}"
    log INFO "Boxarr configured on port 8888 (ghcr.io/iongpt/boxarr)"
}

add_service_deployrr_webui() {
    local id="${1:-deployrr_webui}"
    local f; f="$(app_compose "${id}")"
    local webui_dir="${SCRIPT_DIR}/deployrr-webui"

    if [[ -d "${webui_dir}" ]]; then
        log INFO "Building deployrr-webui image from ${webui_dir}"
        if docker build -q -t deployrr-webui:local "${webui_dir}" >> "${LOG_FILE}" 2>&1; then
            log INFO "deployrr-webui image built OK"
        else
            log_err "WEBUI_BUILD" "docker build failed — WebUI may not start"
        fi
    else
        if docker image inspect deployrr-webui:local &>/dev/null 2>&1; then
            log INFO "deployrr-webui source dir not found but image already exists — reusing"
        else
            log_err "WEBUI_BUILD" "Source dir not found: ${webui_dir} — image missing too"
        fi
    fi

    {
        echo ""
        echo "  deployrr_webui:"
        echo "    image: deployrr-webui:local"
        echo "    pull_policy: never"
        echo "    container_name: deployrr_webui"
        echo "    restart: unless-stopped"
        echo "    pid: host"
        echo "    ports:"
        echo "      - \"9999:9999\""
        echo "    volumes:"
        echo "      - /var/run/docker.sock:/var/run/docker.sock:ro"
    } >> "${f}"
}

add_service_pihole() {
    local id="${1:-pihole}"
    local f; f="$(app_compose "${id}")"
    local webpass="changeme"
    {
        echo ""
        echo "  pihole:"
        echo "    image: pihole/pihole:latest"
        echo "    container_name: pihole"
        echo "    restart: unless-stopped"
        echo "    environment:"
        echo "      - PUID=${PUID_VAL}"
        echo "      - PGID=${PGID_VAL}"
        echo "      - TZ=${TZ_VAL}"
        echo "      - WEBPASSWORD=${webpass}  # <-- Change this!"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/pihole:/etc/pihole"
        echo "      - ${CONFIG_DIR}/dnsmasq.d:/etc/dnsmasq.d"
        echo "    ports:"
        echo "      - \"53:53/tcp\""
        echo "      - \"53:53/udp\""
        echo "      - \"8083:80\""
        echo "    cap_add:"
        echo "      - NET_ADMIN"
    } >> "${f}"
    log INFO "Pi-hole password set to: ${webpass} — CHANGE THIS!"
}

add_service_nextcloud() {
    local id="${1:-nextcloud}"
    local f; f="$(app_compose "${id}")"
    local db_pass="nextclouddb_$(tr -dc 'a-z0-9' < /dev/urandom 2>/dev/null | head -c16 || echo 'changeme')"
    local root_pass="nextcloudroot_$(tr -dc 'a-z0-9' < /dev/urandom 2>/dev/null | head -c16 || echo 'changeme')"
    {
        echo ""
        echo "  nextcloud-db:"
        echo "    image: lscr.io/linuxserver/mariadb:latest"
        echo "    container_name: nextcloud-db"
        echo "    restart: unless-stopped"
        echo "    environment:"
        echo "      - PUID=${PUID_VAL}"
        echo "      - PGID=${PGID_VAL}"
        echo "      - TZ=${TZ_VAL}"
        echo "      - MYSQL_ROOT_PASSWORD=${root_pass}"
        echo "      - MYSQL_DATABASE=nextcloud"
        echo "      - MYSQL_USER=nextcloud"
        echo "      - MYSQL_PASSWORD=${db_pass}"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/nextcloud-db:/config"
        echo ""
        echo "  nextcloud:"
        echo "    image: nextcloud:latest"
        echo "    container_name: nextcloud"
        echo "    restart: unless-stopped"
        echo "    depends_on:"
        echo "      - nextcloud-db"
        echo "    environment:"
        echo "      - PUID=${PUID_VAL}"
        echo "      - PGID=${PGID_VAL}"
        echo "      - TZ=${TZ_VAL}"
        echo "      - MYSQL_HOST=nextcloud-db"
        echo "      - MYSQL_DATABASE=nextcloud"
        echo "      - MYSQL_USER=nextcloud"
        echo "      - MYSQL_PASSWORD=${db_pass}"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/nextcloud:/config"
        echo "      - ${MEDIA_DIR}:/media"
        echo "    ports:"
        echo "      - \"8093:80\""
    } >> "${f}"
    mkdir -p "${CONFIG_DIR}/nextcloud-db" 2>/dev/null || true
    log INFO "Nextcloud DB password: ${db_pass}"
}

add_service_immich() {
    local id="${1:-immich}"
    local f; f="$(app_compose "${id}")"
    local db_pass="immichdb_$(tr -dc 'a-z0-9' < /dev/urandom 2>/dev/null | head -c16 || echo 'changeme')"
    {
        echo ""
        echo "  immich-postgres:"
        echo "    image: postgres:latest"
        echo "    container_name: immich-postgres"
        echo "    restart: unless-stopped"
        echo "    environment:"
        echo "      - POSTGRES_USER=immich"
        echo "      - POSTGRES_PASSWORD=${db_pass}"
        echo "      - POSTGRES_DB=immich"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/immich-postgres:/var/lib/postgresql/data"
        echo ""
        echo "  immich-redis:"
        echo "    image: redis:alpine"
        echo "    container_name: immich-redis"
        echo "    restart: unless-stopped"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/immich-redis:/data"
        echo ""
        echo "  immich-server:"
        echo "    image: ghcr.io/immich-app/immich-server:release"
        echo "    container_name: immich-server"
        echo "    restart: unless-stopped"
        echo "    depends_on:"
        echo "      - immich-postgres"
        echo "      - immich-redis"
        echo "    environment:"
        echo "      - PUID=${PUID_VAL}"
        echo "      - PGID=${PGID_VAL}"
        echo "      - TZ=${TZ_VAL}"
        echo "      - DB_HOSTNAME=immich-postgres"
        echo "      - DB_USERNAME=immich"
        echo "      - DB_PASSWORD=${db_pass}"
        echo "      - DB_DATABASE_NAME=immich"
        echo "      - REDIS_HOSTNAME=immich-redis"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/immich-server:/config"
        echo "      - ${MEDIA_DIR}:/media"
        echo "    ports:"
        echo "      - \"2283:3001\""
    } >> "${f}"
    mkdir -p "${CONFIG_DIR}/immich-postgres" "${CONFIG_DIR}/immich-redis" "${CONFIG_DIR}/immich-server" 2>/dev/null || true
    log INFO "Immich DB password: ${db_pass}"
}

add_service_authentik() {
    local id="${1:-authentik}"
    local f; f="$(app_compose "${id}")"
    local db_pass="authentikdb_$(tr -dc 'a-z0-9' < /dev/urandom 2>/dev/null | head -c16 || echo 'changeme')"
    local secret_key="authentik_$(tr -dc 'a-zA-Z0-9' < /dev/urandom 2>/dev/null | head -c32 || echo 'changeme')"
    {
        echo ""
        echo "  authentik-postgres:"
        echo "    image: postgres:latest"
        echo "    container_name: authentik-postgres"
        echo "    restart: unless-stopped"
        echo "    environment:"
        echo "      - POSTGRES_USER=authentik"
        echo "      - POSTGRES_PASSWORD=${db_pass}"
        echo "      - POSTGRES_DB=authentik"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/authentik-postgres:/var/lib/postgresql/data"
        echo ""
        echo "  authentik-redis:"
        echo "    image: redis:alpine"
        echo "    container_name: authentik-redis"
        echo "    restart: unless-stopped"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/authentik-redis:/data"
        echo ""
        echo "  authentik-server:"
        echo "    image: ghcr.io/goauthentik/server:latest"
        echo "    container_name: authentik-server"
        echo "    restart: unless-stopped"
        echo "    depends_on:"
        echo "      - authentik-postgres"
        echo "      - authentik-redis"
        echo "    environment:"
        echo "      - PUID=${PUID_VAL}"
        echo "      - PGID=${PGID_VAL}"
        echo "      - TZ=${TZ_VAL}"
        echo "      - AUTHENTIK_REDIS__HOST=authentik-redis"
        echo "      - AUTHENTIK_POSTGRESQL__HOST=authentik-postgres"
        echo "      - AUTHENTIK_POSTGRESQL__USER=authentik"
        echo "      - AUTHENTIK_POSTGRESQL__NAME=authentik"
        echo "      - AUTHENTIK_POSTGRESQL__PASSWORD=${db_pass}"
        echo "      - AUTHENTIK_SECRET_KEY=${secret_key}"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/authentik-server:/config"
        echo "    ports:"
        echo "      - \"9000:9000\""
        echo "      - \"9443:9443\""
    } >> "${f}"
    mkdir -p "${CONFIG_DIR}/authentik-postgres" "${CONFIG_DIR}/authentik-redis" "${CONFIG_DIR}/authentik-server" 2>/dev/null || true
    log INFO "Authentik DB password: ${db_pass}"
}

add_service_mariadb() {
    local id="${1:-mariadb}"
    local f; f="$(app_compose "${id}")"
    local root_pass="mariadb_$(tr -dc 'a-z0-9' < /dev/urandom 2>/dev/null | head -c16 || echo 'changeme')"
    local user_pass="deployrr_$(tr -dc 'a-z0-9' < /dev/urandom 2>/dev/null | head -c16 || echo 'changeme')"
    {
        echo ""
        echo "  mariadb:"
        echo "    image: lscr.io/linuxserver/mariadb:latest"
        echo "    container_name: mariadb"
        echo "    restart: unless-stopped"
        echo "    environment:"
        echo "      - PUID=${PUID_VAL}"
        echo "      - PGID=${PGID_VAL}"
        echo "      - TZ=${TZ_VAL}"
        echo "      - MYSQL_ROOT_PASSWORD=${root_pass}"
        echo "      - MYSQL_DATABASE=deployrr"
        echo "      - MYSQL_USER=deployrr"
        echo "      - MYSQL_PASSWORD=${user_pass}"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/mariadb:/config"
    } >> "${f}"
    log INFO "MariaDB root password: ${root_pass}"
    log INFO "MariaDB user (deployrr) password: ${user_pass}"
}

add_service_postgres() {
    local id="${1:-postgres}"
    local f; f="$(app_compose "${id}")"
    local pass="postgres_$(tr -dc 'a-z0-9' < /dev/urandom 2>/dev/null | head -c16 || echo 'changeme')"
    {
        echo ""
        echo "  postgres:"
        echo "    image: postgres:latest"
        echo "    container_name: postgres"
        echo "    restart: unless-stopped"
        echo "    environment:"
        echo "      - POSTGRES_USER=deployrr"
        echo "      - POSTGRES_PASSWORD=${pass}"
        echo "      - POSTGRES_DB=deployrr"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/postgres:/var/lib/postgresql/data"
        echo "    ports:"
        echo "      - \"5432:5432\""
    } >> "${f}"
    log INFO "PostgreSQL password: ${pass}"
}

add_service_redis() {
    local id="${1:-redis}"
    local f; f="$(app_compose "${id}")"
    {
        echo ""
        echo "  redis:"
        echo "    image: redis:alpine"
        echo "    container_name: redis"
        echo "    restart: unless-stopped"
        echo "    command: redis-server --appendonly yes"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/redis:/data"
        echo "    ports:"
        echo "      - \"6379:6379\""
    } >> "${f}"
}

add_service_mongodb() {
    local id="${1:-mongodb}"
    local f; f="$(app_compose "${id}")"
    local pass="mongodb_$(tr -dc 'a-z0-9' < /dev/urandom 2>/dev/null | head -c16 || echo 'changeme')"
    {
        echo ""
        echo "  mongodb:"
        echo "    image: mongo:latest"
        echo "    container_name: mongodb"
        echo "    restart: unless-stopped"
        echo "    environment:"
        echo "      - MONGO_INITDB_ROOT_USERNAME=admin"
        echo "      - MONGO_INITDB_ROOT_PASSWORD=${pass}"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/mongodb:/data/db"
        echo "    ports:"
        echo "      - \"27017:27017\""
    } >> "${f}"
    log INFO "MongoDB admin password: ${pass}"
}

# ---------------------------------------------------------------------------
# New custom service writers for pre-configured apps
# ---------------------------------------------------------------------------
add_service_qbittorrent() {
    local id="${1:-qbittorrent}"
    local f; f="$(app_compose "${id}")"
    {
        echo ""
        echo "  qbittorrent:"
        echo "    image: lscr.io/linuxserver/qbittorrent:latest"
        echo "    container_name: qbittorrent"
        echo "    restart: unless-stopped"
        echo "    environment:"
        echo "      - PUID=${PUID_VAL}"
        echo "      - PGID=${PGID_VAL}"
        echo "      - TZ=${TZ_VAL}"
        echo "      - WEBUI_PORT=8080"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/qbittorrent:/config"
        echo "      - ${MEDIA_DIR}/downloads:/downloads"
        echo "    ports:"
        echo "      - "8080:8080""
        echo "      - "6881:6881""
        echo "      - "6881:6881/udp""
    } >> "${f}"
    log INFO "qBittorrent configured with downloads at ${MEDIA_DIR}/downloads"
    printf '\n\033[1;33m  ▶ qBittorrent default credentials: admin / adminadmin\n  ▶ Change immediately after first login!\033[0m\n'
}

add_service_prometheus() {
    local id="${1:-prometheus}"
    local f; f="$(app_compose "${id}")"
    mkdir -p "${CONFIG_DIR}/prometheus/data" 2>/dev/null || true
    # UID 65534 (nobody) is what the prom/prometheus image runs as — fix ownership
    chown -R 65534:65534 "${CONFIG_DIR}/prometheus/data" 2>/dev/null || true
    cat > "${CONFIG_DIR}/prometheus/prometheus.yml" << 'PROM_EOF'
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: 'prometheus'
    static_configs:
      - targets: ['localhost:9090']

  - job_name: 'node'
    static_configs:
      - targets: ['host.docker.internal:9100']

  - job_name: 'cadvisor'
    static_configs:
      - targets: ['cadvisor:8080']

  - job_name: 'docker'
    static_configs:
      - targets: ['host.docker.internal:9323']
PROM_EOF
    {
        echo ""
        echo "  prometheus:"
        echo "    image: prom/prometheus:latest"
        echo "    container_name: prometheus"
        echo "    restart: unless-stopped"
        echo "    user: \"65534:65534\""
        echo "    command:"
        echo "      - '--config.file=/etc/prometheus/prometheus.yml'"
        echo "      - '--storage.tsdb.path=/prometheus'"
        echo "      - '--web.console.libraries=/etc/prometheus/console_libraries'"
        echo "      - '--web.console.templates=/etc/prometheus/consoles'"
        echo "      - '--web.enable-lifecycle'"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro"
        echo "      - ${CONFIG_DIR}/prometheus/data:/prometheus"
        echo "    ports:"
        echo "      - \"9090:9090\""
        echo "    extra_hosts:"
        echo "      - host.docker.internal:host-gateway"
    } >> "${f}"
    log INFO "Prometheus configured with default scrape targets"
}

add_service_grafana() {
    local id="${1:-grafana}"
    local f; f="$(app_compose "${id}")"
    local grafana_pass="admin"
    mkdir -p "${CONFIG_DIR}/grafana/data" 2>/dev/null || true
    mkdir -p "${CONFIG_DIR}/grafana/provisioning/datasources" 2>/dev/null || true
    mkdir -p "${CONFIG_DIR}/grafana/provisioning/dashboards" 2>/dev/null || true
    mkdir -p "${CONFIG_DIR}/grafana/dashboards" 2>/dev/null || true
    # UID 472 is grafana's default user inside the container — fix ownership upfront
    chown -R 472:472 "${CONFIG_DIR}/grafana" 2>/dev/null || true

    cat > "${CONFIG_DIR}/grafana/provisioning/datasources/prometheus.yaml" << 'GRAFANA_DS_EOF'
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    editable: true
GRAFANA_DS_EOF

    cat > "${CONFIG_DIR}/grafana/provisioning/dashboards/default.yaml" << 'GRAFANA_DB_EOF'
apiVersion: 1
providers:
  - name: 'default'
    orgId: 1
    folder: ''
    type: file
    disableDeletion: false
    options:
      path: /var/lib/grafana/dashboards
GRAFANA_DB_EOF

    {
        echo ""
        echo "  grafana:"
        echo "    image: grafana/grafana:latest"
        echo "    container_name: grafana"
        echo "    restart: unless-stopped"
        echo "    user: \"472\""
        echo "    environment:"
        echo "      - GF_SECURITY_ADMIN_USER=admin"
        echo "      - GF_SECURITY_ADMIN_PASSWORD=${grafana_pass}"
        echo "      - GF_USERS_ALLOW_SIGN_UP=false"
        echo "      - GF_PATHS_DATA=/var/lib/grafana"
        echo "      - GF_PATHS_LOGS=/var/log/grafana"
        echo "      - GF_PATHS_PLUGINS=/var/lib/grafana/plugins"
        echo "      - GF_INSTALL_PLUGINS=grafana-clock-panel,grafana-simple-json-datasource"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/grafana/data:/var/lib/grafana"
        echo "      - ${CONFIG_DIR}/grafana/provisioning:/etc/grafana/provisioning"
        echo "      - ${CONFIG_DIR}/grafana/dashboards:/var/lib/grafana/dashboards"
        echo "    ports:"
        echo "      - \"3000:3000\""
    } >> "${f}"
    log INFO "Grafana configured with Prometheus datasource pre-wired (admin/${grafana_pass})"
    printf '\n\033[1;33m  ▶ Grafana credentials: admin / %s\n  ▶ Prometheus datasource is pre-configured\033[0m\n' "${grafana_pass}"
}

# Dozzle — requires Docker socket; without it the container fatals immediately
add_service_dozzle() {
    local id="${1:-dozzle}"
    local f; f="$(app_compose "${id}")"
    {
        echo ""
        echo "  dozzle:"
        echo "    image: amir20/dozzle:latest"
        echo "    container_name: dozzle"
        echo "    restart: unless-stopped"
        echo "    volumes:"
        echo "      - /var/run/docker.sock:/var/run/docker.sock:ro"
        echo "    ports:"
        echo "      - \"8888:8080\""
    } >> "${f}"
    log INFO "Dozzle configured with Docker socket (read-only)"
}

# Watchtower — requires Docker socket to poll registries and restart containers
add_service_watchtower() {
    local id="${1:-watchtower}"
    local f; f="$(app_compose "${id}")"
    {
        echo ""
        echo "  watchtower:"
        echo "    image: containrrr/watchtower:latest"
        echo "    container_name: watchtower"
        echo "    restart: unless-stopped"
        echo "    volumes:"
        echo "      - /var/run/docker.sock:/var/run/docker.sock"
        echo "    environment:"
        echo "      - WATCHTOWER_CLEANUP=true"
        echo "      - WATCHTOWER_POLL_INTERVAL=86400"
        echo "      - WATCHTOWER_INCLUDE_STOPPED=false"
        echo "      - TZ=${TZ_VAL}"
    } >> "${f}"
    log INFO "Watchtower configured — will poll for updates every 24h"
}

add_service_homer() {
    local id="${1:-homer}"
    local f; f="$(app_compose "${id}")"
    mkdir -p "${CONFIG_DIR}/homer/assets" 2>/dev/null || true
    
    cat > "${CONFIG_DIR}/homer/config.yml" << 'HOMER_EOF'
title: "My Homelab"
subtitle: "Powered by Deployrr"
logo: "assets/logo.png"
header: true
footer: '<p>Deployrr — <a href="https://github.com">GitHub</a></p>'

theme: default
colors:
  light:
    highlight-primary: "#3367d6"
    highlight-secondary: "#4f7ef0"
  dark:
    highlight-primary: "#3367d6"
    highlight-secondary: "#4f7ef0"

links:
  - name: "GitHub"
    icon: "fab fa-github"
    url: "https://github.com"

services:
  - name: "Media"
    icon: "fas fa-film"
    items:
      - name: "Jellyfin"
        icon: "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/jellyfin.png"
        url: "http://HOST_IP:8096"
        subtitle: "Media Server"
      - name: "Plex"
        icon: "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/plex.png"
        url: "http://HOST_IP:32400/web"
        subtitle: "Media Server"
  - name: "ARR Suite"
    icon: "fas fa-search"
    items:
      - name: "Prowlarr"
        icon: "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/prowlarr.png"
        url: "http://HOST_IP:9696"
        subtitle: "Indexer Manager"
      - name: "Radarr"
        icon: "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/radarr.png"
        url: "http://HOST_IP:7878"
        subtitle: "Movies"
      - name: "Sonarr"
        icon: "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/sonarr.png"
        url: "http://HOST_IP:8989"
        subtitle: "TV Shows"
  - name: "Downloads"
    icon: "fas fa-download"
    items:
      - name: "qBittorrent"
        icon: "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/qbittorrent.png"
        url: "http://HOST_IP:8080"
        subtitle: "Torrent Client"
  - name: "Tools"
    icon: "fas fa-tools"
    items:
      - name: "Deployrr"
        icon: "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/docker.png"
        url: "http://HOST_IP:9999"
        subtitle: "Server Dashboard"
HOMER_EOF
    
    local server_ip
    server_ip=$(hostname -I | awk '{print $1}' 2>/dev/null || echo "localhost")
    sed -i "s/HOST_IP/${server_ip}/g" "${CONFIG_DIR}/homer/config.yml" 2>/dev/null || true

    {
        echo ""
        echo "  homer:"
        echo "    image: ghcr.io/bastienwirtz/homer:latest"
        echo "    container_name: homer"
        echo "    restart: unless-stopped"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/homer:/www/assets"
        echo "    ports:"
        echo "      - "8085:8080""
        echo "    environment:"
        echo "      - INIT_ASSETS=0"
    } >> "${f}"
    log INFO "Homer configured with auto-generated config.yml"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load app catalog from generated catalog.sh (auto-download from GitHub if missing)
CATALOG_SH="${SCRIPT_DIR}/apps/catalog.sh"
if [[ ! -f "${CATALOG_SH}" ]]; then
    log INFO "catalog.sh not found at ${CATALOG_SH} — downloading from GitHub..."
    mkdir -p "${SCRIPT_DIR}/apps"
    if curl -fsSL "${GITHUB_RAW}/apps/catalog.sh" -o "${CATALOG_SH}" 2>/dev/null; then
        log INFO "catalog.sh downloaded from GitHub OK"
    else
        log_err "main" "catalog.sh download failed — app catalog unavailable. Run: python3 ${SCRIPT_DIR}/scripts/gen_catalog_sh.py"
    fi
fi
if [[ -f "${CATALOG_SH}" ]]; then
    # shellcheck source=/dev/null
    source "${CATALOG_SH}"
else
    log_err "main" "catalog.sh still missing — app menus will be empty"
fi

# ---------------------------------------------------------------------------
# Filter/search apps in menus
# ---------------------------------------------------------------------------
filter_apps() {
    local -n items_ref="$1"
    local search_term

    search_term=$(d_inputbox "Search Apps" "Enter app name or category to filter:\n\n(Leave empty to cancel)" "") || return 1

    if [[ -z "${search_term}" ]]; then
        return 1
    fi

    local filtered=()
    for i in "${!items_ref[@]}"; do
        if [[ "${items_ref[$i]}" =~ ${search_term} ]]; then
            filtered+=("${items_ref[$i]}")
        fi
    done

    if [[ ${#filtered[@]} -eq 0 ]]; then
        d_msgbox "No Match" "No apps found matching: ${search_term}"
        return 1
    fi

    items_ref=("${filtered[@]}")
    return 0
}

# ---------------------------------------------------------------------------
# Self-update function
# ---------------------------------------------------------------------------
self_update() {
    local script_path
    script_path="$(realpath "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"

    d_infobox "Deployrr Update" "Downloading latest version from GitHub...\n\nThis may take a moment."

    local tmp_file="${TMP_DIR}/deployrr_latest.sh"

    if curl -fsSL "${GITHUB_RAW}/deployrr.sh" -o "${tmp_file}" 2>/dev/null; then
        if [[ -s "${tmp_file}" ]]; then
            chmod +x "${tmp_file}"
            cp "${tmp_file}" "${script_path}"
            log INFO "Self-update completed successfully"
            d_msgbox "Update Complete" "Deployrr has been updated to the latest version.\n\nPlease restart the script."
            exit 0
        else
            log_err "UPDATE" "Downloaded file is empty"
            d_msgbox "Update Failed" "Downloaded file is empty. Check your internet connection."
        fi
    else
        log_err "UPDATE" "Failed to download from GitHub"
        d_msgbox "Update Failed" "Could not download from:\n${GITHUB_RAW}/deployrr.sh\n\nCheck your internet connection."
    fi
}

# ---------------------------------------------------------------------------
# About menu
# ---------------------------------------------------------------------------
about_menu() {
    local sys_info
    sys_info=$(uname -a)
    local docker_version
    docker_version=$(docker --version 2>/dev/null || echo "Not found")
    local compose_version
    compose_version=$(docker compose version 2>/dev/null | grep -oP '(?<=, version )[^,]+' || echo "Unknown")

    d_msgbox "About Deployrr" \
"Deployrr v${VERSION}
Production-ready Docker-based media server deployment

GitHub: https://github.com/${GITHUB_USER}/${GITHUB_REPO}
Branch: ${GITHUB_BRANCH}

System Info:
  $(echo "${sys_info}" | cut -d' ' -f1-3)
  ${docker_version}
  Docker Compose: ${compose_version}

Config: ${CONFIG_DIR}
Media: ${MEDIA_DIR}
Timezone: ${TZ_VAL}
PUID/PGID: ${PUID_VAL}/${PGID_VAL}

Logged to:
  ${LOG_FILE}
  ${ERR_FILE}"
}

# ---------------------------------------------------------------------------
# Core deploy function
# ---------------------------------------------------------------------------
deploy_apps() {
    local apps=("$@")
    [[ ${#apps[@]} -eq 0 ]] && { d_msgbox "Nothing Selected" "No applications were selected."; return; }

    local app_list=""
    for id in "${apps[@]}"; do
        app_list+="  * ${APP_NAME[$id]:-$id}\n"
    done

    if ! d_yesno "Confirm Deploy" \
"The following will be deployed:\n\n${app_list}\nProceed?"; then
        log INFO "Deploy cancelled by user at confirm"
        return
    fi

    local _cancelled=false
    trap '_cancelled=true' INT

    local -a ok_pull=()
    local -a fail_pull=()
    local -a ok_start=()
    local -a fail_start=()
    local total=${#apps[@]}

    log_section

    d_infobox "Deployrr" "Writing per-app compose files...\n\nApps: ${total}"
    for id in "${apps[@]}"; do
        init_app_compose "${id}"
        add_service "${id}"
    done
    log INFO "Per-app compose files written for ${total} apps"

    log INFO "Starting pull phase (${total} images)"

    local idx=0
    local gauge_fifo="${TMP_DIR}/gauge.fifo"
    mkfifo "${gauge_fifo}"

    dialog --clear --backtitle "${BACKTITLE}" \
           --title "Deployrr — Pulling Images" \
           --gauge "Preparing..." 10 70 0 < "${gauge_fifo}" &
    local gauge_pid=$!

    exec 9>"${gauge_fifo}"

    for id in "${apps[@]}"; do
        (( idx++ )) || true

        if ${_cancelled}; then
            log_err "PULL" "Deploy cancelled by user (after ${idx} of ${total})"
            break
        fi

        local image="${APP_IMAGE[$id]}"
        local pull_out="${TMP_DIR}/pull_${id}.log"
        local pct=$(( idx * 100 / total ))
        local label="[${idx}/${total}]  ${APP_NAME[$id]:-$id}"

        printf '%d\n# %s\n' "${pct}" "${label}" >&9

        local is_local=false
        for lid in "${LOCAL_IMAGE_APPS[@]}"; do
            [[ "${id}" == "${lid}" ]] && is_local=true && break
        done

        if ${is_local}; then
            if docker image inspect "${image}" &>/dev/null 2>&1; then
                log INFO "Pull SKIP (local image exists) [${idx}/${total}] ${id}  image=${image}"
                ok_pull+=("${id}")
                printf '%d\n# [LOCAL OK] %s\n' "${pct}" "${label}" >&9
            else
                log_err "PULL" "${id} SKIPPED — local image ${image} not found (build failed or not yet built)"
                fail_pull+=("${id}  [local image missing — run: docker build -t ${image} ${SCRIPT_DIR}/deployrr-webui]")
                printf '%d\n# [LOCAL MISSING] %s\n' "${pct}" "${label}" >&9
            fi
            continue
        fi

        log INFO "Pull [${idx}/${total}] ${id}  image=${image}"

        local pull_exit=0
        docker compose -f "$(app_compose "${id}")" pull > "${pull_out}" 2>&1 || pull_exit=$?

        log_raw "pull:${id}" "${pull_out}"

        if [[ ${pull_exit} -eq 0 ]]; then
            ok_pull+=("${id}")
            log INFO "Pull OK: ${id}"
        elif [[ ${pull_exit} -eq 130 ]]; then
            _cancelled=true
            fail_pull+=("${id}  [interrupted — Ctrl+C]")
            log_err "PULL" "${id} INTERRUPTED (exit 130)"
            log_raw_err "pull:${id}:interrupted" "${pull_out}"
            break
        else
            fail_pull+=("${id}  [pull exit=${pull_exit}]")
            log_err "PULL" "${id} FAILED (exit ${pull_exit})  image=${image}"
            log_raw_err "pull:${id}:failed" "${pull_out}"
        fi
    done

    exec 9>&-
    wait "${gauge_pid}" 2>/dev/null || true

    if ${_cancelled}; then
        trap - INT
        log_err "DEPLOY" "Deploy aborted — Ctrl+C during pull"
        d_msgbox "Deploy Aborted" \
"Deploy was cancelled.\n\nPulled:  ${#ok_pull[@]}\nFailed:  ${#fail_pull[@]}\n\nSee: Main Menu > Logs > Error Log"
        _write_report ok_pull fail_pull ok_start fail_start "${total}" "ABORTED (Ctrl+C during pull)"
        return
    fi

    local start_msg="Starting containers...\n\nPulled OK: ${#ok_pull[@]} of ${total}"
    [[ ${#fail_pull[@]} -gt 0 ]] && start_msg+="\nFailed pulls: ${#fail_pull[@]} (skipped)"
    d_infobox "Deployrr — Starting" "${start_msg}"

    local compose_out="${TMP_DIR}/compose_up.log"

    if [[ ${#ok_pull[@]} -eq 0 ]]; then
        d_msgbox "Nothing to Start" "No images pulled successfully — nothing to start."
        log_err "COMPOSE" "Skipped — no successful pulls"
        trap - INT
        _write_report ok_start fail_start ok_pull fail_pull "${total}" "NO IMAGES PULLED"
        return
    fi

    log INFO "Starting per-app compose up for ${#ok_pull[@]} apps"

    local any_compose_fail=false
    for _cid in "${ok_pull[@]}"; do
        local _f; _f="$(app_compose "${_cid}")"
        local _up_out="${TMP_DIR}/up_${_cid}.log"

        # Pre-remove any stale container with same name
        if docker inspect "${_cid}" &>/dev/null 2>&1; then
            log INFO "Pre-removing existing container: ${_cid}"
            docker rm -f "${_cid}" >> "${LOG_FILE}" 2>&1 || true
        fi

        local _up_exit=0
        docker compose -f "${_f}" up -d > "${_up_out}" 2>&1 || _up_exit=$?
        log_raw "compose_up:${_cid}" "${_up_out}"
        cat "${_up_out}" >> "${compose_out}" || true

        if [[ ${_up_exit} -ne 0 ]]; then
            log_err "COMPOSE" "docker compose up failed for ${_cid} (exit ${_up_exit})"
            log_raw_err "compose_up:${_cid}:failed" "${_up_out}"
            any_compose_fail=true
        else
            log INFO "docker compose up OK: ${_cid}"
        fi
    done

    if ${any_compose_fail}; then
        log_err "COMPOSE" "One or more apps failed to start — see error log"
    else
        log INFO "All per-app compose ups completed successfully"
    fi

    d_infobox "Deployrr" "Verifying container states...\n\nPlease wait (5 seconds)."
    sleep 5

    for id in "${apps[@]}"; do
        local skip=false
        for pf in "${fail_pull[@]:-}"; do
            [[ "${pf}" == "${id}"* ]] && skip=true && break
        done
        if ${skip}; then
            fail_start+=("${id}  [reason: pull failed]")
            log INFO "Verify skip: ${id} (pull failed)"
            continue
        fi

        local state
        state=$(docker inspect --format '{{.State.Status}}' "${id}" 2>/dev/null | tr -d '\n\r' || echo "missing")
        log INFO "Verify ${id}: state=${state}"

        if [[ "${state}" == "running" ]]; then
            ok_start+=("${id}")
        else
            fail_start+=("${id}  [reason: state=${state}]")
            log_err "VERIFY" "${id} not running (state=${state})"
            local clog="${TMP_DIR}/clog_${id}.log"
            docker logs --tail=60 "${id}" > "${clog}" 2>&1 || true
            log_raw_err "container_logs:${id}" "${clog}"
        fi
    done

    trap - INT
    _write_report ok_start fail_start ok_pull fail_pull "${total}" ""
}

_write_report() {
    local -n _ok_start="$1"
    local -n _fail_start="$2"
    local -n _ok_pull="$3"
    local -n _fail_pull="$4"
    local total="$5"
    local note="${6:-}"

    local report="${TMP_DIR}/report.txt"
    {
        printf 'DEPLOYMENT REPORT  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
        [[ -n "${note}" ]] && printf 'STATUS: %s\n' "${note}"
        printf '%s\n\n' "$(printf -- '-%.0s' {1..58})"

        printf 'CONTAINERS RUNNING  (%d of %d):\n' "${#_ok_start[@]}" "${total}"
        if [[ ${#_ok_start[@]} -gt 0 ]]; then
            for id in "${_ok_start[@]}"; do
                printf '  [OK  ] %s\n' "${APP_NAME[$id]:-$id}"
            done
        else
            printf '  (none)\n'
        fi

        printf '\nFAILED TO START  (%d of %d):\n' "${#_fail_start[@]}" "${total}"
        if [[ ${#_fail_start[@]} -gt 0 ]]; then
            for entry in "${_fail_start[@]}"; do
                printf '  [FAIL] %s\n' "${entry}"
            done
        else
            printf '  (none — all containers running)\n'
        fi

        if [[ ${#_fail_pull[@]} -gt 0 ]]; then
            printf '\nIMAGE PULL FAILURES  (%d):\n' "${#_fail_pull[@]}"
            for entry in "${_fail_pull[@]}"; do
                printf '  [PULL] %s\n' "${entry}"
            done
        fi

        if [[ ${#_fail_start[@]} -gt 0 || ${#_fail_pull[@]} -gt 0 ]]; then
            printf '\nDIAGNOSTICS:\n'
            printf '  Error log (pull output, container logs):\n'
            printf '    %s\n' "${ERR_FILE}"
            printf '  Full activity log:\n'
            printf '    %s\n' "${LOG_FILE}"
            printf '\n  View both from:  Main Menu > Logs\n'
        fi
    } > "${report}"

    {
        printf '\n--- DEPLOYMENT REPORT ---\n'
        cat "${report}"
        printf '--- END REPORT ---\n\n'
    } >> "${ERR_FILE}" 2>/dev/null || true

    log INFO "Report: OK=${#_ok_start[@]} FAIL=${#_fail_start[@]} PULL_FAIL=${#_fail_pull[@]} TOTAL=${total}"

    if [[ ${#_fail_start[@]} -gt 0 || ${#_fail_pull[@]} -gt 0 ]]; then
        d_textbox "Deployment Report  *** ERRORS FOUND ***" "${report}"
        if d_yesno "View Error Log" \
"Open the error log now?\nContains pull output and container crash logs.\n\n${ERR_FILE}"; then
            d_textbox "Error Log — ${ERR_FILE}" "${ERR_FILE}"
        fi
    else
        d_msgbox "Deploy Complete" \
"All ${#_ok_start[@]} containers started successfully.\n\nFull log: ${LOG_FILE}"
    fi
}

# ---------------------------------------------------------------------------
# Deploy menus
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Media Server Deployment Wizard
# ---------------------------------------------------------------------------
deploy_media_wizard() {
    local media_server=""
    media_server=$(d_menu "Step 1 — Media Server" "Choose your media server:" \
        "jellyfin"  "Jellyfin (recommended, free)" \
        "plex"      "Plex (freemium)" \
        "emby"      "Emby (freemium)" \
        "none"      "None - skip media server") || return
    
    [[ "${media_server}" == "none" ]] && media_server=""

    local downloader=""
    downloader=$(d_menu "Step 2 — Download Client" "Choose your download client:" \
        "qbittorrent" "qBittorrent (recommended)" \
        "transmission" "Transmission (lightweight)" \
        "deluge"      "Deluge (plugin-rich)" \
        "sabnzbd"     "SABnzbd (Usenet/NZB)" \
        "none"        "None - skip downloader") || return
    
    [[ "${downloader}" == "none" ]] && downloader=""

    # ARR Apps selection with defaults
    local arr_items=(
        "prowlarr"  "Prowlarr (indexer manager)" "on"
        "radarr"    "Radarr (movies)" "on"
        "sonarr"    "Sonarr (TV shows)" "on"
        "lidarr"    "Lidarr (music)" "off"
        "bazarr"    "Bazarr (subtitles)" "on"
        "whisparr"  "Whisparr (adult content)" "off"
        "readarr"   "Readarr (ebooks)" "off"
        "mylar3"    "Mylar3 (comics)" "off"
        "boxarr"    "Boxarr (media database browser)" "on"
           "Huntarr (dashboard)" "off"
        "recyclarr" "Recyclarr (config management)" "off"
        "unpackerr" "Unpackerr (archive extraction)" "on"
        "notifiarr" "Notifiarr (notifications)" "off"
    )

    local sel_arr
    sel_arr=$(d_checklist "Step 3 — ARR Suite Apps" \
        "Select which ARR apps to deploy (adjust defaults as needed):" "${arr_items[@]}") || return
    sel_arr=$(printf '%s' "${sel_arr}" | tr -d '"')

    # Request & Tools selection with defaults
    local tools_items=(
        "seerr"       "Seerr (media requests)" "on"
        "tautulli"    "Tautulli (watch stats)" "on"
        "flaresolverr" "FlareSolverr (captcha solver)" "on"
        "homer"       "Homer (dashboard)" "off"
        "homarr"      "Homarr (dashboard)" "off"
    )

    local sel_tools
    sel_tools=$(d_checklist "Step 4 — Request & Tools" \
        "Select optional request management & monitoring tools:" "${tools_items[@]}") || return
    sel_tools=$(printf '%s' "${sel_tools}" | tr -d '"')

    # Combine selections
    local final_selection=()
    [[ -n "${media_server}" ]] && final_selection+=("${media_server}")
    [[ -n "${downloader}" ]] && final_selection+=("${downloader}")
    [[ -n "${sel_arr}" ]] && final_selection+=(${sel_arr})
    [[ -n "${sel_tools}" ]] && final_selection+=(${sel_tools})
    
    # Build summary
    local summary="Media Server: ${media_server:-<none>}
Download Client: ${downloader:-<none>}

ARR Apps: ${sel_arr:-<none>}
Tools: ${sel_tools:-<none>}"
    
    if d_yesno "Confirm Deployment" "Review your selections:

${summary}

Proceed?"; then
        log INFO "Media wizard: deploying ${#final_selection[@]} apps"
        deploy_apps "${final_selection[@]}"
    fi
}

deploy_quick_preset() {
    local preset
    preset=$(d_menu "Quick Deploy Preset" "Choose a preset stack:" \
        "1" "Minimal — Jellyfin + qBit + basic ARR" \
        "2" "ARR Only — all ARR apps + downloader" \
        "3" "Media + ARR — full media stack" \
        "4" "Full Stack — everything (no VPN)" \
        "5" "Monitoring — Grafana, Prometheus, etc." \
        "6" "Back") || return

    local selected=()
    case "${preset}" in
        1) selected=("${MINIMAL_STACK[@]}") ;;
        2) selected=("${ARR_ONLY_STACK[@]}") ;;
        3) selected=("${MEDIA_ARR_STACK[@]}") ;;
        4)
            selected=("${FULL_STACK_ARR[@]}" "${FULL_STACK_TOOLS[@]}")
            selected+=(jellyfin qbittorrent)
            ;;
        5) selected=("${FULL_STACK_MONITORING[@]}") ;;
        6) return ;;
    esac

    log INFO "Quick preset ${preset}: ${selected[*]}"
    deploy_apps "${selected[@]}"
}

deploy_by_category() {
    local cat
    cat=$(d_menu "Deploy by Category" "Select a category:" \
        "Downloaders"      "Torrent & Usenet download clients" \
        "ARR Suite"        "Radarr, Sonarr, Prowlarr, and more" \
        "Media Servers"    "Jellyfin, Plex, Emby, etc." \
        "Media Tools"      "Tdarr, Handbrake, Kometa, etc." \
        "Request & Tools"  "Seerr, Tautulli, Ombi, etc." \
        "Monitoring"       "Grafana, Prometheus, Uptime Kuma, etc." \
        "Dashboards"       "Homer, Homarr, Flame, etc." \
        "Reverse Proxies"  "Traefik, NPM, Caddy, SWAG" \
        "VPN & Network"    "WireGuard, Tailscale, Gluetun, etc." \
        "Automation"       "n8n, Huginn, Node-RED, etc." \
        "File & Cloud"     "Nextcloud, Immich, Syncthing, etc." \
        "Security"         "Vaultwarden, Authentik, Authelia, etc." \
        "Communication"    "ntfy, Gotify, Matrix, etc." \
        "Development"      "Gitea, Code-Server, Drone" \
        "Databases"        "MariaDB, PostgreSQL, Redis, MongoDB") || return

    local items=()
    for id in "${ALL_APPS[@]}"; do
        [[ "${APP_CAT[$id]}" == "${cat}" ]] && items+=("${id}" "${APP_NAME[$id]}" "off")
    done
    [[ ${#items[@]} -eq 0 ]] && { d_msgbox "Empty" "No apps in: ${cat}"; return; }

    local sel
    sel=$(d_checklist "Select Apps — ${cat}" \
        "Space = toggle   Enter = confirm   (arrow up for search):" "${items[@]}") || return
    # Strip literal quote characters dialog adds around each selected item
    sel=$(printf '%s' "${sel}" | tr -d '"')
    # shellcheck disable=SC2086
    [[ -n "${sel}" ]] && deploy_apps ${sel} || d_msgbox "Nothing Selected" "No applications were selected."
}

deploy_search() {
    local search_term
    search_term=$(d_inputbox "Search Apps" "Enter app name, image, or category to search:\n\n(e.g. 'sonarr', 'downloader', 'media')" "") || return
    [[ -z "${search_term}" ]] && return

    local items=()
    local st_lower="${search_term,,}"  # lowercase

    for id in "${ALL_APPS[@]}"; do
        local name_lower="${APP_NAME[$id],,}"
        local cat_lower="${APP_CAT[$id],,}"
        local img_lower="${APP_IMAGE[$id],,}"
        if [[ "${name_lower}" =~ ${st_lower} || "${cat_lower}" =~ ${st_lower} || "${img_lower}" =~ ${st_lower} || "${id,,}" =~ ${st_lower} ]]; then
            items+=("${id}" "[${APP_CAT[$id]}] ${APP_NAME[$id]}" "off")
        fi
    done

    if [[ ${#items[@]} -eq 0 ]]; then
        d_msgbox "No Results" "No apps found matching: '${search_term}'\n\nTry searching by category name or partial app name."
        return
    fi

    local match_count=$(( ${#items[@]} / 3 ))
    local sel
    sel=$(d_checklist "Search Results: '${search_term}' (${match_count} found)" \
        "Space = toggle   Enter = confirm:" "${items[@]}") || return
    sel=$(printf '%s' "${sel}" | tr -d '"')
    [[ -n "${sel}" ]] && deploy_apps ${sel} || d_msgbox "Nothing Selected" "No applications were selected."
}

deploy_individual() {
    local items=()
    for id in "${ALL_APPS[@]}"; do
        items+=("${id}" "[${APP_CAT[$id]}] ${APP_NAME[$id]}" "off")
    done

    local sel
    sel=$(d_checklist "Select Apps (110+ available)" \
        "Space = toggle   Enter = confirm   (use arrow up/down to navigate):" "${items[@]}") || return
    # Strip literal quote characters dialog adds around each selected item
    sel=$(printf '%s' "${sel}" | tr -d '"')
    # shellcheck disable=SC2086
    [[ -n "${sel}" ]] && deploy_apps ${sel} || d_msgbox "Nothing Selected" "No applications were selected."
}

deploy_menu() {
    while true; do
        local choice
        choice=$(d_menu "Deploy Stack" "Choose deployment method:" \
            "1" "Media Server Wizard — Step-by-step guided setup (RECOMMENDED)" \
            "2" "Quick Presets       — All-in-one preset stacks" \
            "3" "By Category         — Browse by app category" \
            "4" "Individual Apps     — Cherry-pick from 110+ apps" \
            "5" "Search Apps         — Search by name or category" \
            "6" "Back") || return

        case "${choice}" in
            1) deploy_media_wizard ;;
            2) deploy_quick_preset ;;
            3) deploy_by_category ;;
            4) deploy_individual ;;
            5) deploy_search ;;
            6) return ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
dashboard_menu() {
    while true; do
        local ps_out
        # List all running/stopped containers (per-app compose + any legacy stack)
        if ! ps_out=$(docker ps -a --format '{{.Names}}\t{{.Status}}' 2>/dev/null); then
            d_msgbox "Docker Error" "Could not list containers. Is Docker running?"
            return
        fi
        [[ -z "${ps_out}" ]] && { d_msgbox "No Containers" "No containers found. Deploy apps first."; return; }

        local items=()
        while IFS=$'\t' read -r name status; do
            [[ -z "${name}" ]] && continue
            items+=("${name}" "${status}")
        done <<< "${ps_out}"

        local selected
        selected=$(d_menu "Container Dashboard" "Select a container to manage:" "${items[@]}") || return

        local action
        action=$(d_menu "Manage: ${selected}" "Choose an action:" \
            "status"  "Show detailed status" \
            "start"   "Start container" \
            "stop"    "Stop container" \
            "restart" "Restart container" \
            "logs"    "View recent logs" \
            "remove"  "Remove container (keeps config)") || continue

        local op_out="${TMP_DIR}/op_${selected}.log"
        local op_exit

        case "${action}" in
            status)
                docker inspect "${selected}" > "${op_out}" 2>&1 || true
                d_textbox "Status: ${selected}" "${op_out}"
                ;;

            start)
                _live_header "Starting: ${selected}"
                log INFO "Dashboard start: ${selected}"
                local _cf; _cf="$(app_compose "${selected}")"
                if [[ -f "${_cf}" ]]; then
                    docker compose -f "${_cf}" up -d 2>&1 | tee "${op_out}"
                else
                    docker start "${selected}" 2>&1 | tee "${op_out}"
                fi
                op_exit=${PIPESTATUS[0]}
                log_raw "start:${selected}" "${op_out}"
                if [[ ${op_exit} -eq 0 ]]; then
                    log INFO "Started: ${selected}"
                    printf '\n\033[1;32m✓ %s started.\033[0m\n' "${selected}"
                else
                    log_err "DASHBOARD" "Start failed: ${selected} (exit ${op_exit})"
                    log_raw_err "start:${selected}" "${op_out}"
                    printf '\n\033[1;31m✗ Start failed (exit %d). See Logs > Error Log.\033[0m\n' "${op_exit}"
                fi
                _live_wait_return
                ;;

            stop)
                _live_header "Stopping: ${selected}"
                log INFO "Dashboard stop: ${selected}"
                docker stop "${selected}" 2>&1 | tee "${op_out}"
                op_exit=${PIPESTATUS[0]}
                log_raw "stop:${selected}" "${op_out}"
                if [[ ${op_exit} -eq 0 ]]; then
                    log INFO "Stopped: ${selected}"
                    printf '\n\033[1;32m✓ %s stopped.\033[0m\n' "${selected}"
                else
                    log_err "DASHBOARD" "Stop failed: ${selected} (exit ${op_exit})"
                    log_raw_err "stop:${selected}" "${op_out}"
                    printf '\n\033[1;31m✗ Stop failed (exit %d). See Logs > Error Log.\033[0m\n' "${op_exit}"
                fi
                _live_wait_return
                ;;

            restart)
                _live_header "Restarting: ${selected}"
                log INFO "Dashboard restart: ${selected}"
                docker restart "${selected}" 2>&1 | tee "${op_out}"
                op_exit=${PIPESTATUS[0]}
                log_raw "restart:${selected}" "${op_out}"
                if [[ ${op_exit} -eq 0 ]]; then
                    log INFO "Restarted: ${selected}"
                    printf '\n\033[1;32m✓ %s restarted.\033[0m\n' "${selected}"
                else
                    log_err "DASHBOARD" "Restart failed: ${selected} (exit ${op_exit})"
                    log_raw_err "restart:${selected}" "${op_out}"
                    printf '\n\033[1;31m✗ Restart failed (exit %d). See Logs > Error Log.\033[0m\n' "${op_exit}"
                fi
                _live_wait_return
                ;;

            logs)
                docker logs --tail=150 "${selected}" \
                    > "${op_out}" 2>&1 || true
                d_textbox "Logs: ${selected}" "${op_out}"
                ;;

            remove)
                if d_yesno "Confirm Remove" \
"Remove '${selected}'?\n\nConfig in ${CONFIG_DIR}/${selected}\nwill NOT be deleted."; then
                    _live_header "Removing: ${selected}"
                    log INFO "Dashboard remove: ${selected}"
                    docker rm -fs "${selected}" 2>&1 | tee "${op_out}"
                    log INFO "Removed: ${selected}"
                    printf '\n\033[1;32m✓ %s removed.\033[0m\n' "${selected}"
                    _live_wait_return
                fi
                ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Network utilities
# ---------------------------------------------------------------------------
# Combined Utilities & Network Menu
utilities_network_menu() {
    while true; do
        local choice
        choice=$(d_menu "Utilities & Network" "Choose an option:" \
            "1" "Network     — Port checks & connectivity tests" \
            "2" "Utilities   — Cleanup, backup, system info" \
            "3" "Back") || return
        
        case "${choice}" in
            1) network_menu ;;
            2) utilities_menu ;;
            3) return ;;
        esac
    done
}

network_menu() {
    while true; do
        local choice
        choice=$(d_menu "Network Utilities" "Choose:" \
            "1" "Check listening ports" \
            "2" "Test port reachability" \
            "3" "Show server IP addresses" \
            "4" "Show Docker networks" \
            "5" "Back") || return

        local tmp="${TMP_DIR}/net.txt"

        case "${choice}" in
            1)
                { ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null \
                    || echo "ss/netstat not available"; } > "${tmp}"
                d_textbox "Listening Ports" "${tmp}"
                ;;
            2)
                local host port
                host=$(d_inputbox "Test Port" "Hostname or IP:") || continue
                port=$(d_inputbox "Test Port" "Port number:") || continue
                if timeout 3 bash -c "echo >/dev/tcp/${host}/${port}" 2>/dev/null; then
                    d_msgbox "Port Test" "${host}:${port} — OPEN"
                else
                    d_msgbox "Port Test" "${host}:${port} — CLOSED or unreachable"
                fi
                ;;
            3)
                ip addr show | grep -E 'inet ' | awk '{print $2}' > "${tmp}"
                d_textbox "Server IPs" "${tmp}"
                ;;
            4)
                docker network ls > "${tmp}" 2>&1
                d_textbox "Docker Networks" "${tmp}"
                ;;
            5) return ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
utilities_menu() {
    while true; do
        local choice
        choice=$(d_menu "Utilities" "Choose:" \
            "1" "System info & disk usage" \
            "2" "Docker system info" \
            "3" "Backup config directory" \
            "4" "Cleanup — targeted removal" \
            "5" "Nuclear cleanup — choose level" \
            "6" "Back") || return

        local tmp="${TMP_DIR}/util.txt"

        case "${choice}" in
            1) { uname -a; echo; df -h; echo; free -h; } > "${tmp}"; d_textbox "System Info" "${tmp}" ;;
            2) docker info > "${tmp}" 2>&1; d_textbox "Docker Info" "${tmp}" ;;
            3)
                local bk="/root/deployrr_backup_$(date +%Y%m%d_%H%M%S).tar.gz"
                d_infobox "Backup" "Creating backup of ${CONFIG_DIR}..."
                if tar -czf "${bk}" "${CONFIG_DIR}" 2>"${tmp}"; then
                    log INFO "Backup: ${bk}"; d_msgbox "Backup Complete" "Saved to:\n${bk}"
                else
                    log_err "BACKUP" "tar failed: $(cat "${tmp}")"
                    d_textbox "Backup Error" "${tmp}"
                fi
                ;;
            4) cleanup_targeted_menu ;;
            5) cleanup_nuclear_menu ;;
            6) return ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Targeted cleanup
# ---------------------------------------------------------------------------
cleanup_targeted_menu() {
    while true; do
        local choice
        choice=$(d_menu "Targeted Cleanup" \
"Select what to remove.\nAll actions are reversible except volumes." \
            "1" "Stopped containers      — docker container prune" \
            "2" "Unused images           — docker image prune -a" \
            "3" "Unused volumes          — docker volume prune  ⚠ DATA LOSS" \
            "4" "Unused networks         — docker network prune" \
            "5" "Build cache             — docker builder prune -a" \
            "6" "Config files only       — delete ${CONFIG_DIR}/* (keep containers)" \
            "7" "Back") || return

        local tmp="${TMP_DIR}/cleanup.txt"

        case "${choice}" in
            1)
                if d_yesno "Prune Containers" "Remove all stopped containers?"; then
                    docker container prune -f > "${tmp}" 2>&1 || true
                    log INFO "Container prune done"
                    d_msgbox "Done" "Stopped containers removed.\n\n$(cat "${tmp}")"
                fi
                ;;
            2)
                if d_yesno "Prune Images" \
"Remove ALL unused images?\n(images not used by any container)"; then
                    docker image prune -af > "${tmp}" 2>&1 || true
                    log INFO "Image prune done"
                    d_msgbox "Done" "Unused images removed.\n\n$(cat "${tmp}" | tail -3)"
                fi
                ;;
            3)
                if d_yesno "⚠ Prune Volumes" \
"Remove ALL unused volumes?\n\nWARNING: This permanently deletes\npersistent data not attached to a container.\n\nThis CANNOT be undone."; then
                    if d_yesno "Confirm Volume Purge" "Are you sure? Persistent data will be lost."; then
                        docker volume prune -f > "${tmp}" 2>&1 || true
                        log WARN "Volume prune done"
                        d_msgbox "Done" "Unused volumes removed.\n\n$(cat "${tmp}" | tail -3)"
                    fi
                fi
                ;;
            4)
                if d_yesno "Prune Networks" "Remove all unused Docker networks?"; then
                    docker network prune -f > "${tmp}" 2>&1 || true
                    log INFO "Network prune done"
                    d_msgbox "Done" "Unused networks removed.\n\n$(cat "${tmp}")"
                fi
                ;;
            5)
                if d_yesno "Prune Build Cache" "Remove all Docker build cache?"; then
                    docker builder prune -af > "${tmp}" 2>&1 || true
                    log INFO "Build cache prune done"
                    d_msgbox "Done" "Build cache cleared.\n\n$(cat "${tmp}" | tail -3)"
                fi
                ;;
            6)
                if d_yesno "Delete Config Files" \
"Delete all files in:\n  ${CONFIG_DIR}/*\n\nContainers will KEEP running but\nwill lose their configuration data.\n\nThis CANNOT be undone."; then
                    rm -rf "${CONFIG_DIR:?}"/* 2>/dev/null || true
                    log WARN "Config dir wiped: ${CONFIG_DIR}"
                    d_msgbox "Done" "Config directory cleared.\n${CONFIG_DIR}"
                fi
                ;;
            7) return ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Nuclear cleanup
# ---------------------------------------------------------------------------
cleanup_nuclear_menu() {
    local level
    level=$(d_menu "☢ Nuclear Cleanup" \
"Choose destruction level.\nHigher levels include all actions below them." \
        "1" "Level 1 — Stop all containers only" \
        "2" "Level 2 — Stop + remove containers" \
        "3" "Level 3 — + Remove all images" \
        "4" "Level 4 — + Remove volumes & networks" \
        "5" "Level 5 — + Wipe config dir  (FULL RESET)" \
        "6" "Back") || return

    [[ "${level}" == "6" ]] && return

    local desc=""
    case "${level}" in
        1) desc="Stop ALL running containers" ;;
        2) desc="Stop + remove ALL containers" ;;
        3) desc="Stop + remove containers\n  + Remove ALL images" ;;
        4) desc="Stop + remove containers\n  + Remove ALL images\n  + Remove ALL volumes & networks" ;;
        5) desc="Stop + remove containers\n  + Remove ALL images\n  + Remove ALL volumes & networks\n  + Delete ALL config files in ${CONFIG_DIR}" ;;
    esac

    if ! d_yesno "Confirm Level ${level} Cleanup" \
"This will:\n  ${desc}\n\nThis CANNOT be undone."; then
        return
    fi
    if ! d_yesno "Final Confirmation" \
"Level ${level} Nuclear Cleanup.\n\nAre you ABSOLUTELY certain?"; then
        return
    fi

    local tmp="${TMP_DIR}/nuclear.txt"
    _live_header "☢ Nuclear Cleanup — Level ${level}" "Please wait..."

    printf ' \033[1;33m→ Stopping all containers...\033[0m\n'
    docker compose -f "${COMPOSE_FILE}" stop 2>&1 || true
    docker stop $(docker ps -q) 2>/dev/null || true
    printf ' \033[1;32m✓ Containers stopped\033[0m\n'

    if [[ "${level}" -ge 2 ]]; then
        printf ' \033[1;33m→ Removing all containers...\033[0m\n'
        docker compose -f "${COMPOSE_FILE}" down --remove-orphans 2>&1 || true
        docker rm -f $(docker ps -aq) 2>/dev/null || true
        printf ' \033[1;32m✓ Containers removed\033[0m\n'
    fi

    if [[ "${level}" -ge 3 ]]; then
        printf ' \033[1;33m→ Removing all images...\033[0m\n'
        docker image prune -af >> "${tmp}" 2>&1 || true
        printf ' \033[1;32m✓ Images removed\033[0m\n'
    fi

    if [[ "${level}" -ge 4 ]]; then
        printf ' \033[1;33m→ Removing volumes & networks...\033[0m\n'
        docker volume prune -f >> "${tmp}" 2>&1 || true
        docker network prune -f >> "${tmp}" 2>&1 || true
        printf ' \033[1;32m✓ Volumes & networks removed\033[0m\n'
    fi

    if [[ "${level}" -ge 5 ]]; then
        printf ' \033[1;33m→ Wiping config directory...\033[0m\n'
        rm -rf "${CONFIG_DIR:?}"/* 2>/dev/null || true
        printf ' \033[1;32m✓ Config directory wiped\033[0m\n'
    fi

    log WARN "Nuclear cleanup level ${level} performed"
    printf '\n\033[1;32m✓ Level %s cleanup complete.\033[0m\n' "${level}"
    _live_wait_return
}

# ---------------------------------------------------------------------------
# WebUI control
# ---------------------------------------------------------------------------
webui_menu() {
    local webui_dir="${SCRIPT_DIR}/deployrr-webui"
    local container="deployrr_webui"

    while true; do
        local state
        state=$(docker inspect --format '{{.State.Status}}' "${container}" 2>/dev/null || echo "not found")

        local status_line
        if [[ "${state}" == "running" ]]; then
            status_line="Status: \Zb\Z2RUNNING\Zn  — http://$(hostname -I | awk '{print $1}'):9999"
        elif [[ "${state}" == "not found" ]]; then
            status_line="Status: \Zb\Z1NOT INSTALLED\Zn"
        else
            status_line="Status: \Zb\Z3${state^^}\Zn"
        fi

        local choice
        choice=$(dialog --clear --backtitle "${BACKTITLE}" \
            --title "WebUI Control" \
            --colors \
            --menu "${status_line}\n\nManage the Deployrr Monitor web dashboard:" \
            18 68 8 \
            "1" "Start  WebUI" \
            "2" "Stop   WebUI" \
            "3" "Restart WebUI" \
            "4" "Open browser URL" \
            "5" "View WebUI logs" \
            "6" "Rebuild image (after update)" \
            "7" "Back" \
            3>&1 1>&2 2>&3) || return

        local tmp="${TMP_DIR}/webui.txt"

        case "${choice}" in
            1)
                if [[ "${state}" == "not found" ]]; then
                    d_infobox "WebUI" "Building image and starting WebUI..."
                    docker rm -f "${container}" >> "${LOG_FILE}" 2>&1 || true
                    if docker build -q -t deployrr-webui:local "${webui_dir}" >> "${LOG_FILE}" 2>&1; then
                        docker run -d --name "${container}" --restart unless-stopped \
                            -p 9999:9999 \
                            -v /var/run/docker.sock:/var/run/docker.sock \
                            --pid=host \
                            deployrr-webui:local >> "${LOG_FILE}" 2>&1
                        log INFO "WebUI started (fresh install)"
                        d_msgbox "WebUI Started" "Running at:\nhttp://$(hostname -I | awk '{print $1}'):9999"
                    else
                        d_msgbox "Build Failed" "Image build failed.\nCheck: Main Menu > Logs > Error Log"
                    fi
                else
                    docker start "${container}" > "${tmp}" 2>&1 || true
                    log INFO "WebUI started"
                    d_msgbox "WebUI Started" "http://$(hostname -I | awk '{print $1}'):9999"
                fi
                ;;
            2)
                docker stop "${container}" > "${tmp}" 2>&1 || true
                log INFO "WebUI stopped"
                d_msgbox "WebUI Stopped" "The web dashboard has been stopped."
                ;;
            3)
                docker restart "${container}" > "${tmp}" 2>&1 || true
                log INFO "WebUI restarted"
                d_msgbox "WebUI Restarted" "http://$(hostname -I | awk '{print $1}'):9999"
                ;;
            4)
                local url="http://$(hostname -I | awk '{print $1}'):9999"
                d_msgbox "WebUI URL" "Open in your browser:\n\n  ${url}\n\nOr from this machine:\n  curl http://localhost:9999"
                ;;
            5)
                docker logs --tail=60 "${container}" > "${tmp}" 2>&1 || true
                d_textbox "WebUI Logs" "${tmp}"
                ;;
            6)
                if [[ -d "${webui_dir}" ]]; then
                    _live_header "Rebuilding WebUI Image" "This may take a minute..."
                    docker rm -f "${container}" 2>/dev/null || true
                    if docker build -t deployrr-webui:local "${webui_dir}" 2>&1 | tee "${tmp}"; then
                        docker run -d --name "${container}" --restart unless-stopped \
                            -p 9999:9999 \
                            -v /var/run/docker.sock:/var/run/docker.sock \
                            --pid=host \
                            deployrr-webui:local >> "${LOG_FILE}" 2>&1
                        log INFO "WebUI rebuilt and restarted"
                        printf '\n\033[1;32m✓ WebUI rebuilt and running.\033[0m\n'
                    else
                        log_err "WEBUI" "Rebuild failed"
                        printf '\n\033[1;31m✗ Build failed — check logs.\033[0m\n'
                    fi
                    _live_wait_return
                else
                    d_msgbox "Not Found" "WebUI source not found at:\n${webui_dir}"
                fi
                ;;
            7) return ;;
        esac
    done
}

settings_menu() {
    while true; do
        local choice
        choice=$(d_menu "Settings" \
"Current:\n  Media:  ${MEDIA_DIR}\n  Config: ${CONFIG_DIR}\n  TZ=${TZ_VAL}  PUID=${PUID_VAL}  PGID=${PGID_VAL}" \
            "1" "Set media directory" \
            "2" "Set config directory" \
            "3" "Set timezone" \
            "4" "Set PUID / PGID" \
            "5" "Back") || return

        case "${choice}" in
            1)
                local val
                val=$(d_inputbox "Media Directory" "Enter path:" "${MEDIA_DIR}") || continue
                MEDIA_DIR="${val}"
                mkdir -p "${MEDIA_DIR}"/{movies,tv,downloads,music,books} 2>/dev/null || true
                log INFO "MEDIA_DIR=${MEDIA_DIR}"; d_msgbox "Saved" "Media dir:\n${MEDIA_DIR}"
                ;;
            2)
                local val
                val=$(d_inputbox "Config Directory" "Enter path:" "${CONFIG_DIR}") || continue
                CONFIG_DIR="${val}"; COMPOSE_FILE="${CONFIG_DIR}/docker-compose.yml"

# Per-app compose — each app gets its own /docker/<appname>/docker-compose.yml
# Use: app_compose <id>   to get the path
# (COMPOSE_FILE kept as backwards-compat alias for dashboard ops on existing stacks)
app_compose() { echo "${CONFIG_DIR}/${1}/docker-compose.yml"; }
                mkdir -p "${CONFIG_DIR}" 2>/dev/null || true
                log INFO "CONFIG_DIR=${CONFIG_DIR}"; d_msgbox "Saved" "Config dir:\n${CONFIG_DIR}"
                ;;
            3)
                local val
                val=$(d_inputbox "Timezone" "e.g. Europe/London or America/New_York:" "${TZ_VAL}") || continue
                TZ_VAL="${val}"; log INFO "TZ_VAL=${TZ_VAL}"; d_msgbox "Saved" "TZ: ${TZ_VAL}"
                ;;
            4)
                local puid pgid
                puid=$(d_inputbox "PUID" "Enter PUID (0 = root):" "${PUID_VAL}") || continue
                pgid=$(d_inputbox "PGID" "Enter PGID (0 = root):" "${PGID_VAL}") || continue
                PUID_VAL="${puid}"; PGID_VAL="${pgid}"
                log INFO "PUID=${PUID_VAL} PGID=${PGID_VAL}"
                d_msgbox "Saved" "PUID=${PUID_VAL}  PGID=${PGID_VAL}"
                ;;
            5) return ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Log viewer
# ---------------------------------------------------------------------------
show_logs_menu() {
    while true; do
        local choice
        choice=$(d_menu "Logs" "Choose which log to view:" \
            "1" "Error log — pull failures, crashes, deploy report" \
            "2" "Full log  — every action timestamped" \
            "3" "Back") || return

        case "${choice}" in
            1)
                if [[ -s "${ERR_FILE}" ]]; then
                    d_textbox "Error Log — ${ERR_FILE}" "${ERR_FILE}"
                else
                    d_msgbox "Error Log" "No errors recorded yet.\n\n${ERR_FILE}"
                fi
                ;;
            2)
                if [[ -s "${LOG_FILE}" ]]; then
                    d_textbox "Full Log — ${LOG_FILE}" "${LOG_FILE}"
                else
                    d_msgbox "Full Log" "Log is empty.\n\n${LOG_FILE}"
                fi
                ;;
            3) return ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
# Update & Help Menu
update_help_menu() {
    while true; do
        local choice
        choice=$(d_menu "Update & Help" "Deployrr v${VERSION}" \
            "1" "Check for updates    — Pull latest version from GitHub" \
            "2" "Self-update          — Download and apply update" \
            "3" "Help & Documentation — Usage guide" \
            "4" "About                — Version & system info" \
            "5" "Back") || return
        case "${choice}" in
            1) self_update_check ;;
            2) self_update ;;
            3) show_help ;;
            4) about_menu ;;
            5) return ;;
        esac
    done
}

# Check for updates (non-blocking)
self_update_check() {
    d_infobox "Deployrr Update Check" "Checking GitHub for updates...

This may take a moment."
    
    local latest_version
    latest_version=$(curl -fsSL "https://api.github.com/repos/${GITHUB_USER}/${GITHUB_REPO}/releases/latest" 2>/dev/null | grep -o '"tag_name":"[^"]*"' | cut -d'"' -f4 || echo "")
    
    if [[ -z "${latest_version}" ]]; then
        d_msgbox "Update Check" "Could not reach GitHub to check for updates.

Check your internet connection."
        return
    fi
    
    if [[ "${latest_version}" == "v${VERSION}" ]] || [[ "${latest_version}" == "${VERSION}" ]]; then
        d_msgbox "Up to Date" "You are running the latest version: ${VERSION}"
    else
        d_msgbox "Update Available" "A newer version is available: ${latest_version}

Current: v${VERSION}

Select 'Self-update' to download and apply."
    fi
}

show_help() {
    d_msgbox "Help — Deployrr v${VERSION}" \
"DEPLOYMENT GUIDE

Quick Preset: Fast setup with pre-configured stacks
  • Minimal: Jellyfin + qBit + core ARR
  • ARR Only: All ARR apps + downloader
  • Media + ARR: Full media stack
  • Full Stack: Everything except VPN
  • Monitoring: Grafana, Prometheus, etc.

By Category: Select from 15+ categories
By Apps: Hand-pick from 110+ apps

IMPORTANT — DO NOT press Ctrl+C during pulls
  Interrupts downloads. Wait for completion.

DASHBOARD: Start, stop, restart, logs, remove
LOGS: Check Error Log first if something fails
NETWORK: Test ports, check IPs
UTILITIES: Backup, cleanup, system info
SETTINGS: Configure paths, TZ, PUID/PGID
WEBUI: Deployrr Monitor dashboard (:9999)
ABOUT: System & version information

App Categories:
  Downloaders (8)  ARR Suite (15)  Media Servers (7)
  Media Tools (6)  Request & Tools (6)  Monitoring (10)
  Dashboards (6)  Reverse Proxies (4)  VPN & Network (7)
  Automation (5)  File & Cloud (7)  Security (4)
  Communication (3)  Development (3)  Databases (4)
  Home & Misc (8)  Deployrr (1)

Total: 110+ container images"
}

# ---------------------------------------------------------------------------
# Tailscale Menu
# ---------------------------------------------------------------------------
# Tailscale Set Menu - Preferences
tailscale_set_menu() {
    local ts_cmd=""
    if command -v tailscale &>/dev/null; then
        ts_cmd="tailscale"
    elif docker inspect tailscale &>/dev/null 2>&1; then
        ts_cmd="docker exec tailscale tailscale"
    else
        return
    fi

    while true; do
        local set_choice
        set_choice=$(d_menu "tailscale set — Preferences" \
"Configure persistent Tailscale preferences.\nThese survive reconnects unlike 'tailscale up' flags." \
            "1"  "--accept-dns          Toggle: accept DNS from tailnet" \
            "2"  "--accept-routes       Toggle: accept advertised routes" \
            "3"  "--advertise-exit-node Toggle: offer as exit node" \
            "4"  "--advertise-routes    Set subnet routes to advertise" \
            "5"  "--exit-node           Set exit node to use" \
            "6"  "--exit-node-allow-lan Toggle: allow LAN when using exit node" \
            "7"  "--hostname            Set custom tailnet hostname" \
            "8"  "--operator            Set local operator user (no-sudo)" \
            "9"  "--shields-up          Toggle: block all incoming" \
            "10" "--ssh                 Toggle: enable Tailscale SSH server" \
            "11" "--auto-update         Toggle: automatic updates" \
            "12" "Show current prefs    Display all current settings" \
            "13" "Back") || return

        local tmp="${TMP_DIR}/ts_set.txt"

        case "${set_choice}" in
            1)
                local val
                val=$(d_menu "accept-dns" "Accept DNS pushed by coordinator?" \
                    "true" "Enable — Use tailnet DNS" \
                    "false" "Disable — Use local DNS only") || continue
                ${ts_cmd} set --accept-dns="${val}" 2>&1 > "${tmp}" || true
                d_msgbox "Done" "accept-dns set to: ${val}"
                ;;
            2)
                local val
                val=$(d_menu "accept-routes" "Accept routes advertised by peers?" \
                    "true" "Enable — Accept subnet routes" \
                    "false" "Disable — Ignore subnet routes") || continue
                ${ts_cmd} set --accept-routes="${val}" 2>&1 > "${tmp}" || true
                d_msgbox "Done" "accept-routes set to: ${val}"
                ;;
            3)
                local val
                val=$(d_menu "advertise-exit-node" "Offer this machine as an exit node?" \
                    "true" "Enable — Advertise as exit node" \
                    "false" "Disable — Stop advertising") || continue
                ${ts_cmd} set --advertise-exit-node="${val}" 2>&1 > "${tmp}" || true
                d_msgbox "Done" "advertise-exit-node: ${val}"
                ;;
            4)
                local routes
                routes=$(d_inputbox "advertise-routes" \
"Enter comma-separated CIDR ranges to advertise.\n\nExamples:\n  192.168.1.0/24\n  10.0.0.0/8,192.168.1.0/24\n\nLeave empty to clear all routes." "") || continue
                ${ts_cmd} set --advertise-routes="${routes}" 2>&1 > "${tmp}" || true
                d_msgbox "Done" "Routes set to: ${routes:-<cleared>}"
                ;;
            5)
                local node
                node=$(d_inputbox "exit-node" \
"Enter exit node IP or hostname.\nLeave empty to clear (stop using exit node).\n\nExample: 100.64.x.x or hostname" "") || continue
                ${ts_cmd} set --exit-node="${node}" 2>&1 > "${tmp}" || true
                d_msgbox "Done" "Exit node: ${node:-<cleared>}"
                ;;
            6)
                local val
                val=$(d_menu "exit-node-allow-lan-access" "Allow direct LAN access while using exit node?" \
                    "true" "Enable — Can still reach local network" \
                    "false" "Disable — All traffic via exit node") || continue
                ${ts_cmd} set --exit-node-allow-lan-access="${val}" 2>&1 > "${tmp}" || true
                d_msgbox "Done" "exit-node-allow-lan-access: ${val}"
                ;;
            7)
                local hn
                hn=$(d_inputbox "hostname" "Custom tailnet hostname (leave blank = use system hostname):" "$(hostname -s)") || continue
                ${ts_cmd} set --hostname="${hn}" 2>&1 > "${tmp}" || true
                d_msgbox "Done" "Hostname set to: ${hn}"
                ;;
            8)
                local user
                user=$(d_inputbox "operator" \
"Unix username allowed to run tailscale without sudo.\nLeave blank to clear operator.\n\nExample: ubuntu, pi, your-username" "") || continue
                ${ts_cmd} set --operator="${user}" 2>&1 > "${tmp}" || true
                d_msgbox "Done" "Operator: ${user:-<cleared>}"
                ;;
            9)
                local val
                val=$(d_menu "shields-up" "Block all incoming connections?" \
                    "true" "Enable — Block all incoming (shields up)" \
                    "false" "Disable — Allow incoming connections") || continue
                ${ts_cmd} set --shields-up="${val}" 2>&1 > "${tmp}" || true
                d_msgbox "Done" "shields-up: ${val}"
                ;;
            10)
                local val
                val=$(d_menu "ssh" "Enable Tailscale SSH server?" \
                    "true" "Enable — Allow SSH via Tailscale" \
                    "false" "Disable — Use system SSH only") || continue
                ${ts_cmd} set --ssh="${val}" 2>&1 > "${tmp}" || true
                d_msgbox "Done" "SSH server: ${val}"
                ;;
            11)
                local val
                val=$(d_menu "auto-update" "Enable automatic Tailscale updates?" \
                    "true" "Enable — Auto-update when available" \
                    "false" "Disable — Manual updates only") || continue
                ${ts_cmd} set --auto-update="${val}" 2>&1 > "${tmp}" || true
                d_msgbox "Done" "auto-update: ${val}"
                ;;
            12)
                ${ts_cmd} debug prefs 2>&1 > "${tmp}" || \
                ${ts_cmd} status --peers=false 2>&1 > "${tmp}" || true
                d_textbox "Current Tailscale Preferences" "${tmp}"
                ;;
            13) return ;;
        esac
    done
}

tailscale_lxc_install() {
    # Install Tailscale inside a Proxmox LXC container (inspired by ProxMenuX)
    # Handles TUN device passthrough, cgroup permissions, and in-container install.

    # Verify we are on a Proxmox host
    if ! command -v pct &>/dev/null; then
        d_msgbox "Not Proxmox" \
"This feature requires a Proxmox host.\n\n'pct' command not found.\n\nIf you are inside an LXC already, install Tailscale directly:\n  curl -fsSL https://tailscale.com/install.sh | sh"
        return
    fi

    # List running LXC containers for the user to choose from
    local ct_list
    ct_list=$(pct list 2>/dev/null | awk 'NR>1 {printf "%s \"%s (%s)\" off ", $1, $3, $2}')
    if [[ -z "${ct_list}" ]]; then
        d_msgbox "No LXC Containers" "No LXC containers found on this Proxmox host."
        return
    fi

    local ctid
    # Build array for d_menu from ct_list string
    local ct_arr=()
    while IFS= read -r line; do
        ct_arr+=($line)
    done < <(pct list 2>/dev/null | awk 'NR>1 {print $1, $3" ("$2")"}')

    local menu_items=()
    local i=1
    while [[ $i -lt ${#ct_arr[@]} ]]; do
        menu_items+=("${ct_arr[$i-1+0]}" "${ct_arr[$i-1+1]} ${ct_arr[$i-1+2]}")
        i=$((i+3))
    done
    # Rebuild properly: pct list gives VMID STATUS NAME
    menu_items=()
    while read -r vmid status name; do
        menu_items+=("${vmid}" "${name} [${status}]")
    done < <(pct list 2>/dev/null | awk 'NR>1 {print $1, $2, $3}')

    ctid=$(d_menu "Select LXC Container" \
"Choose which LXC container to install Tailscale into:" \
        "${menu_items[@]}") || return

    # Get LXC config path
    local ct_conf="/etc/pve/lxc/${ctid}.conf"
    if [[ ! -f "${ct_conf}" ]]; then
        d_msgbox "Error" "LXC config not found: ${ct_conf}"
        return
    fi

    # Check if already has TUN config
    local needs_tun=true
    if grep -q "lxc.cgroup2.devices.allow: c 10:200 rwm" "${ct_conf}" 2>/dev/null; then
        needs_tun=false
    fi

    # Confirm plan
    local plan_msg
    if ${needs_tun}; then
        plan_msg="Container: ${ctid}\nConfig: ${ct_conf}\n\nThis will:\n  1. Add TUN device passthrough to LXC config\n  2. Enable nesting + keyctl features\n  3. Restart the container\n  4. Install Tailscale inside it\n  5. Start Tailscale (auth key optional)\n\nProceed?"
    else
        plan_msg="Container: ${ctid}\n\nTUN device already configured.\n\nThis will:\n  1. Install Tailscale inside the container\n  2. Start Tailscale (auth key optional)\n\nProceed?"
    fi

    d_yesno "Install Tailscale in LXC ${ctid}" "${plan_msg}" || return

    _live_header "Tailscale → Proxmox LXC ${ctid}" "Inspired by ProxMenuX — github.com/tteck/Proxmox"

    local tmp="${TMP_DIR}/ts_lxc.txt"
    local ok=true

    # Step 1: Patch LXC config for TUN device access
    if ${needs_tun}; then
        printf '\033[1;33m[1/5]\033[0m Patching LXC config for TUN device access...\n'
        {
            printf '\n# Tailscale TUN device — added by Deployrr\n'
            printf 'lxc.cgroup2.devices.allow: c 10:200 rwm\n'
            printf 'lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file\n'
        } >> "${ct_conf}" 2>"${tmp}" || { printf '\033[1;31mFAILED\033[0m — could not write to %s\n' "${ct_conf}"; ok=false; }

        if ${ok}; then
            printf '\033[1;33m[2/5]\033[0m Enabling nesting + keyctl features...\n'
            pct set "${ctid}" -features "keyctl=1,nesting=1" 2>"${tmp}" || {
                printf '\033[1;31mWARN\033[0m — could not set features (may already be set)\n'
            }

            printf '\033[1;33m[3/5]\033[0m Restarting LXC container %s...\n' "${ctid}"
            pct restart "${ctid}" 2>&1 | tee -a "${tmp}" || {
                printf '\033[1;31mFAILED\033[0m — could not restart container\n'; ok=false
            }
            sleep 3
        fi
    else
        printf '\033[0;32m[1-3/5]\033[0m TUN already configured — skipping config patch.\n'
    fi

    # Step 4: Install Tailscale inside the container
    if ${ok}; then
        printf '\033[1;33m[4/5]\033[0m Installing Tailscale inside LXC %s...\n' "${ctid}"
        pct exec "${ctid}" -- bash -c \
            "curl -fsSL https://tailscale.com/install.sh | sh" 2>&1 | tee -a "${tmp}" || {
            printf '\033[1;31mFAILED\033[0m — Tailscale install script failed\n'; ok=false
        }
    fi

    # Step 5: Start Tailscale (optionally with an auth key)
    if ${ok}; then
        local authkey
        authkey=$(d_inputbox "Tailscale Auth Key (optional)" \
"Enter your Tailscale auth key to automatically authenticate.\nGet one at: https://login.tailscale.com/admin/settings/keys\n\nLeave blank to start without authenticating (you will see a URL to visit)." "") || authkey=""

        printf '\033[1;33m[5/5]\033[0m Starting Tailscale in LXC %s...\n' "${ctid}"
        if [[ -n "${authkey}" ]]; then
            pct exec "${ctid}" -- tailscale up --authkey="${authkey}" --accept-routes 2>&1 | tee -a "${tmp}" || true
        else
            pct exec "${ctid}" -- tailscale up --accept-routes 2>&1 | tee -a "${tmp}" || true
        fi

        # Show IP
        printf '\n\033[1;32mTailscale IP in container %s:\033[0m\n' "${ctid}"
        pct exec "${ctid}" -- tailscale ip 2>&1 | tee -a "${tmp}" || true
    fi

    if ${ok}; then
        printf '\n\033[1;32m✓ Tailscale installed successfully in LXC %s\033[0m\n' "${ctid}"
        log INFO "Tailscale LXC install: ctid=${ctid} ok"
    else
        printf '\n\033[1;31m✗ Installation encountered errors — check output above\033[0m\n'
        log_err "tailscale_lxc" "Install failed for ctid=${ctid}"
    fi

    _live_wait_return
}

tailscale_menu() {
    # Detect where tailscale is: native binary or in a container
    local ts_cmd=""
    if command -v tailscale &>/dev/null; then
        ts_cmd="tailscale"
    elif docker inspect tailscale &>/dev/null 2>&1; then
        ts_cmd="docker exec tailscale tailscale"
    fi

    # Show install options if tailscale not found (but keep LXC option available)
    if [[ -z "${ts_cmd}" ]]; then
        local install_choice
        install_choice=$(d_menu "Tailscale Not Found" \
"Tailscale is not installed or not running." \
            "1" "Install in Proxmox LXC   — guided TUN + install (Proxmox hosts only)" \
            "2" "Install natively          — run official install script on this host" \
            "3" "Deploy as Docker container — via Deploy menu" \
            "4" "Back") || return
        case "${install_choice}" in
            1) tailscale_lxc_install; return ;;
            2)
                _live_header "Install Tailscale — Native"
                curl -fsSL https://tailscale.com/install.sh | sh
                # Ensure tailscaled daemon is running after install (needed in LXC / non-systemd hosts)
                if ! pgrep -x tailscaled &>/dev/null; then
                    printf '\033[1;33m  Starting tailscaled daemon...\033[0m\n'
                    if command -v systemctl &>/dev/null && systemctl start tailscaled 2>/dev/null; then
                        printf '\033[1;32m  tailscaled started via systemctl\033[0m\n'
                    else
                        mkdir -p /var/lib/tailscale /run/tailscale
                        tailscaled --state=/var/lib/tailscale/tailscaled.state \
                            --socket=/run/tailscale/tailscaled.sock &>/dev/null &
                        sleep 3
                        printf '\033[1;32m  tailscaled started in background\033[0m\n'
                    fi
                fi
                printf '\033[1;32m  Tailscale installed. Use menu option 2 (Connect) to authenticate.\033[0m\n'
                _live_wait_return
                return
                ;;
            3)
                d_msgbox "Deploy Tailscale" "Go to: Main Menu → Deploy → Search → 'tailscale'"
                return
                ;;
            4) return ;;
        esac
        return
    fi

    while true; do
        # Get current status for the menu header
        local ts_status_short
        ts_status_short=$(${ts_cmd} status --peers=false 2>/dev/null | head -3 | tr '\n' ' ' || echo "Unknown")

        local choice
        choice=$(d_menu "Tailscale Manager" \
"${ts_status_short:0:60}..." \
            "1"  "Status          — Show all peers and IPs" \
            "2"  "Connect (up)    — Connect to Tailscale network" \
            "3"  "Disconnect      — Disconnect from network" \
            "4"  "My IP           — Show this device's Tailscale IP" \
            "5"  "Ping peer       — Ping a node by name or IP" \
            "6"  "SSH into peer   — SSH to another Tailscale device" \
            "7"  "Net check       — Check connectivity conditions" \
            "8"  "Serve           — Share a local port within tailnet" \
            "9"  "Funnel          — Expose a port to the internet" \
            "10" "Advertise route — Share a subnet to tailnet" \
            "11" "Exit node       — Use/set an exit node" \
            "12" "Whois           — Identify a peer by IP" \
            "13" "Set preferences — Shields-up, hostname, etc." \
            "14" "DNS status      — Show DNS configuration" \
            "15" "Bug report      — Generate support report" \
            "16" "Proxmox LXC     — Install Tailscale in an LXC container" \
            "17" "Back") || return

        local tmp="${TMP_DIR}/ts_out.txt"

        case "${choice}" in
            1)
                ${ts_cmd} status 2>&1 > "${tmp}" || true
                d_textbox "Tailscale Status" "${tmp}"
                ;;
            2)
                _live_header "Tailscale — Connecting"
                local flags
                flags=$(d_inputbox "tailscale up flags" \
"Optional flags (leave blank for defaults):\n\nExamples:\n  --accept-routes\n  --advertise-exit-node\n  --shields-up\n  --exit-node=<ip>" "") || { _live_wait_return; continue; }
                # Ensure tailscaled daemon is running (needed in LXC containers)
                if ! systemctl is-active --quiet tailscaled 2>/dev/null; then
                    printf '[1;33m  tailscaled not running — attempting to start...[0m
'
                    if systemctl start tailscaled 2>/dev/null; then
                        printf '[1;32m  tailscaled started via systemctl[0m
'
                        sleep 2
                    elif ! pgrep -x tailscaled &>/dev/null; then
                        printf '[1;33m  Starting tailscaled in background...[0m
'
                        tailscaled --state=/var/lib/tailscale/tailscaled.state \
                            --socket=/run/tailscale/tailscaled.sock &>/dev/null &
                        sleep 3
                    fi
                fi
                ${ts_cmd} up ${flags} 2>&1 | tee "${tmp}" || true
                log INFO "Tailscale up: ${flags}"
                _live_wait_return
                ;;
            3)
                if d_yesno "Tailscale Disconnect" "Disconnect from Tailscale network?\nYou will lose access to tailnet peers."; then
                    ${ts_cmd} down 2>&1 | tee "${tmp}" || true
                    log INFO "Tailscale down"
                fi
                ;;
            4)
                ${ts_cmd} ip 2>&1 > "${tmp}" || true
                d_textbox "Tailscale IP" "${tmp}"
                ;;
            5)
                local peer
                peer=$(d_inputbox "Ping Peer" "Enter peer hostname or Tailscale IP:" "") || continue
                _live_header "Tailscale — Pinging ${peer}"
                ${ts_cmd} ping "${peer}" 2>&1 | tee "${tmp}" || true
                _live_wait_return
                ;;
            6)
                local peer
                peer=$(d_inputbox "SSH to Peer" "Enter peer hostname or Tailscale IP:" "") || continue
                _live_header "Tailscale SSH — ${peer}" "Type 'exit' to return"
                ${ts_cmd%tailscale} ssh "${peer}" || true
                _live_wait_return
                ;;
            7)
                _live_header "Tailscale — Network Check"
                ${ts_cmd} netcheck 2>&1 | tee "${tmp}" || true
                _live_wait_return
                ;;
            8)
                local port
                port=$(d_inputbox "Tailscale Serve" \
"Share a local service within your tailnet.\n\nEnter local port to share (or 'off' to disable):\n\nExample: 8096 (Jellyfin), 32400 (Plex)" "") || continue
                _live_header "Tailscale Serve — Port ${port}"
                ${ts_cmd} serve "${port}" 2>&1 | tee "${tmp}" || true
                _live_wait_return
                ;;
            9)
                local port
                port=$(d_inputbox "Tailscale Funnel" \
"Expose a local service to the ENTIRE INTERNET.\nRequires: Tailscale account with Funnel enabled.\n\nEnter local port to expose (or 'off' to disable):\n\nExample: 8096, 9999" "") || continue
                if d_yesno "Confirm Funnel" "This exposes port ${port} to the PUBLIC internet.\nOnly proceed if intentional."; then
                    _live_header "Tailscale Funnel — Port ${port}"
                    ${ts_cmd} funnel "${port}" 2>&1 | tee "${tmp}" || true
                    _live_wait_return
                fi
                ;;
            10)
                local routes
                routes=$(d_inputbox "Advertise Routes" \
"Advertise subnets to your tailnet.\n\nEnter subnet(s) comma-separated:\n\nExample: 192.168.1.0/24\nExample: 10.0.0.0/8,192.168.0.0/16" "") || continue
                _live_header "Tailscale — Advertising Routes"
                ${ts_cmd} up --advertise-routes="${routes}" 2>&1 | tee "${tmp}" || true
                _live_wait_return
                ;;
            11)
                local node_action
                node_action=$(d_menu "Exit Node" "Configure exit node:" \
                    "use"      "Use a specific exit node" \
                    "auto"     "Use auto-selected exit node" \
                    "clear"    "Stop using exit node" \
                    "advertise" "Advertise THIS machine as exit node") || continue
                case "${node_action}" in
                    use)
                        local node
                        node=$(d_inputbox "Exit Node" "Enter exit node IP or hostname:" "") || continue
                        _live_header "Tailscale — Setting Exit Node: ${node}"
                        ${ts_cmd} up --exit-node="${node}" 2>&1 | tee "${tmp}" || true
                        ;;
                    auto)
                        _live_header "Tailscale — Auto Exit Node"
                        ${ts_cmd} up --exit-node=auto:any 2>&1 | tee "${tmp}" || true
                        ;;
                    clear)
                        _live_header "Tailscale — Clearing Exit Node"
                        ${ts_cmd} up --exit-node= 2>&1 | tee "${tmp}" || true
                        ;;
                    advertise)
                        _live_header "Tailscale — Advertising as Exit Node"
                        ${ts_cmd} up --advertise-exit-node 2>&1 | tee "${tmp}" || true
                        ;;
                esac
                _live_wait_return
                ;;
            12)
                local ip
                ip=$(d_inputbox "Whois" "Enter Tailscale IP to identify:" "") || continue
                ${ts_cmd} whois "${ip}" 2>&1 > "${tmp}" || true
                d_textbox "Whois: ${ip}" "${tmp}"
                ;;
            13)
                tailscale_set_menu
                ;;
            14)
                ${ts_cmd} status --peers=false 2>&1 > "${tmp}" || true
                ${ts_cmd} dns status 2>&1 >> "${tmp}" || true
                d_textbox "Tailscale DNS Status" "${tmp}"
                ;;
            15)
                local report
                report=$(${ts_cmd} bugreport 2>&1 || echo "Error generating report")
                d_msgbox "Bug Report" "Bug report ID:\n${report}\n\nShare this with Tailscale support."
                ;;
            16) tailscale_lxc_install ;;
            17) return ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Uninstall Deployrr
# ---------------------------------------------------------------------------
uninstall_deployrr() {
    if ! d_yesno "Uninstall Deployrr" \
"This will remove:

  • All Deployrr Docker containers
  • Deployrr Docker images
  • /opt/deployrr/ (install dir)
  • /usr/local/bin/media (alias)

Your app configs in ${CONFIG_DIR} will NOT be deleted.

Proceed with uninstall?"; then
        return
    fi

    _live_header "Uninstalling Deployrr" "Stopping and removing containers..."

    # Stop and remove all containers managed by Deployrr
    local container_ids
    container_ids=$(docker ps -aq --filter "label=com.docker.compose.project" 2>/dev/null || true)
    if [[ -n "${container_ids}" ]]; then
        printf '[1;33m  Stopping all Deployrr-managed containers...[0m
'
        for compose_f in "${CONFIG_DIR}"/*/docker-compose.yml; do
            [[ -f "${compose_f}" ]] || continue
            docker compose -f "${compose_f}" down --remove-orphans 2>/dev/null || true
        done
        [[ -f "${COMPOSE_FILE}" ]] && docker compose -f "${COMPOSE_FILE}" down --remove-orphans 2>/dev/null || true
    fi

    printf '[1;33m  Removing Deployrr WebUI image...[0m
'
    docker image rm deployrr-webui:local 2>/dev/null || true
    docker image rm "ghcr.io/${GITHUB_USER}/${GITHUB_REPO}:latest" 2>/dev/null || true

    printf '[1;33m  Removing /opt/deployrr/...[0m
'
    rm -rf /opt/deployrr 2>/dev/null || true

    printf '[1;33m  Removing /usr/local/bin/media...[0m
'
    rm -f /usr/local/bin/media 2>/dev/null || true

    rm -f "${LOG_FILE}" "${ERR_FILE}" 2>/dev/null || true

    log INFO "Deployrr uninstalled"
    printf '
[1;32m✓ Deployrr has been uninstalled successfully.[0m
'
    printf '[0;37m  App configs in %s were NOT deleted.[0m
' "${CONFIG_DIR}"
    printf '[0;37m  To remove app configs:  rm -rf %s[0m

' "${CONFIG_DIR}"
    _live_wait_return
    clear
    exit 0
}

# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------
main_menu() {
    while true; do
        local container_count
        container_count=$(docker ps --quiet 2>/dev/null | wc -l || echo "0")

        local choice
        choice=$(d_menu "Main Menu — Deployrr v${VERSION}" \
"Media: ${MEDIA_DIR}   Config: ${CONFIG_DIR}   Containers: ${container_count}" \
            "1"  "Deploy          — Install & manage apps (110+)" \
            "2"  "Containers      — Manage running containers" \
            "3"  "WebUI           — Browser dashboard (port 9999)" \
            "4"  "Utilities       — Network, cleanup, backup, system info" \
            "5"  "Settings        — Paths, timezone, PUID/PGID" \
            "6"  "Tailscale       — Mesh VPN management" \
            "7"  "Logs            — Error & activity logs" \
            "8"  "Update/Help     — Update tool, help & about" \
            "10" "Uninstall       — Remove Deployrr completely" \
            "9"  "Exit") || {
            if d_yesno "Exit" "Exit Deployrr?"; then
                log INFO "Exiting"; clear; exit 0
            fi
            continue
        }

        log INFO "Menu: ${choice}"
        case "${choice}" in
            1)  deploy_menu ;;
            2)  dashboard_menu ;;
            3)  webui_menu ;;
            4)  utilities_network_menu ;;
            5)  settings_menu ;;
            6)  tailscale_menu ;;
            7)  show_logs_menu ;;
            8)  update_help_menu ;;
                        10) uninstall_deployrr ;;
            9)
                if d_yesno "Exit" "Exit Deployrr?"; then
                    log INFO "Exiting"; clear; exit 0
                fi
                ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Install media alias
# ---------------------------------------------------------------------------
install_media_alias() {
    local target="/usr/local/bin/media"
    local script_path
    script_path="$(realpath "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"

    if [[ -x "${target}" ]]; then
        return 0
    fi

    {
        printf '#!/bin/bash\n'
        printf '# Deployrr media shortcut — auto-generated\n'
        printf 'exec bash "%s" "$@"\n' "${script_path}"
    } > "${target}" 2>/dev/null && chmod +x "${target}" 2>/dev/null || {
        log_err "ALIAS" "Could not write ${target}"
        return 1
    }
    log INFO "Installed media alias → ${target}"
}

# ---------------------------------------------------------------------------
# Pinchflat service
# ---------------------------------------------------------------------------
add_service_pinchflat() {
    local id="${1:-pinchflat}"
    local f; f="$(app_compose "${id}")"
    {
        echo ""
        echo "  pinchflat:"
        echo "    image: ghcr.io/kieraneglin/pinchflat:latest"
        echo "    container_name: pinchflat"
        echo "    restart: unless-stopped"
        echo "    environment:"
        echo "      - TZ=${TZ_VAL}"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/pinchflat:/config"
        echo "      - ${MEDIA_DIR}/downloads:/downloads"
        echo "    ports:"
        echo "      - "8945:8945""
    } >> "${f}"
    log INFO "Pinchflat configured — YouTube/media downloader on port 8945"
}

# ---------------------------------------------------------------------------
# qbitrr service
# ---------------------------------------------------------------------------
add_service_qbitrr() {
    local id="${1:-qbitrr}"
    local f; f="$(app_compose "${id}")"
    {
        echo ""
        echo "  qbitrr:"
        echo "    image: feramance/qbitrr:latest"
        echo "    container_name: qbitrr"
        echo "    restart: unless-stopped"
        echo "    tty: true"
        echo "    environment:"
        echo "      - TZ=${TZ_VAL}"
        echo "    volumes:"
        echo "      - ${CONFIG_DIR}/qbitrr:/config"
        echo "      - ${MEDIA_DIR}/downloads:/completed_downloads:rw"
        echo "    ports:"
        echo "      - "6969:6969""
    } >> "${f}"
    log INFO "qbitrr configured — qBittorrent/ARR companion on port 6969"
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
main() {
    # Check for update command
    if [[ "${1:-}" == "update" ]]; then
        self_update
        exit 0
    fi

    load_catalog
    init_logs
    check_requirements
    install_media_alias
    ensure_dirs
    log INFO "Entering main menu"
    main_menu
}

main "$@"
