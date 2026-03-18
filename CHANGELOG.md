# Changelog

All notable changes to ArrHub are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [3.15.32] — 2026-03-17

### Fixed
- **Dashboard card overflow** — CSS Grid children default to `min-width: auto`, causing content
  (text, gauges, tables) to overflow their column width. Added `min-width: 0; width: 100%;
  box-sizing: border-box` to `.dash-cell` and `#tab-overview .panel`. Docker/Network I/O sections
  inside `#dash-infra` also received `min-width: 0; overflow: hidden` to prevent inner flex rows
  from blowing out the cell. Gauge row changed to `minmax(160px,1fr)` so gauges never squash
  below a usable size.
- **BinTV channels not loading** — `_BINTV_CHANNELS` was an empty array; selecting the BinTV
  source rendered nothing. Added 26 hardcoded UK/sports channels (Sky Sports, BT Sport,
  Eurosport, BBC One/Two, ITV, Channel 4, TNT Sports, etc.) with correct BinTV slugs.
  Added `iptvLoadSourceChannels()` dispatcher that bypasses the fetch path for BinTV/DaddyLive
  and populates `_iptvChannels` directly, then calls `iptvBuildCatPills()` and
  `iptvFilterChannels()`.
- **DaddyLive channels not loading** — same root cause as BinTV. Added 50 hardcoded DaddyLive
  channels (numeric IDs 1–50, covering Sky Sports, TNT Sports, BT Sport, beIN Sports, ESPN,
  Fox Sports, CNN, BBC, ITV and more). `iptvSetSource()` now calls `iptvLoadSourceChannels()`
  instead of the dead code path.
- **DaddyLive hardcoded domain** — stream URLs were built against `daddylive.cv` which rotates
  frequently. Domain is now stored in `localStorage` (`iptv_daddylive_domain`, default
  `daddylive.me`) and exposed via a small input field in the IPTV header so the user can update
  it without a code change.
- **Football fixtures showing nothing (ESPN date-range bug)** — the ESPN scoreboard endpoint was
  called with up to 60 separate `dates=YYYYMMDD` query parameters; the API honours only one
  `dates` value so only today's matches were ever returned, leaving the fixtures pane blank for
  future and past dates. Fixed by using the documented single-range format `dates=YYYYMMDD-YYYYMMDD`.
- **Football fixtures — football-data.org integration** — `/api/epl/matches` now tries
  football-data.org (API v4) first when a key is stored in Settings. Supports all major leagues
  via code map (`PL`, `PD`, `BL1`, `SA`, `FL1`, `PPL`, …). Falls back to ESPN automatically
  when no key is set or the league is not mapped.

---

## [3.15.31] — 2026-03-17

### Changed
- **Dashboard rebuilt with Homarr-style widget system** — complete overhaul of the overview grid
  and widget management to match Homarr's UX philosophy:
  - Grid switched from named CSS Grid areas to **12-column `grid-auto-flow: dense`** — hidden
    widgets leave no empty gap; remaining widgets compact automatically.
  - **"Edit" and "Widgets" buttons** are now always visible in the overview header. Edit mode
    highlights cells with a blue dashed outline and reveals an ✕ button on each widget to hide it.
    "Done" exits edit mode.
  - **Widget palette modal** redesigned: 9 toggleable widget cards with icon, name, description,
    and visible/hidden status. "Show All" restores everything. Click outside to close.
  - **Scroll opt-in**: only `storage`, `ctrs`, `services`, `infra`, `logs` carry `scrollable`
    class with a max-height cap. Gauges, System Info, Weather, and Launcher now size naturally —
    no scroll, no height crop.
  - `resetGridLayout()` now restores all hidden widgets without a page reload.
  - `WIDGET_DEFS` updated with `storage` widget and `desc`/`scrollable` metadata.

---

## [3.15.30] — 2026-03-17

### Changed
- **Overview dashboard redesigned to Homarr-style 3-column layout** — migrated from 2-column
  CSS Grid to a 3-column grid (`1fr 1fr 1fr`) with updated area assignments:
  `gauges(2col) | sysinfo` / `weather | storage | ctrs` / `services(3col)` /
  `infra(2col) | logs` / `launcher(3col)`. Responsive breakpoints at 900px (2-col) and 600px (1-col).
- **Containers widget** moved from the bottom row to the top-right (`weather | storage | ctrs`),
  giving it equal visual weight to weather and storage.
- **Per-cell max-heights** tuned for the new layout (gauges 200px, sysinfo/weather/storage/ctrs 320px).

### Added
- **Storage widget** (`#dash-storage`) — new dashboard cell using a compact horizontal-bar design.
  Shows each real filesystem (tmpfs/loop/overlay filtered out) with a colour-coded bar (green < 70%,
  yellow < 90%, red ≥ 90%), used/free/total sizes, and mount path. Loads via `/api/storage` at t=1.8s
  and refreshes every 30s alongside other dashboard data.

---

## [3.15.29] — 2026-03-17

### Added
- **UniDownloader stack** — new deployable preset combining three web-based download managers:
  - `gallery_dl` (Gallery-DL Server, `qx6ghqkz/gallery-dl-server:latest`, port 9080) — grab images/galleries from hundreds of sites (Twitter/X, Reddit, Instagram, Pixiv, etc.). Downloads land in `<MEDIA_DIR>/downloads/gallery-dl`.
  - `metube` (MeTube / yt-dlp, `ghcr.io/alexta69/metube:latest`, port 8081) — browser-based yt-dlp frontend for video & audio downloads. Subtitles enabled by default; downloads land in `<MEDIA_DIR>/downloads/metube`.
  - `jdownloader2` (JDownloader 2 GUI, `jlesage/jdownloader-2:latest`, port 5800) — full JDownloader 2 with remote VNC GUI for managed downloads from file-hosts.
- `UNIDOWNLOADER_STACK` preset — accessible from Quick Presets → option 15 "UniDownloader ★".
- `add_service_gallery_dl` and `add_service_metube` custom service writers that mount the correct download directories and respect the port manager.

---

## [3.15.28] — 2026-03-17

### Fixed
- **Intelligent Port Manager — intra-session conflict prevention** — deploying multiple apps in
  one session no longer causes two containers to receive the same host port. `find_free_port` and
  `resolve_ports` now use `_pm_port_available`, which checks both the live socket table **and**
  an in-session reservation table (`_PM_SESSION_RESERVED`). Every assigned port is immediately
  reserved so the next app in the same deployment can't claim it before any container has started.
- **Port Manager syntax bug** — `_pm_reserve_port` had an unclosed string literal (`"${2:-?"`)
  that would cause a bash parse error in Bash 4; corrected to `"${2:-unknown}"`.
- **`resolve_ports` now respects session reservations** — previously called the removed
  `port_in_use` helper; updated to use `_pm_port_available` + `_pm_reserve_port` so the same
  conflict-prevention logic applies to multi-port mappings (e.g. qBittorrent 8090+6881).

### Added
- `_pm_port_available <port>` — unified check: session-reserved OR host socket in use → false.
- `_pm_reserve_port <port> <app_id>` — marks a port as taken for the current session.
- `_pm_session_owner <port>` — returns the app that reserved a port this session (for logging).
- `_pm_is_hardcoded_port <port>` — prevents any app from being auto-assigned a system/infra port
  (SSH 22, HTTP 80/443, Proxmox WebUI 8006, DB ports, etc.).
- `_PM_CTR_PORT` — reference map of known container-internal ports for 30+ apps.
- `_PM_APP_ENV_PORT` — marks apps (e.g. qBittorrent via `WEBUI_PORT`) that can shift their
  internal port via an environment variable.

---

## [3.15.27] — 2026-03-17

### Fixed
- **Dashboard overview cards now scroll internally** — each widget cell has a capped max-height
  (380px default, tuned per-cell) and scrolls its content rather than stretching the page.
- **Football fixtures now show upcoming matches** — the JS fixtures loader was hitting the ESPN
  scoreboard endpoint directly with no date filter (returning only today's games). It now uses
  the server-side proxy which does a 60-day date-range scan for upcoming fixtures and 21 days for
  results. The Python endpoint also now accepts a `league` parameter so all leagues work.
- **IPTV browse panel no longer pops out as a fullscreen overlay** — Browse Channels is now an
  inline panel that slides open below the IPTV controls inside the tab, not a fixed modal covering
  the whole screen. Switching to BinTV or DaddyLive no longer auto-opens the browser.
- **Config Backup & Restore** — new Settings panel with Export Config (downloads a JSON backup of
  all server-side settings: API keys, URLs, credentials) and Import Config (uploads and restores
  from a backup file, then reloads the page). Useful after fresh installs or container rebuilds.

### Added
- `GET /api/config/export` — streams all database settings as a dated JSON attachment.
- `POST /api/config/import` — restores settings from a backup JSON, skipping internal keys.

---

## [3.15.26] — 2026-03-17

### Fixed
- **Twitter / X web viewer URL error** — pill buttons now carry a `data-handle` attribute
  with the clean username. Web Viewer mode reads `data-handle` instead of `textContent`,
  preventing the "𝕏 @Handle" prefix from polluting the proxy URL. Python endpoint also
  strips non-word leading characters as a safety net.
- **Reddit session login 403 Blocked** — login flow now seeds the CookieJar by first GETting
  the Reddit homepage so Reddit's anonymous `loid` cookies are present before the POST,
  matching real browser behaviour. Also retries against `old.reddit.com/api/login` if
  `www.reddit.com` fails. Browser-like Origin/Referer headers added. Error message for
  persistent 403s now includes step-by-step guidance for creating a Reddit script app.

---

## [3.15.25] — 2026-03-16

### Added
- **Reddit session login — no app required** — new Tier 1 auth using Reddit's cookie-based
  session login (`POST /api/login`). Only username and password needed; no Reddit app creation,
  no Client ID or Secret. Session cookie cached for 1 hour. Falls through to OAuth (Tier 2)
  and anonymous (Tier 3/4) if session login fails.

### Changed
- **Settings → Reddit Login panel** — username and password are now the primary fields at the
  top, clearly marked "No app creation required." Client ID and Secret moved into a collapsed
  `<details>` block labelled "Advanced — Reddit API App (optional)". Description updated to
  reflect that username+password is all that is needed for NSFW access.
- **Error messages** — Reddit errors now reference "Settings → Reddit Login" and distinguish
  between wrong password, session failure, and missing credentials.

---

## [3.15.24] — 2026-03-16

### Fixed
- **Football fixtures — upcoming matches not showing** — removed premature `break` in the
  `api_football_team_fixtures` proxy that stopped fetching after the first URL returned >5 past
  events. Now collects all unique events across every `season × seasontype` candidate URL
  (deduped by ESPN event ID), so upcoming PL fixtures and cup/European matches all appear.
- **Football team news empty** — team news proxy now tries 3 ESPN endpoints in order
  (`/teams/{id}/news`, `/league/news?team={id}`, `now.core.api.espn.com`) and normalises
  across response keys (`articles`, `items`, `feed`, `headlines`). Returns the first
  non-empty result.
- **Reddit error messages not actionable** — final error now explains exactly what is missing:
  username not set, login failed (with hint to check Settings), or NSFW/restricted sub.
- **Reddit JS error banner** — error display now shows an "Open Settings" button when the
  message indicates missing credentials, so the user can jump straight to the fix.

---

## [3.15.23] — 2026-03-16

### Fixed
- **Tab switching broken — all tabs showed Overview content** — the v3.15.21 CSS Grid redesign
  left 4 extra `</div>` closing tags inside `dash-services`, `dash-infra`, `dash-logs`, and
  `dash-ctrs` cells. This pushed every other tab panel (`#tab-iptv`, `#tab-containers`,
  `#tab-epl`, etc.) outside the `#content` div, making them render off-screen even when given
  the `active` class. Clicking any sidebar item appeared to do nothing. Fixed by removing one
  extra `</div>` from each of the 4 affected cells; all 8 dash-cells now open at depth 3 and
  return cleanly to depth 2.

---

## [3.15.22] — 2026-03-16

### Fixed
- **`_gs`/`_gsEditing` ReferenceError** — v3.15.21 removed the GridStack instance variables but
  left 5 call sites in `removeWidget`, `restoreWidget`, `_saveWidgetConfig`, and
  `_loadWidgetConfig`. Added stub declarations (`let _gs = null; let _gsEditing = false;`)
  and rewrote the four widget helper functions to operate on CSS Grid `#dash-{id}` cells
  directly (show/hide via `style.display`) instead of calling GridStack APIs.

---

## [3.15.21] — 2026-03-16

### Changed
- **Dashboard redesign — GridStack → CSS Grid** — replaced GridStack drag-and-drop layout with
  a clean, fixed 2-column CSS Grid inspired by Glance. Eight named grid areas:
  `gauges`, `weather`, `sysinfo`, `services`, `infra`, `logs`, `ctrs`, `launcher`.
  No more layout thrashing, overlapping titles, or positioning bugs from saved localStorage state.

### Added
- **Reddit 4-tier OAuth fallback** — Tier 1: password grant (script app with username+password);
  Tier 2: client_credentials (app-only); Tier 3: anonymous old.reddit.com with over18 cookie;
  Tier 4: Reddit RSS feed. Each tier falls through to the next on failure.

---

## [3.15.20] — 2026-03-15

### Added
- **Glance-style 7-day weather grid** — forecast widget redesigned as a horizontal strip with
  day label, date, emoji icon, high/low temperatures; matches the clean Glance dashboard style.
- **Reddit OAuth settings** — new Settings panel for Reddit Client ID, Client Secret, Username,
  and Password; required for NSFW subreddit access. Saved server-side to SQLite.
- **Twitter/X server proxy** — `/api/twitter/webviewer` fetches twitterwebviewer.com server-side,
  injects `<base href>`, strips `X-Frame-Options`/CSP headers, and returns proxied HTML so the
  feed displays inside an iframe. Cards/Web Viewer toggle in the Twitter tab.
- **Arsenal multi-season fixture fallback** — fixture proxy tries `season`, `season-1`,
  `season+1` × `no-type`, `type-2`, `type-3` to reliably find active season data across
  mid-season API edge cases.

---

## [3.15.19] — 2026-03-15

### Added
- **Football Hub tab** — dedicated Football section with: live Premier League table (ESPN free
  API), fixtures/results, highlights (Scorebat free API), and news. Tab navigation: Tables,
  Fixtures, Results, Highlights, News.
- **Team fixture panel** — click any team in the standings to open a side panel showing upcoming
  fixtures and recent results with scores, competition badges, and date/time. Server-side ESPN
  proxy handles CORS and season-year calculation (`year+1` if month ≥ August).
- **No-flicker feed refresh** — `_fbSetLoading`/`_fbClearLoading` helpers dim existing content
  (opacity 0.45 + pointer-events none) on repeat loads instead of replacing with a spinner,
  eliminating content flash.
- **Reddit always-proxy** — Reddit feed removed from direct browser fetch path entirely;
  all requests routed through the server proxy to avoid CORS blocks and age-gate HTML responses.

---

## [3.14.0] — 2026-03-10

### Added
- **Radarr/Sonarr tabbed service cards** — each card now has Upcoming, Queue, and Library tabs.
  Queue tab shows active downloads with progress bars, time remaining, quality, indexer, and
  download client info. Library tab shows aggregate stats (total, monitored, downloaded, missing).
- **Click-to-expand detail** — all service card items (Radarr, Sonarr, Plex, Seerr) are now
  clickable to reveal an inline detail panel with additional metadata.
- **System Gauges widget** — CPU, Memory, Load Average, and Containers are now a proper GridStack
  widget (`gs-id="gauges"`) that can be dragged, resized, and rearranged alongside other widgets.
  Previously the gauge row was stuck outside the grid.
- **Widget minimum sizes** — all widgets now have `gs-min-w` and `gs-min-h` constraints to prevent
  them from being resized too small to be useful.
- **Responsive compact mode** — widgets auto-detect their rendered size and toggle a `.widget-compact`
  CSS class that tightens padding, shrinks fonts, and stacks grids for small widget sizes.
- **New API endpoints** — `/api/services/radarr/queue`, `/api/services/radarr/library`,
  `/api/services/sonarr/queue`, `/api/services/sonarr/library`.

### Fixed
- **RSS feed collapse bug** — clicking a feed dropdown no longer collapses other feeds in the same
  CSS grid row. Fixed by adding `align-items:start` to the feed grid container.
- **GridStack layout version** — added `_GRID_VER` key to invalidate stale saved layouts when
  widget structure changes (e.g., adding the gauges widget).

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
