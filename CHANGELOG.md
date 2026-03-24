# Changelog

All notable changes to ArrHub are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [3.17.16] — 2026-03-24

### Fixed
- **Intellibot panel bleeding onto overview dashboard** — `intellibotInit()` was setting an inline
  `display:flex` style that persisted after `showTab()` removed the `active` class (inline styles
  override CSS class rules). Removed the inline style assignment; display is now controlled
  entirely by CSS `.tab-panel{display:none}` / `#tab-intellibot.active{display:flex}`.
  Also removed redundant inline `display:none;flex-direction:column` from the panel HTML element.
- **Live TV video IDs outdated** — YouTube 24/7 stream IDs rotate; updated all eight channels to
  currently-live IDs verified via YouTube live search: Bloomberg `iEpJwprxDdk`, Sky News
  `YDvsBbKfLPA`, Euronews `pykpO5kQJ98`, DW `LuKwFajn37U`, CNBC `9NyxcX3rhQs`, France24
  `Ap-UM1O9RBU`, Al Arabiya `n7eQejkXbnM`, Al Jazeera `gCNeDWCI0vo`.
- **Version bump** `3.17.15` → `3.17.16`.

---

## [3.17.15] — 2026-03-24

### Fixed
- **Services card slides not working** — Only the Launcher slide (slide 0) had visible content;
  slides 1–3 appeared broken/empty. Two root causes fixed:
  1. Missing overflow wrapper: added `<div style="flex:1;overflow:hidden;position:relative;min-height:0">`
     around `#apps-track`, matching the structure of the working Media Suite carousel. Without this,
     `translateX(-N×100%)` percentages didn't compute to one-slide increments.
  2. Content never loaded: `loadDashboardContainers()` was never called on page load so Slide 1
     (Containers) always displayed "Loading…". Now called on `window.load` and also when navigating
     to slide 1 via `appsGoTo(1)`.
- **Version bump** `3.17.14` → `3.17.15`.

---

### Fixed
- **Services card — only Launcher slide visible** — `#apps-track` had `overflow:hidden` which
  clipped slides 1-3 inside the track before the CSS transform could reveal them. The card's own
  `overflow:hidden` is the intended viewport; the track must not clip its own contents. Changed to
  `overflow:visible` — identical to the fix applied to `#msc-track` in v3.17.12.
- **Version bump** `3.17.14` → `3.17.15`.

---

## [3.17.14] — 2026-03-24

### Added
- **Live TV slide** — New Slide 4 in the Services / 🖥️ Apps widget with eight 24/7 YouTube
  live news streams: Bloomberg, Sky News, Euronews, DW, CNBC, France24, Al Arabiya, Al Jazeera.
  Tab bar to switch channels, mute/unmute toggle, lazy-loads only when swiped to.
- **Intellibot tab** — Full intellibot.app embedded in a new "Intellibot" sidebar entry and
  mobile bottom-nav button. Falls back to an open-in-new-tab button if the site blocks iframes.
- **Version bump** `3.17.13` → `3.17.14`.

---

## [3.17.13] — 2026-03-24

### Added
- **Live News Feed** — New "Live News" slide (Slide 3) in the Services / 🖥️ Apps widget.
  Swipe right or click the third nav dot to open it. Features: pulsing live red dot,
  category filter pills (🌐 All / 💻 Tech / 🖥️ Lab / 🌍 World), color-coded source
  badges, time-ago stamps, click-to-open-in-tab. Auto-refreshes every 5 minutes.
  Backend: new `/api/news/quick` endpoint fetches 4 RSS/Reddit feeds concurrently
  per category (ThreadPoolExecutor), merges + de-dupes, caches 5 min server-side.
  Sources: BBC World, AP News, Al Jazeera, Guardian, Hacker News, The Verge,
  Ars Technica, TechCrunch, r/selfhosted, r/homelab, r/Proxmox, r/docker.
  No API keys required.
- **Version bump** `3.17.12` → `3.17.13`.

---

## [3.17.12] — 2026-03-23

### Fixed
- **MSC slides 1–4 blank on navigation** — `#msc-track` had `overflow:hidden` which clipped
  every slide within the track's own box. Sonarr, Downloads, Plex, and Seerr had data the
  whole time but were invisible whenever the carousel transformed. Removed the erroneous
  overflow rule; the parent viewport div already handles clipping.
- **Downloads showing "qBittorrent login failed"** — Downloader type was set to qBittorrent
  pointing at Transmission port 9091. Switched to Transmission; Downloads now shows all torrents.
- **Version bump in all files** — `install.sh`, `arrhub.sh`, `README.md` all updated to 3.17.12.

---

## [3.17.11] — 2026-03-23

### Fixed
- **Media Suite Card navigation** — Replaced tiny dot indicators with named tab bar
  (`🎥 Radarr · 📺 Sonarr · ⬇ Downloads · ▶ Plex · 🎬 Seerr`). All five panels now clickable.
- **MoVITV removed** — Removed from IPTV dropdown, badge, source handler, and all conditions.
- **Featured panel slow load** — Added `sessionStorage` cache; last-known data renders instantly.

---

## [3.13.0] — 2026-03-10

### Fixed
- **Service Launcher widget** — `loadServiceLauncher()` was calling `data.filter()` on the API
  response object instead of `data.containers`, throwing `TypeError: data.filter is not a function`.
  Also fixed `c.state === 'running'` → `c.status === 'running'` to match the actual API field.
- **GridStack stacking flash on fresh install** — widgets were visible as a stacked pile before
  GridStack positioned them. Fixed by: moving gridstack-all.js to `<head>`, using
  `visibility:hidden` via `.grid-stack:not(.gs-ready)` CSS, and removing the 600ms `setTimeout`.

---

## [3.12.0] — 2026-03-10

### Fixed
- **Plex auth 401 error** — removed erroneous `_svc_get(url, "/status/sessions", None)` call
  that fired before the authenticated request, immediately raising a 401. Added explicit
  `if not token: return configured=False` guard. Plex card now loads correctly once token is set.

### Added
- **Service Launcher widget** (new overview panel) — displays all running Docker containers as
  clickable tiles with app icon, name, and first exposed port URL. Auto-refreshes when switching
  to the Overview tab.
- **Widget hide/show palette** — in Edit Layout mode each widget gets a ✕ button to remove it
  from the grid. An "Add Widget" button opens a palette modal showing all widgets with toggle
  toggle (click to hide or restore). State persists server-side via `/api/widget_config`.
- **Tdarr custom service** (`add_service_tdarr`) — proper compose YAML with internal transcoding
  node enabled, media dir mounted at `/media`, config/server/logs directories created, port
  conflict resolution. Auto-starts transcoding once a library is configured in the WebUI.
- **FileFlows custom service** (`add_service_fileflows`) — compose with `/media` mount, `/temp`
  working dir, port conflict resolution. Both Tdarr and FileFlows marked as `APP_CUSTOM_SVC`.

## [3.11.0] — 2026-03-10

### Fixed
- **API keys clear on revisit** — `GET /api/settings` now returns all 8 service integration
  fields (radarr/sonarr/plex/seerr urls + keys) so the Settings form repopulates correctly
  when navigating back to the tab.
- **arrhub_webui stopped by media wizard deploy** — `deploy_apps` was calling `docker rm -f`
  on every LOCAL_IMAGE_APP in `ok_pull`, which killed a running `arrhub_webui` then failed to
  restart it (compose file was empty because the early-return in `add_service_arrhub_webui`
  skipped writing it). Fix: if a LOCAL_IMAGE_APP container is already running, skip it from
  `ok_pull` entirely; the verify loop detects it running and counts it as `ok_start`.
- **Container page ghost/skeleton cards** — `renderContainers()` now clears all
  non-`.ctr-card` elements at the very start, including the early-return path for empty
  filtered results that previously left skeleton cards in place.
- **Live News broken embeds** — YouTube `live_stream?channel=` embedding is blocked by most
  news channels at the channel level. Replaced with 3 confirmed-working embedded streams
  (Al Jazeera, France 24, DW) + quick-launch cards (BBC, Sky, Bloomberg, ABC, Euronews, NHK,
  WION, CBS, Lofi Girl) that open YouTube directly. Removed RT International (banned from
  YouTube). Fixed ABC News using BBC's channel ID.

### Changed
- **Network bandwidth card** — canvas height reduced 100→55px, panel padding tightened;
  card is now readable without dominating the Storage & Network tab.
- **Storage & Network tab** — added combined section header with subtitle and Refresh button.
- **Overview GridStack layout** — `cellHeight` 80→60 (finer snap), resize handles `se` only →
  `e,se,s,sw,w` (all sides), drag bound to `.panel-title` bar, new **Reset Layout** button
  appears after a custom layout is saved.

---

## [3.10.0] — 2026-03-09

### Fixed
- **Service API keys not persisting** — `POST /api/settings` allowed-list was missing all service
  integration keys (`seerr_url`, `seerr_api_key`, `radarr_*`, `sonarr_*`, `plex_*`). They were
  sent correctly by the JS but silently discarded server-side. Now all 8 service fields are saved.
- **GridStack widget scrollbars on resize** — shrinking a widget now scales text/emojis down
  instead of showing a scrollbar. Fixed by: (a) setting `overflow:hidden` on
  `.grid-stack-item-content` and all widget panels, and (b) adding CSS container queries on
  `.stat-card` so values and labels shrink proportionally when the card is ≤130px or ≤90px wide.

### Changed
- **Storage & Network merged in sidebar** — the two separate "Storage" and "Network" nav entries
  are now a single "Storage & Network" item that opens a combined tab showing disk info (top)
  and live bandwidth charts + interface table (bottom).
- **RSS feed list overhauled** — replaced deprecated/paywalled sources with working alternatives:
  CNN→AP News, Reuters→NPR+DW, IGN Feedburner→feeds.ign.com, Bloomberg→MarketWatch,
  FT→Economist, Goal.com→BBC Football, Sky Sports kept, New Scientist→Phys.org+Space.com.

---

## [3.9.0] — 2026-03-09

### Added
- **dlstreams.top Free TV integration** — replaced broken HLS-direct streams (CORS failures) with
  embedded iframe of `dlstreams.top/24-7-channels.php` (1 000+ channels) plus 12 quick-launch
  channel cards (ABC, ESPN, Sky Sports, beIN, CNN, Fox News, BBC, NBA TV, TNT, NASA, DW, More).
  Custom M3U8 input kept inside a collapsible `<details>` block for advanced users.

### Fixed
- **Container skeleton cards persisting** — `renderContainers()` now removes all non-`.ctr-card`
  children (skeleton placeholders) before injecting real data, instead of only clearing on
  `.empty` state; fixes ghost cards visible alongside live container cards.
- **`arrhub_webui` duplicate install from media wizard** — `build_my_stack()` wizard no longer
  force-appends `arrhub_webui` to `final_selection`; `install.sh` already handles WebUI install
  before the wizard runs, so this caused a redundant deploy attempt on every media stack.

### Changed
- **Dashboard: Docker & Network merged panel** — two separate 6-column GridStack widgets (Docker
  stats and Network I/O) collapsed into a single 12-column "Docker & Network" panel with a
  6-up stat grid (Images, Volumes, Networks, Docker Disk, Net ↑ Sent, Net ↓ Recv).

---

## [3.8.0] — 2026-03-09

### Added
- **Free TV / IPTV tab** — new "🎬 Free TV" sub-tab in Live Feeds; includes HLS.js-powered live
  streams (NASA TV, France 24 English, Al Jazeera English) that play directly in-browser, plus a
  custom M3U8 URL input field and link cards for Pluto TV, Tubi, Plex Free TV, Peacock, Roku
  Channel and Crackle (open in new tab).
- **HLS.js 1.5.13** — loaded from CDN; used for native browser HLS stream playback on the Free TV
  tab. Falls back to native HLS on Safari/iOS.

### Fixed
- **Unsplash URL resolution** — `_resolveUnsplash()` now correctly extracts the short photo ID
  from the URL slug (the last dash-separated token, e.g. `MjH55Ef3w_0`) and builds a
  `source.unsplash.com/{id}/1920x1080` URL. Previously the entire slug was used as the ID.
- **GridStack `resizestop`** — added event handler so any Chart.js canvases inside a widget are
  asked to resize when the user drags the resize handle. Also added a layout reflow nudge for
  panels and stat grids.
- **Network chart aspect ratio** — changed `maintainAspectRatio: true → false` on TX/RX line
  charts so they fill their container height when the widget is resized.

### Changed
- **Preset stacks no longer include `arrhub_webui`** — all 9 quick-deploy presets (Movies, Music,
  Photos, Homelab, Gaming, Home Automation, Dev, Security, Downloads) had `arrhub_webui` appended.
  Removed from all presets because `install.sh` already installs ArrHub WebUI before any preset
  is deployed; including it in presets caused redundant re-installs.
- **Live News grid** — column min-width reduced from `480px` to `300px` so streams stack
  vertically on mobile screens instead of overflowing.
- **GridStack mobile** — single-column layout and drag/resize disabled when viewport < 900px to
  prevent awkward touch interactions.
- **RSS view tab controls** — expand/collapse controls now hidden when the Live News or Free TV
  sub-tab is active (only relevant for the Feeds view).

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
