# Changelog

All notable changes to ArrHub are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [3.7.0] — 2026-03-09

### Added
- **Sidebar collapse toggle** — chevron button in the topbar collapses the left sidebar to 60px
  icon-only mode; state persisted to `localStorage`.
- **Container card size slider** — range input in the Containers header sets `--ctr-card-min`
  (200–500 px) so users control card density; persisted to `localStorage`.
- **Network bandwidth charts** — Chart.js line charts (TX ↑ and RX ↓) on the Network tab with
  60-point rolling history; rate computed from cumulative `bytes_sent`/`bytes_recv` deltas.
- **Storage pie/donut charts** — each mounted filesystem shown as a Chart.js donut card with
  colour-coded fill (green < 70%, yellow < 90%, red ≥ 90%) and used/total labels.
- **Port Map accordion** — ports grouped by container; collapsed by default for multi-port apps
  and stopped containers; "⊞ All / ⊟ All" bulk-expand controls.
- **Stack Manager cards** — each stack now shows as a rich card with Up / Down / Restart / Pull
  actions; new `/api/stack/<name>/pull` backend endpoint runs `docker compose pull`.
- **More live news streams** — 6 additional YouTube 24/7 channels: ABC News, Euronews, NHK World,
  RT International, Lofi Girl (study), 8K Nature/Relax. Total: 12 channels.
- **Unsplash URL support** — paste `https://unsplash.com/photos/<id>` in Background Image;
  automatically resolved to `https://images.unsplash.com/photo-<id>?w=1920&q=80`.
- **5 new TUI presets** — Gaming (Sunshine/Moonlight), Home Automation (HA + Node-RED + Zigbee),
  Dev Workstation (Gitea + Code-Server), Security (Vaultwarden + Authentik), Downloads-only.

### Fixed
- **Container table view invisible** — `setCtrView('table')` was clearing the inline `display`
  style, causing the CSS `display:none` default to take over; fixed to use `display:block`.
- **Tailscale serve/funnel** — now pass `--bg` flag so serve/funnel configs persist as background
  services across tailscaled restarts.

### Changed
- **Tailscale LXC install** — pins version v1.94.2 from `pkgs.tailscale.com` (last known-good
  release for unprivileged cgroup2 LXC containers); falls back to `install.sh` on arch mismatch.

---

## [3.6.0] — 2026-03-09

### Added
- **Draggable / resizable Overview dashboard** — GridStack v10 powers 7 moveable widgets
  (System Info, Weather, Service Cards, Docker, Network I/O, Recent Logs, Containers Live).
  Click "Edit Layout" in the Overview header to enter drag-and-resize mode; layout is saved
  to `localStorage` per browser.
- **Theme system** — 5 built-in themes selectable from Settings → Appearance:
  Dark (default), Light, Nord, Catppuccin, Dracula. All implemented via CSS `data-theme` attribute.
- **Accent colors** — 6 accent swatches (Blue, Purple, Green, Orange, Pink, Cyan) that repaint
  every interactive element without requiring a page reload.
- **Background image support** — paste any image URL; configure blur (0–20 px) and overlay
  darkness (0–95 %); persisted across sessions via `localStorage`.
- **Collapsible RSS All-tab** — in the "All" category view each feed column starts collapsed
  so all categories fit on one screen. Click any header to expand. Chevron badge updates
  from "N sources" to "N articles" once the feed loads. Source tabs remain visible while
  collapsed and auto-expand the column when clicked. "Expand All" / "Collapse All" bulk controls.
- **YouTube live news streams** — 6 channels (BBC, Al Jazeera, Sky News, Bloomberg, DW News,
  France 24) replace broken direct-site iframes that were blocked by `X-Frame-Options` headers.
- **Service cards on Overview** — Radarr upcoming movies, Sonarr upcoming episodes,
  Plex active streams, Seerr/Overseerr recent requests; API keys configured in Settings.
- **`arrhub_webui` re-install guard** — `add_service_arrhub_webui()` in `arrhub.sh` skips
  silently if the container is already running, preventing double-installs from wizard/presets.
- **`.env.example`** — documents all environment variables: auth, database path, and the 8
  new service integration keys (Radarr, Sonarr, Plex, Seerr).
- **GITHUB_BRANCH enforcement** — pre-commit hook (`.github/hooks/pre-commit`) and GitHub
  Actions workflow (`.github/workflows/check-install-branch.yml`) block commits/PRs where
  `GITHUB_BRANCH` in `install.sh` doesn't match the target branch.

### Fixed
- **Live News iframes** — replaced direct-site embeds (bbc.com, aljazeera.com, etc.) with
  YouTube live stream embeds which allow iframe embedding; expanded from 4 → 6 channels.
- **SSE reconnect after wizard** — exponential backoff (3 → 30 s) prevents hammering a
  busy server; null evtSource before close() prevents the early-return guard blocking retries.
- **Container chart flicker** — smart DOM diffing updates existing container cards in-place
  rather than destroying and recreating all Chart.js canvas instances on every 8 s poll.
- **`install.sh` GITHUB_BRANCH mismatch** — `dev` branch `install.sh` had `GITHUB_BRANCH="main"`
  hardcoded, so dev installs silently pulled files from main. Now set to `"dev"` in dev branch.
- **Tailscale in LXC** — default now uses `--tun=userspace-networking`; new PVE host install
  path (`tailscale_pve_host_install()`) avoids TUN device dependency entirely.

### Changed
- **RSS feeds** — rewritten to extract thumbnails (`media:thumbnail`, `media:content`,
  enclosure, first `<img>`, YouTube thumbnail), 200-char excerpts, 5-minute per-feed cache;
  rich card layout with parallel loading via `Promise.all`.
- **Weather widget** — now shows humidity, wind speed, and "feels like" alongside temperature
  and 5-day forecast strip.
- **App catalog** — Homer and Homarr removed (replaced by ArrHub WebUI); `total_apps` 103 → 101.
- **TUI presets** — 9 presets including Movies★, Music★, Photos★, General Homelab;
  `detect_installed_services()` and `_smart_preset_hint()` auto-detect running containers.
- **Wizard Step 4** — Homer/Homarr replaced with Uptime Kuma + Watchtower.

### Removed
- **Homer** — removed from `apps/catalog.json`, `arrhub.sh` (`define_app`, `ALL_APPS`,
  `add_service_homer()` function, ~95 lines), and `README.md` dashboards table.
- **Homarr** — same as Homer above.

---

## [3.5.0] — initial public release

- One-command install via `curl | sudo bash`
- Pure Bash TUI (`media` command) with Media Server Wizard
- Flask WebUI on `:9999` with real-time SSE metrics
- 103 apps across 17 categories in `apps/catalog.json`
- Container management, Deploy tab, Stack Manager, Updates, Backup
- RSS feeds with category pills and YouTube feed support
- Tailscale TUI integration
- Multi-arch Docker builds (`linux/amd64`, `linux/arm64`) via GitHub Actions
