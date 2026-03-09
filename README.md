# 👻 ArrHub

> A dead-simple, fully open-source homelab Docker deployment tool.
> One `curl | sudo bash` install. Pure Bash TUI + real-time Flask WebUI. **103 apps across 17 categories.** MIT licensed.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-3.5.0-blue)](https://github.com/twoeagles404/arrhub/releases)
[![Docker Image](https://img.shields.io/badge/ghcr.io-twoeagles404%2Farrhub-blue)](https://github.com/twoeagles404/arrhub/pkgs/container/arrhub)

---

## Features

- **One-command install** — `curl -fsSL ... | sudo bash`, zero manual steps
- **103 apps** across 17 categories — ARR Suite, Media Servers, Monitoring, Security, and more
- **Pure Bash TUI** — run `media` to launch the interactive deployment wizard
- **Flask WebUI** on port `:9999` — real-time SSE dashboard with dark UI
- **Live monitoring** — CPU, RAM, load, network, storage via Server-Sent Events every 2s
- **Container management** — start, stop, restart, remove, logs, ⬆ Update & Recreate, per-container CPU/MEM charts
- **Deploy tab** — browse full catalog, filter by category, search, sort, one-click deploy with favorites
- **Stack Manager** — view per-app compose files and deployment history
- **Updates tab** — check for image updates, pull latest with one click
- **Backup tab** — one-click config backup and restore
- **RSS & Live News** — CNN, BBC, Al Jazeera, Sky News, Sports, Tech, Science, YouTube feeds + live iframes
- **Alerts bar** — automatic warnings for down containers and high disk usage
- **Toast notifications** — inline feedback for every action (deploy, stop, restart, update)
- **Mobile responsive** — bottom nav, hamburger sidebar, touch-friendly at any screen size
- **Favorites** — star apps in the catalog; pinned to top of Deploy tab (localStorage)
- **Port conflict detection** — auto-reassigns ports before deploying
- **Settings tab** — persist config dir, media dir, timezone, PUID/PGID
- **Tailscale integration** — install, connect, manage mesh VPN from the TUI
- **No auth required** — designed for trusted LAN use (optional bearer token available)
- **SQLite persistence** — settings and deploy history at `/data/arrhub.db`
- **No PHP, no Node, no NPM** — Python + Bash, fully self-contained
- **MIT licensed** — no paid tiers, fork it, use it, do whatever

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/twoeagles404/arrhub/main/install.sh | sudo bash
```

The installer downloads all files to `/opt/arrhub/`, **builds the WebUI image locally** from the downloaded Dockerfile, starts the container on port `9999`, and installs the `media` TUI alias.

---

## Usage

### Bash TUI

```bash
media
```

Interactive terminal UI for browsing categories, running the Media Server Wizard, deploying apps, managing stacks, configuring Tailscale, and more.

### WebUI

Open `http://your-server-ip:9999` in your browser. No login required by default.

**Enable token auth:**
```bash
docker run -e ARRHUB_TOKEN=your-secret-token ...
```

---

## App Catalog — 103 Apps

### 🎬 ARR Suite (12)
| App | Description |
|-----|-------------|
| **Bazarr** | Companion app to Sonarr and Radarr for automatic subtitle management |
| **Boxarr** | Physical media collection manager — track box sets alongside digital library |
| **Doplarr** | Discord bot for requesting media via Overseerr, Radarr, and Sonarr |
| **Lidarr** | Music collection manager. Automatically downloads music to your library |
| **Mylar3** | Automated comic book downloader for CBZ/CBR files |
| **Notifiarr** | Unified notification and integration hub for your ARR stack |
| **Prowlarr** | Indexer manager and proxy supporting many torrent and Usenet trackers |
| **Radarr** | Movie collection manager. Integrates with download clients and media servers |
| **Recyclarr** | Syncs TRaSH Guide quality profiles and settings to Sonarr and Radarr |
| **Sonarr** | Smart PVR for TV shows. Searches, downloads, and manages your TV library |
| **Unpackerr** | Extracts downloaded archives for Radarr, Sonarr, Lidarr, and Readarr |
| **Whisparr** | Adult content collection manager. Part of the ARR family |

### ⚙️ Automation (5)
| App | Description |
|-----|-------------|
| **Activepieces** | Open-source Zapier alternative with no-code automation flows |
| **Changedetection.io** | Monitor websites for changes and receive notifications |
| **Huginn** | Create agents that monitor the web and act on your behalf |
| **Node-RED** | Flow-based programming for visual wiring of IoT hardware and APIs |
| **n8n** | Workflow automation tool with 350+ integrations. Like Zapier, self-hosted |

### 💬 Communication (3)
| App | Description |
|-----|-------------|
| **Gotify** | Simple server for sending and receiving notification messages |
| **Matrix Synapse** | Open, decentralized real-time communication server |
| **ntfy** | Simple HTTP-based pub-sub notification service. Push to any device |

### 🖥️ Dashboards (7)
| App | Description |
|-----|-------------|
| **Dasherr** | Minimal, lightweight app dashboard with customizable layout |
| **Flame** | Self-hosted startpage and application dashboard with bookmarks |
| **Heimdall** | Application dashboard with search functionality and enhanced app tiles |
| **Launcharr** | Modern, customizable application launcher and dashboard for self-hosted services |
| **Organizr** | HTPC/homelab organizer with tabbed interface and user authentication |

### 🗄️ Databases (4)
| App | Description |
|-----|-------------|
| **MariaDB** | MySQL-compatible relational database. Used by many homelab apps |
| **MongoDB** | Document-oriented NoSQL database for flexible data storage |
| **PostgreSQL** | Advanced open-source relational database with JSON support |
| **Redis** | In-memory data structure store. Used as cache/message broker |

### 💻 Development (3)
| App | Description |
|-----|-------------|
| **Code Server** | Run VS Code in the browser on your server. Code from anywhere |
| **Drone CI** | Container-native continuous integration and delivery platform |
| **Gitea** | Lightweight self-hosted Git service. Like GitHub, locally |

### ⬇️ Downloaders (10)
| App | Description |
|-----|-------------|
| **Aria2 Pro** | Multi-protocol & multi-source command-line download utility |
| **Deluge** | Highly extensible BitTorrent client with a rich plugin ecosystem |
| **JDownloader 2** | Java-based download manager supporting 100s of file hosters |
| **NZBGet** | Efficient, highly optimised Usenet downloader |
| **Pinchflat** | Self-hosted YouTube and media subscription manager with automatic downloads |
| **SABnzbd** | Best-in-class Usenet NZB downloader with post-processing |
| **Transmission** | Lightweight, cross-platform BitTorrent client |
| **pyLoad** | Free, open-source download manager written in Python |
| **qBittorrent** | Popular, feature-rich BitTorrent client with a clean web UI |
| **qbitrr** | Companion app for qBittorrent, Radarr, and Sonarr — manages queue and stalled items |

### ☁️ File & Cloud (7)
| App | Description |
|-----|-------------|
| **File Browser** | Web-based file manager with user management and sharing |
| **Immich** | High-performance self-hosted photo and video backup solution |
| **Nextcloud** | Self-hosted productivity platform: files, calendar, contacts, and more |
| **Paperless-ngx** | Document management system that transforms scans into searchable documents |
| **PhotoPrism** | AI-powered photo management with face recognition and geo-tagging |
| **Stirling PDF** | Locally hosted web-based PDF manipulation tool with 30+ operations |
| **Syncthing** | Continuous file synchronization between devices. No cloud required |

### 🏠 Home & Misc (8)
| App | Description |
|-----|-------------|
| **Actual Budget** | Self-hosted personal finance app. Local-first budgeting that syncs |
| **Calibre-Web** | Web-based eBook library browser and reader with OPDS support |
| **CyberChef** | The Cyber Swiss Army Knife — encode, decode, encrypt, analyse data |
| **FreshRSS** | Self-hosted RSS feed aggregator with multi-user support |
| **Grocy** | Grocery and household management system with barcode scanning |
| **Linkding** | Self-hosted bookmark manager with tags, search, and browser extensions |
| **Mealie** | Self-hosted recipe manager and meal planner with import from any URL |
| **Wallabag** | Self-hosted read-it-later application with offline access |

### 📺 Media Servers (7)
| App | Description |
|-----|-------------|
| **Audiobookshelf** | Self-hosted audiobook and podcast server with app support |
| **Emby** | Personal media server with live TV, DVR, and subtitle support |
| **Jellyfin** | Free, open-source media server. No subscriptions, no tracking |
| **Kavita** | Manga, comics, and book server with reading progress tracking |
| **Komga** | Media server for comics, manga, and graphic novels with OPDS support |
| **Navidrome** | Modern, lightweight music server compatible with Subsonic/Airsonic clients |
| **Plex Media Server** | Feature-rich media server with apps for almost every device |

### 🎞️ Media Tools (6)
| App | Description |
|-----|-------------|
| **FileFlows** | File processing system with a visual flow editor for media conversion |
| **HandBrake** | Video transcoder GUI accessible via web browser |
| **Jellystat** | Statistics and activity tracking dashboard for Jellyfin |
| **Kometa (Plex Meta Manager)** | Automates Plex collection and metadata management using YAML configs |
| **Tdarr** | Distributed media transcoding automation with health checks and GPU support |
| **Wizarr** | Automatic user invitation and onboarding system for Plex and Jellyfin |

### 📊 Monitoring (10)
| App | Description |
|-----|-------------|
| **Dozzle** | Realtime Docker log viewer with a clean, searchable web interface |
| **Glances** | Cross-platform system monitoring tool with a web interface |
| **Grafana** | Open observability platform for metrics, logs, and traces visualization |
| **Netdata** | Real-time, high-resolution system performance monitoring |
| **Portainer CE** | Docker management GUI with stacks, container management, and user roles |
| **Prometheus** | Systems monitoring and alerting toolkit with time-series database |
| **Scrutiny** | Hard drive health dashboard using S.M.A.R.T. data with alerts |
| **Speedtest Tracker** | Self-hosted internet speed test tracker with historical graphing |
| **Uptime Kuma** | Fancy self-hosted monitoring tool with status pages and notifications |
| **Watchtower** | Automatically updates running Docker containers when new images are available |

### 🎟️ Request Tools (5)
| App | Description |
|-----|-------------|
| **FlareSolverr** | Proxy server to bypass Cloudflare and DDoS-Guard protection |
| **Ombi** | User-facing media request portal for Plex, Emby, and Jellyfin |
| **Requestrr** | Discord bot for requesting movies and TV shows via Radarr/Sonarr |
| **Seerr** | Unified media request manager — successor to Jellyseerr and Overseerr |
| **Tautulli** | Monitoring and tracking tool for Plex Media Server |

### 🔀 Reverse Proxies (4)
| App | Description |
|-----|-------------|
| **Caddy** | Modern web server with automatic HTTPS, simple configuration |
| **Nginx Proxy Manager** | Easiest way to manage Nginx proxy hosts with SSL via Let's Encrypt |
| **SWAG** | Secure Web Application Gateway: Nginx + Certbot + Fail2ban in one container |
| **Traefik** | Cloud-native reverse proxy and load balancer with auto SSL |

### 🔒 Security (4)
| App | Description |
|-----|-------------|
| **Authelia** | Open-source authentication and authorization server with 2FA/MFA |
| **Authentik** | Open-source identity provider with SSO, OAuth2, and LDAP support |
| **CrowdSec** | Crowd-sourced security engine that detects and blocks malicious IPs |
| **Vaultwarden** | Unofficial Bitwarden-compatible password manager server. Lightweight |

### 🌐 VPN & Network (7)
| App | Description |
|-----|-------------|
| **AdGuard Home** | Network-wide ad and tracker blocking DNS server |
| **Gluetun** | VPN client container for 20+ providers. Route other containers through it |
| **Pi-hole** | DNS-based ad blocker for your entire network |
| **Tailscale** | Zero-config mesh VPN built on WireGuard. Connect all your devices |
| **Technitium DNS** | Self-hosted authoritative DNS server with ad blocking and split horizon |
| **WG-Easy** | Easiest WireGuard VPN server with a beautiful web UI for peer management |
| **WireGuard** | Fast, modern VPN tunnel. Use as a VPN server or client |

---

## Architecture

```
arrhub/
├── install.sh           ← One-command installer (builds image locally)
├── arrhub.sh            ← Bash TUI (run via `media`)
├── app.py               ← Flask WebUI (all HTML/CSS/JS embedded, served on :9999)
├── Dockerfile           ← Multi-stage build: docker CLI + python:3.12-slim
├── apps/
│   └── catalog.json     ← Master app catalog (103 apps)
└── README.md
```

Each app deploys to its own compose file at `/docker/<appname>/docker-compose.yml`, keeping stacks isolated and independently manageable.

**Adding a new app:** add an entry to `apps/catalog.json` with `id`, `name`, `category`, `image`, `ports`, `volumes`, `environment`, and optionally `description`.

---

## Docker Run (Manual)

```bash
docker build -t arrhub-webui:local .

docker run -d \
  --name arrhub_webui \
  --restart unless-stopped \
  -p 9999:9999 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /opt/arrhub/apps:/opt/arrhub/apps:ro \
  -v /opt/arrhub/data:/data \
  --pid=host \
  arrhub-webui:local
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
