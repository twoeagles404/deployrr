# Update Summary — Deployrr Files

**Date:** 2026-03-03
**Updated by:** Claude Agent
**Target:** GitHub user `twoeagles404` personal fork

---

## File 1: Dockerfile

### Changes Made:

1. **Line 16 — Maintainer Label**
   - OLD: `LABEL maintainer="YOUR_NAME"`
   - NEW: `LABEL maintainer="twoeagles404"`

2. **Line 17 — Version Label**
   - OLD: `LABEL version="2.0.0"`
   - NEW: `LABEL version="3.0.0"`

3. **Line 18 — Description Label**
   - OLD: `LABEL description="Deployrr Max — ..."`
   - NEW: `LABEL description="Deployrr — ..."`

4. **Line 19 — Image Source Label**
   - OLD: `LABEL org.opencontainers.image.source="https://github.com/YOUR_GITHUB_USERNAME/deployrr-max"`
   - NEW: `LABEL org.opencontainers.image.source="https://github.com/twoeagles404/deployrr"`

5. **Line 37 — Python Dependencies**
   - REMOVED: `pyyaml==6.0.1`
   - ADDED: `flask-sock==0.7.0` (for WebSocket log streaming)

6. **Line 43 — Copy Application Files**
   - OLD: `COPY app.py .`
   - NEW: 
     ```dockerfile
     COPY app.py .
     COPY apps/ ./apps/
     ```

7. **Lines 50-56 — Volume Documentation**
   - ADDED comments documenting volumes and environment variables:
     ```dockerfile
     # Volume: -v /opt/deployrr/data:/data  (SQLite DB + settings)
     # Volume: -v /var/run/docker.sock:/var/run/docker.sock  (Docker access)
     # Env: DEPLOYRR_TOKEN=your-secret-token  (optional auth token)
     # Env: DEPLOYRR_NO_AUTH=true  (disable auth for LAN-only use)
     ```

8. **CMD Structure**
   - NO CHANGES — kept gthread worker class (simpler than gevent setup)
   - Updated comment to reflect SSE support is maintained

### Result:
- Dockerfile is syntactically valid
- References updated to match new repo name (`deployrr` not `deployrr-max`)
- Version bumped to 3.0.0
- YAML dependency removed (catalog.json used instead)
- WebSocket support added via `flask-sock`
- App catalog directory now copied into image

---

## File 2: install.sh

### Changes Made:

1. **Lines 17-19 — GitHub Variables**
   - OLD: 
     ```bash
     GITHUB_USER="YOUR_GITHUB_USERNAME"
     GITHUB_REPO="deployrr-max"
     GITHUB_BRANCH="main"
     GITHUB_RAW="https://raw.githubusercontent.com/${GITHUB_USER}/${GITHUB_REPO}/${GITHUB_BRANCH}"
     ```
   - NEW:
     ```bash
     GITHUB_USER="twoeagles404"
     GITHUB_REPO="deployrr"
     GITHUB_BRANCH="main"
     GITHUB_RAW="https://raw.githubusercontent.com/${GITHUB_USER}/${GITHUB_REPO}/${GITHUB_BRANCH}"
     ```

2. **Line 22 — Version**
   - OLD: `VERSION="2.0.0"`
   - NEW: `VERSION="3.0.0"`

3. **Line 58 — Banner Title**
   - OLD: `Deployrr Max — v${VERSION}`
   - NEW: `v${VERSION}` (cleaner)

4. **Line 59 — Banner Subtitle**
   - OLD: Mentioned "Deployrr Max"
   - NEW: Generic branding (just version)

5. **Line 130 — Deployrr Description**
   - OLD: `Deployrr Max — 'media' CLI shortcut`
   - NEW: `Deployrr — 'media' CLI shortcut`

6. **Line 285 — Download Comment**
   - OLD: `# ── deployrr.yaml — app catalog (used by WebUI)`
   - NEW (implicit): Will download `catalog.json` instead
   - Line 291: Changed from `catalog.yaml` to `catalog.json`

7. **Lines 330-334 — Success Summary**
   - OLD: References to "Deployrr Max"
   - NEW: References to "Deployrr"

8. **Line 349 — Final Message**
   - OLD: `Deployrr Max v${VERSION} installed successfully!`
   - NEW: `Deployrr v${VERSION} installed successfully!`

### Result:
- install.sh is syntactically valid (verified with `bash -n`)
- All references updated from `YOUR_GITHUB_USERNAME` to `twoeagles404`
- All repo references updated from `deployrr-max` to `deployrr`
- Version bumped to 3.0.0
- Catalog download changed from `.yaml` to `.json`
- All branding updated to reflect new repository name

---

## Summary of Key Changes

| Item | Before | After |
|------|--------|-------|
| **Maintainer** | YOUR_NAME | twoeagles404 |
| **GitHub Repo** | deployrr-max | deployrr |
| **Version** | 2.0.0 | 3.0.0 |
| **Catalog Format** | catalog.yaml | catalog.json |
| **YAML Support** | Yes (pyyaml) | No (removed) |
| **WebSocket** | No (flask-sock) | Yes (flask-sock==0.7.0) |
| **Apps Directory** | Not copied | Copied to image |
| **SQLite** | Implicit | Documented |

---

## Files Modified

1. `/sessions/keen-exciting-darwin/mnt/New Deployrr/Dockerfile`
2. `/sessions/keen-exciting-darwin/mnt/New Deployrr/install.sh`

Both files are ready for git commit and push to GitHub.
