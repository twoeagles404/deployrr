# 🚀 Deployrr

> A dead-simple, fully open-source homelab Docker deployment tool.  
> One `curl | sudo bash` install. Pure Bash TUI + beautiful Flask WebUI. 104+ apps. MIT licensed.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![GitHub Release](https://img.shields.io/github/v/release/twoeagles404/deployrr)](https://github.com/twoeagles404/deployrr/releases)
[![Docker Image](https://img.shields.io/badge/ghcr.io-twoeagles404%2Fdeployrr-blue)](https://github.com/twoeagles404/deployrr/pkgs/container/deployrr)

---

## ✨ Features

- **One-command install** — `curl -fsSL ... | sudo bash`, zero manual steps
- **104+ apps** across 15 categories (ARR Suite, Media Servers, Monitoring, Security, and more)
- **Pure Bash TUI** — run `media` to launch the interactive deployment wizard
- **Flask WebUI** on port `:9999` — real-time SSE dashboard, no polling
- **Deploy tab** — browse the catalog, search apps, one-click deploy
- **Stack Manager** — view your compose file and deployment history
- **Updates tab** — pull latest images and restart containers
- **Backup tab** — one-click config backup/restore
- **Settings tab** — persist config dir, media dir, timezone, PUID/PGID
- **Optional auth** — bearer token with auto-generation, or disable for LAN
- **SQLite persistence** — settings and history stored at `/data/deployrr.db`
- **No PHP, no Node, no NPM** — Python + Bash, fully self-contained
- **MIT licensed** — no paid tiers, fork it, use it, do whatever

---

## 🚀 Install

```bash
curl -fsSL https://raw.githubusercontent.com/twoeagles404/deployrr/main/install.sh | sudo bash
```

The installer:
1. Installs Docker if not present
2. Downloads all Deployrr files to `/opt/deployrr/`
3. Builds and starts the WebUI container on port `9999`
4. Installs the `media` alias to `/usr/local/bin/media`

---

## 🖥️ Usage

### Bash TUI
```bash
media
```
Launches the interactive terminal UI where you can browse categories, deploy apps, manage your stack, and configure settings.

### WebUI
Open `http://your-server-ip:9999` in your browser.

**First launch:** Get your token from container logs:
```bash
docker logs deployrr_webui | grep "DEPLOYRR TOKEN"
```
Enter the token in the login screen.

**Disable auth (LAN-only):**
```bash
docker run -e DEPLOYRR_NO_AUTH=true ...
```

---

## 📦 App Categories

| Category | Apps |
|---|---|
| ARR Suite | Radarr, Sonarr, Lidarr, Bazarr, Prowlarr, Readarr, Whisparr, Huntarr... |
| Downloaders | qBittorrent, Transmission, Deluge, SABnzbd, NZBGet, JDownloader2... |
| Media Servers | Jellyfin, Plex, Emby, Navidrome, Komga, Audiobookshelf, Kavita |
| Request Tools | Jellyseerr, Overseerr, Ombi |
| Dashboards | Homer, Homarr, Homepage, Dashy, Heimdall |
| Monitoring | Uptime Kuma, Netdata, Grafana, Prometheus, Dozzle, Scrutiny |
| Reverse Proxy | Traefik, Nginx Proxy Manager, Caddy, SWAG |
| Auth & Security | Authelia, Authentik, Vaultwarden, CrowdSec |
| Databases | MariaDB, PostgreSQL, Redis, MongoDB |
| Photos | Immich, PhotoPrism, Photoview |
| Notes & Docs | Bookstack, Outline, WikiJS, Joplin |
| Files & Storage | Nextcloud, FileBrowser, Syncthing, MinIO |
| Dev Tools | Gitea, code-server, Portainer, Watchtower, Drone CI |
| Home Automation | Home Assistant, Homebridge, Node-RED, ESPHome |
| VPN & Network | WireGuard, Tailscale, Pi-hole, AdGuard Home |

---

## 🏗️ Architecture

```
deployrr/
├── install.sh          ← One-command installer
├── deployrr.sh         ← Bash TUI (run via `media`)
├── app.py              ← Flask WebUI (all HTML/CSS/JS embedded)
├── Dockerfile          ← WebUI container (python:3.12-slim)
├── apps/
│   ├── catalog.json    ← Master app catalog (104 apps)
│   └── catalog.sh      ← Auto-generated Bash version
├── scripts/
│   └── gen_catalog_sh.py  ← Generates catalog.sh from catalog.json
└── docs/
    └── auth-setup.md   ← Auth + HTTPS setup guide
```

**Adding a new app:** Edit `apps/catalog.json`, then run:
```bash
python3 scripts/gen_catalog_sh.py
```

---

## 🔐 Authentication & HTTPS

See [docs/auth-setup.md](docs/auth-setup.md) for the complete guide covering:
- Custom tokens via environment variable
- Disabling auth for LAN-only use
- Nginx, Caddy, and NPM reverse proxy configs
- SSL/HTTPS setup

---

## 🐳 Docker Run (Manual)

```bash
docker run -d \
  --name deployrr_webui \
  -p 9999:9999 \
  -e DEPLOYRR_TOKEN=your-secret-token \
  -v /opt/deployrr/data:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --pid=host \
  --restart unless-stopped \
  ghcr.io/twoeagles404/deployrr:latest
```

---

## 📡 GitHub Push Instructions

```bash
cd /opt/deployrr
git init
git remote add origin https://github.com/twoeagles404/deployrr.git
git add .
git commit -m "Initial release: Deployrr v3.0.0"
git push -u origin main

# Tag a release (triggers GitHub Actions auto-release + Docker build)
git tag v3.0.0
git push origin v3.0.0
```

---

## 📄 License

MIT — see [LICENSE](LICENSE)

---

*Inspired by SimpleHomelab/Deployrr and PegaProx. Built with ❤️ for the homelab community.*
