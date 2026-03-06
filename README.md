# 👻 ☀️ ArrHub

> A dead-simple, fully open-source homelab Docker deployment tool.
> One `curl | sudo bash` install. Pure Bash TUI + beautiful Flask WebUI. 104+ apps. MIT licensed.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![GitHub Release](https://img.shields.io/github/v/release/twoeagles404/arrhub)](https://github.com/twoeagles404/arrhub/releases)
[![Docker Image](https://img.shields.io/badge/ghcr.io-twoeagles404%2Farrhub-blue)](https://github.com/twoeagles404/arrhub/pkgs/container/arrhub)

---

## Features

- **One-command install** — `curl -fsSL ... | sudo bash`, zero manual steps
- **104+ apps** across 17 categories (ARR Suite, Media Servers, Monitoring, Security, and more)
- **Pure Bash TUI** — run `media` to launch the interactive deployment wizard
- **Flask WebUI** on port `:9999` — real-time SSE dashboard with PegaProx-inspired dark UI
- **Live monitoring** — CPU, RAM, load, network, and storage via Server-Sent Events
- **Container management** — start, stop, restart, remove, view logs, per-container CPU/MEM stats
- **Deploy tab** — browse the full catalog, filter by category, search, one-click deploy
- **Stack Manager** — view per-app compose files and deployment history
- **Updates tab** — check for image updates, pull latest with one click
- **Backup tab** — one-click config backup and restore
- **RSS feeds** — built-in news, tech, sports, and Reddit feeds right in the dashboard
- **Port conflict detection** — checks for port collisions before deploying
- **Settings tab** — persist config dir, media dir, timezone, PUID/PGID
- **No auth required** — designed for trusted LAN use (optional bearer token available)
- **SQLite persistence** — settings and history stored at `/data/arrhub.db`
- **No PHP, no Node, no NPM** — Python + Bash, fully self-contained
- **MIT licensed** — no paid tiers, fork it, use it, do whatever

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/twoeagles404/arrhub/main/install.sh | sudo bash
```
Unstable
```
curl -fsSL https://raw.githubusercontent.com/twoeagles404/arrhub/dev/install.sh | sudo bash
```

The installer downloads all files to `/opt/arrhub/`, pulls the Docker image from `ghcr.io` (or builds locally as fallback), starts the WebUI on port `9999`, and installs the `media` TUI alias.

---

## Usage

### Bash TUI

```bash
media
```

Launches the interactive terminal UI for browsing categories, deploying apps, managing stacks, and configuring settings.

### WebUI

Open `http://your-server-ip:9999` in your browser. No login required by default.

To enable token auth:

```bash
docker run -e ARRHUB_TOKEN=your-secret-token ...
```

---

## App Categories

| Category | Example Apps |
|---|---|
| ARR Suite | Radarr, Sonarr, Lidarr, Bazarr, Prowlarr, Whisparr, Boxarr |
| Downloaders | qBittorrent, Transmission, Deluge, SABnzbd, NZBGet, JDownloader2, Pinchflat, qbitrr |
| Media Servers | Jellyfin, Plex, Emby, Navidrome, Komga, Audiobookshelf, Kavita |
| Request Tools | Jellyseerr, Overseerr, Ombi, Doplarr |
| Dashboards | Homer, Homarr, Homepage, Dashy, Heimdall |
| Monitoring | Uptime Kuma, Netdata, Grafana, Prometheus, Dozzle, Glances, Scrutiny |
| Reverse Proxy | Traefik, Nginx Proxy Manager, Caddy, SWAG |
| Auth & Security | Authelia, Authentik, Vaultwarden, CrowdSec, Fail2ban |
| Databases | MariaDB, PostgreSQL, Redis, Adminer, pgAdmin |
| Photos | Immich, PhotoPrism, Photoview, Lychee, Pigallery2 |
| Notes & Docs | Bookstack, Outline, WikiJS, Joplin Server, Memos |
| Files & Storage | Nextcloud, FileBrowser, Syncthing, Rclone, Seafile |
| Dev Tools | Gitea, Forgejo, code-server, Portainer, Watchtower, Drone CI, Dockge |
| Home Automation | Home Assistant, Homebridge, Node-RED, ESPHome, Mosquitto, Zigbee2MQTT |
| VPN & Network | WireGuard, Tailscale, Pi-hole, AdGuard Home, Unbound, Gluetun |
| Communication | Matrix Synapse, Rocket.Chat |
| Other | Speedtest Tracker, IT-Tools |

---

## Architecture

```
arrhub/
├── install.sh           ← One-command installer
├── arrhub.sh            ← Bash TUI (run via `media`)
├── app.py               ← Flask WebUI (all HTML/CSS/JS embedded)
├── Dockerfile           ← WebUI container (python:3.12-slim)
├── apps/
│   ├── catalog.json     ← Master app catalog (104+ apps)
│   └── catalog.sh       ← Auto-generated Bash version
├── scripts/
│   └── gen_catalog_sh.py
└── docs/
    └── auth-setup.md    ← Auth + HTTPS setup guide
```

Each app deploys to its own compose file at `/docker/<appname>/docker-compose.yml`, keeping stacks isolated and independently manageable.

**Adding a new app:** Edit `apps/catalog.json`, then run:

```bash
python3 scripts/gen_catalog_sh.py
```

---

## Docker Run (Manual)

```bash
docker run -d \
  --name arrhub_webui \
  -p 9999:9999 \
  -v /opt/arrhub/data:/data \
  -v /opt/arrhub/apps:/opt/arrhub/apps \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --pid=host \
  --restart unless-stopped \
  ghcr.io/twoeagles404/arrhub:latest
```

To enable auth, add `-e ARRHUB_TOKEN=your-secret-token`.

---

## Authentication & HTTPS

See [docs/auth-setup.md](docs/auth-setup.md) for Nginx, Caddy, and NPM reverse proxy configs with SSL/HTTPS setup.

---

## License

MIT — see [LICENSE](LICENSE)

---

*Built with care for the homelab community.*
