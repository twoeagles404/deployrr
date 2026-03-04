#!/usr/bin/env python3
"""
Deployrr Max Monitor — Enhanced Server Administration Dashboard
Version: 3.1.0 · Full deployment, update management, and real-time monitoring
Port: 9999

Dependencies:
  pip install flask psutil requests docker

"""
import json, os, re, subprocess, time, glob, threading, xml.etree.ElementTree as ET, sqlite3, secrets, hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from functools import wraps
import psutil
import requests
from flask import Flask, jsonify, request, Response

app = Flask(__name__)

# ── Docker (optional) ────────────────────────────────────────────────────────
try:
    import docker as _docker
    _dc = _docker.from_env()
    DOCKER_OK = True
except Exception:
    _dc = None
    DOCKER_OK = False

_EXECUTOR = ThreadPoolExecutor(max_workers=8)

# ── Caching ──────────────────────────────────────────────────────────────────
_weather_cache = {"data": None, "ts": 0}
_rss_cache = {}
_catalog_cache = {"data": None, "ts": 0}
CACHE_WEATHER = 1800  # 30 minutes
CACHE_RSS = 900       # 15 minutes
CACHE_CATALOG = 300   # 5 minutes

# ── Catalog ───────────────────────────────────────────────────────────────────
# Check multiple locations: volume mount first, then local (for development/baked-in)
_catalog_candidates = [
    "/opt/deployrr/apps/catalog.json",                          # runtime volume mount
    os.path.join(os.path.dirname(__file__), "apps", "catalog.json"),  # baked into image
]
CATALOG_PATH = next((p for p in _catalog_candidates if os.path.isfile(p)),
                     _catalog_candidates[-1])

def _load_catalog():
    """Load app catalog from catalog.json (cached)."""
    global _catalog_cache
    if _catalog_cache["data"] and (time.time() - _catalog_cache["ts"]) < CACHE_CATALOG:
        return _catalog_cache["data"]
    try:
        with open(CATALOG_PATH) as f:
            data = json.load(f)
        _catalog_cache["data"] = data
        _catalog_cache["ts"] = time.time()
        return data
    except Exception:
        return {"apps": []}

def _catalog_to_registry():
    """Convert catalog.json apps list to registry dict (keyed by id)."""
    catalog = _load_catalog()
    return {a["id"]: a for a in catalog.get("apps", [])}

# Keep backward compat - load from catalog
APP_REGISTRY = _catalog_to_registry()

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = os.environ.get("DEPLOYRR_DB", "/data/deployrr.db")
os.makedirs(os.path.dirname(DB_PATH) or "/data", exist_ok=True)

def _db_init():
    """Initialize SQLite database with required tables."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS deploy_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now')),
                app_id TEXT,
                app_name TEXT,
                action TEXT,
                status TEXT,
                compose_snapshot TEXT,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY,
                metric TEXT,
                threshold REAL,
                operator TEXT DEFAULT 'gt',
                enabled INTEGER DEFAULT 1,
                updated_at TEXT DEFAULT (datetime('now'))
            );
        """)
        conn.commit()

def _db_get(key, default=None):
    """Get a setting value from SQLite."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else default
    except Exception:
        return default

def _db_set(key, value):
    """Set a setting value in SQLite."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now'))", (key, str(value)))
            conn.commit()
    except Exception as e:
        print(f"DB set error: {e}")

_db_init()

# ── Authentication ────────────────────────────────────────────────────────────
_NO_AUTH = os.environ.get("DEPLOYRR_NO_AUTH", "").lower() in ("1", "true", "yes")

def _get_token_hash():
    """Get or generate the auth token (hashed)."""
    stored = _db_get("token_hash")
    if stored:
        return stored
    # Generate new token
    raw = os.environ.get("DEPLOYRR_TOKEN") or secrets.token_urlsafe(24)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    _db_set("token_hash", token_hash)
    _db_set("token_raw_hint", raw[:8] + "..." + raw[-4:])
    print(f"\n{'='*60}")
    print(f"  DEPLOYRR TOKEN: {raw}")
    print(f"  Save this — it won't be shown again.")
    print(f"{'='*60}\n")
    return token_hash

def _check_auth():
    """Verify Bearer token. Returns True if authenticated or auth disabled."""
    if _NO_AUTH:
        return True
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header[7:]
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return token_hash == _get_token_hash()

def require_auth(f):
    """Decorator for routes that require auth."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _check_auth():
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# Initialize token on startup
if not _NO_AUTH:
    _get_token_hash()

# =============================================================================
# API ENDPOINTS
# =============================================================================

@app.route("/")
def index():
    return _HTML_SPA

@app.route("/api/overview")
def api_overview():
    """System overview: CPU, memory, load average, uptime."""
    try:
        cpu_pct = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        load = os.getloadavg() if hasattr(os, 'getloadavg') else (0, 0, 0)
        uptime = time.time() - psutil.boot_time()

        # Temperature
        temps = {}
        try:
            if hasattr(psutil, 'sensors_temperatures'):
                temps = psutil.sensors_temperatures()
        except Exception:
            pass

        return jsonify({
            "cpu_percent": cpu_pct,
            "memory": {
                "total": mem.total,
                "used": mem.used,
                "percent": mem.percent,
                "available": mem.available
            },
            "load_avg": {"1m": load[0], "5m": load[1], "15m": load[2]},
            "uptime_seconds": uptime,
            "uptime_display": _format_uptime(uptime),
            "temperatures": temps
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stream")
def api_stream():
    """SSE stream: pushes overview metrics every 2 seconds."""
    def generate():
        while True:
            try:
                cpu_pct = psutil.cpu_percent(interval=0.5)
                mem = psutil.virtual_memory()
                load = os.getloadavg() if hasattr(os, 'getloadavg') else (0, 0, 0)
                uptime = time.time() - psutil.boot_time()
                data = {
                    "cpu_percent": round(cpu_pct, 1),
                    "mem_percent": round(mem.percent, 1),
                    "mem_used_gb": round(mem.used / 1e9, 2),
                    "mem_total_gb": round(mem.total / 1e9, 2),
                    "load_1m": round(load[0], 2),
                    "load_5m": round(load[1], 2),
                    "load_15m": round(load[2], 2),
                    "uptime": _format_uptime(uptime),
                    "ts": int(time.time())
                }
                yield f"data: {json.dumps(data)}\n\n"
                time.sleep(2)
            except GeneratorExit:
                break
            except Exception:
                time.sleep(2)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/storage")
def api_storage():
    """Disk usage and I/O statistics."""
    try:
        disks = {}
        for part in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disks[part.mountpoint] = {
                    "device": part.device,
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                    "percent": usage.percent,
                    "fstype": part.fstype
                }
            except (PermissionError, OSError):
                pass

        io_stats = psutil.disk_io_counters()
        io_data = {
            "read_bytes": io_stats.read_bytes,
            "write_bytes": io_stats.write_bytes,
            "read_count": io_stats.read_count,
            "write_count": io_stats.write_count
        } if io_stats else {}

        return jsonify({
            "disks": disks,
            "io": io_data
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/network")
def api_network():
    """Network interfaces and statistics."""
    try:
        interfaces = {}
        for name, addrs in psutil.net_if_addrs().items():
            interfaces[name] = [{"family": str(addr.family), "address": addr.address, "netmask": addr.netmask} for addr in addrs]

        net_io = psutil.net_io_counters()
        connections = len(psutil.net_connections(kind='inet'))

        return jsonify({
            "interfaces": interfaces,
            "io": {
                "bytes_sent": net_io.bytes_sent,
                "bytes_recv": net_io.bytes_recv,
                "packets_sent": net_io.packets_sent,
                "packets_recv": net_io.packets_recv,
                "errin": net_io.errin,
                "errout": net_io.errout,
                "dropin": net_io.dropin,
                "dropout": net_io.dropout
            },
            "connections": connections
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/containers")
def api_containers():
    """List all containers with status."""
    if not DOCKER_OK:
        return jsonify({"containers": [], "error": "Docker not available"}), 500

    try:
        containers = _dc.containers.list(all=True)
        result = []
        for c in containers:
            result.append({
                "id": c.id,
                "name": c.name,
                "image": c.image.tags[0] if c.image.tags else "unknown",
                "status": c.status,
                "state": c.attrs.get("State", {}),
                "ports": _extract_ports(c)
            })
        return jsonify({"containers": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/container/<cname>/<action>", methods=["POST"])
def api_container_action(cname, action):
    """Container action: start, stop, restart, remove, logs."""
    if not DOCKER_OK:
        return jsonify({"error": "Docker not available"}), 500

    try:
        container = _dc.containers.get(cname)

        if action == "start":
            container.start()
            return jsonify({"status": "started"})
        elif action == "stop":
            container.stop()
            return jsonify({"status": "stopped"})
        elif action == "restart":
            container.restart()
            return jsonify({"status": "restarted"})
        elif action == "remove":
            container.remove(force=True)
            return jsonify({"status": "removed"})
        else:
            return jsonify({"error": "Unknown action"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/container/<cname>/logs")
def api_container_logs(cname):
    """Get container logs."""
    if not DOCKER_OK:
        return jsonify({"error": "Docker not available"}), 500

    try:
        container = _dc.containers.get(cname)
        logs = container.logs(tail=100).decode('utf-8', errors='replace')
        return jsonify({"logs": logs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/hardware")
def api_hardware():
    """Hardware information: CPU, memory, board, GPU."""
    try:
        cpu_info = {
            "count": psutil.cpu_count(),
            "freq": psutil.cpu_freq()._asdict() if psutil.cpu_freq() else {}
        }

        memory = psutil.virtual_memory()
        mem_info = {
            "total": memory.total,
            "available": memory.available,
            "used": memory.used,
            "percent": memory.percent
        }

        return jsonify({
            "cpu": cpu_info,
            "memory": mem_info,
            "platform": {
                "system": os.uname().sysname,
                "release": os.uname().release,
                "machine": os.uname().machine
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/logs")
def api_logs():
    """System logs from journalctl."""
    try:
        lines = request.args.get('lines', 100, type=int)
        unit = request.args.get('unit', '')

        cmd = ["journalctl", "-n", str(lines), "--no-pager"]
        if unit:
            cmd.extend(["-u", unit])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return jsonify({"logs": result.stdout})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/catalog")
def api_catalog():
    """Get application catalog."""
    return jsonify({"apps": APP_REGISTRY})

@app.route("/api/catalog/apps")
def api_catalog_apps():
    """Get all apps from catalog.json, optionally filtered by category or search."""
    catalog = _load_catalog()
    apps = catalog.get("apps", [])

    q = request.args.get("q", "").lower()
    cat = request.args.get("category", "")

    if q:
        apps = [a for a in apps if q in a.get("name","").lower() or q in a.get("description","").lower() or q in a.get("id","").lower()]
    if cat:
        apps = [a for a in apps if a.get("category","") == cat]

    # Get running container names for status check
    running = set()
    if DOCKER_OK:
        try:
            for c in _dc.containers.list(all=True):
                running.add(c.name.lower())
        except Exception:
            pass

    for a in apps:
        a["deployed"] = any(a["id"].lower() in name for name in running)

    categories = sorted(set(a.get("category","") for a in catalog.get("apps", [])))
    return jsonify({"apps": apps, "categories": categories, "total": len(apps)})

@app.route("/api/catalog/categories")
def api_catalog_categories():
    """Get all unique categories from catalog."""
    catalog = _load_catalog()
    cats = sorted(set(a.get("category","") for a in catalog.get("apps", [])))
    return jsonify({"categories": cats})

@app.route("/api/deploy", methods=["POST"])
@require_auth
def api_deploy_app():
    """Deploy an app by generating and running its compose snippet."""
    data = request.json or {}
    app_id = data.get("app_id")

    if not app_id:
        return jsonify({"error": "Missing app_id"}), 400

    catalog = _load_catalog()
    app_data = next((a for a in catalog.get("apps", []) if a["id"] == app_id), None)
    if not app_data:
        return jsonify({"error": f"App '{app_id}' not found in catalog"}), 404

    config_dir = _db_get("config_dir", "/docker")
    media_dir = _db_get("media_dir", "/mnt/media")
    tz = _db_get("tz", "America/New_York")
    puid = _db_get("puid", "1000")
    pgid = _db_get("pgid", "1000")

    def replace_placeholders(s):
        return (s.replace("{CONFIG}", config_dir)
                 .replace("{MEDIA}", media_dir)
                 .replace("{TZ}", tz)
                 .replace("{PUID}", puid)
                 .replace("{PGID}", pgid))

    # Build compose snippet
    ports_yaml = "\n".join(f"      - \"{p}\"" for p in app_data.get("ports", []))
    vols_yaml = "\n".join(f"      - \"{replace_placeholders(v)}\"" for v in app_data.get("volumes", []))
    env_yaml = "\n".join(f"      - \"{replace_placeholders(e)}\"" for e in app_data.get("environment", []))

    snippet = f"""
  {app_id}:
    image: {app_data['image']}
    container_name: {app_id}
    restart: {app_data.get('restart', 'unless-stopped')}
"""
    if ports_yaml:
        snippet += f"    ports:\n{ports_yaml}\n"
    if vols_yaml:
        snippet += f"    volumes:\n{vols_yaml}\n"
    if env_yaml:
        snippet += f"    environment:\n{env_yaml}\n"

    # Write to /tmp compose file and run
    compose_path = f"/tmp/deployrr_{app_id}.yml"
    compose_content = f"services:\n{snippet}"

    try:
        with open(compose_path, "w") as f:
            f.write(compose_content)

        result = subprocess.run(
            ["docker", "compose", "-f", compose_path, "up", "-d"],
            capture_output=True, text=True, timeout=120
        )

        status = "success" if result.returncode == 0 else "failed"
        error = result.stderr if result.returncode != 0 else None

        # Log to history
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "INSERT INTO deploy_history (app_id, app_name, action, status, compose_snapshot, error) VALUES (?,?,?,?,?,?)",
                    (app_id, app_data["name"], "deploy", status, compose_content, error)
                )
                conn.commit()
        except Exception:
            pass

        return jsonify({
            "status": status,
            "app_id": app_id,
            "stdout": result.stdout[-2000:],
            "stderr": result.stderr[-2000:] if error else ""
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/history")
def api_deploy_history():
    """Get deployment history."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT id, timestamp, app_id, app_name, action, status, error FROM deploy_history ORDER BY timestamp DESC LIMIT 50"
            ).fetchall()
        return jsonify({"history": [
            {"id": r[0], "timestamp": r[1], "app_id": r[2], "app_name": r[3], "action": r[4], "status": r[5], "error": r[6]}
            for r in rows
        ]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stack/compose")
def api_stack_compose():
    """Get the current docker-compose.yml content."""
    config_dir = _db_get("config_dir", "/docker")
    compose_path = os.path.join(config_dir, "docker-compose.yml")
    try:
        if os.path.exists(compose_path):
            with open(compose_path) as f:
                content = f.read()
            return jsonify({"content": content, "path": compose_path, "exists": True})
        return jsonify({"content": "", "path": compose_path, "exists": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    """Get current settings."""
    return jsonify({
        "config_dir": _db_get("config_dir", "/docker"),
        "media_dir": _db_get("media_dir", "/mnt/media"),
        "tz": _db_get("tz", "America/New_York"),
        "puid": _db_get("puid", "1000"),
        "pgid": _db_get("pgid", "1000"),
        "no_auth": _NO_AUTH,
        "token_hint": _db_get("token_raw_hint", "not set"),
        "version": "3.1.0"
    })

@app.route("/api/settings", methods=["POST"])
@require_auth
def api_settings_set():
    """Save settings."""
    data = request.json or {}
    allowed = ["config_dir", "media_dir", "tz", "puid", "pgid"]
    for key in allowed:
        if key in data:
            _db_set(key, data[key])
    return jsonify({"status": "saved"})

@app.route("/api/updates/check")
def api_updates_check():
    """Check which containers have image updates available."""
    if not DOCKER_OK:
        return jsonify({"error": "Docker not available"}), 500
    results = []
    try:
        containers = _dc.containers.list()
        for c in containers:
            image_tag = c.image.tags[0] if c.image.tags else "unknown"
            results.append({
                "name": c.name,
                "image": image_tag,
                "status": c.status,
                "update_available": False
            })
        return jsonify({"containers": results, "checked_at": datetime.now().isoformat()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/updates/pull/<cname>", methods=["POST"])
@require_auth
def api_update_container(cname):
    """Pull latest image for a container and recreate it."""
    if not DOCKER_OK:
        return jsonify({"error": "Docker not available"}), 500
    try:
        container = _dc.containers.get(cname)
        image = container.image.tags[0] if container.image.tags else None
        if not image:
            return jsonify({"error": "No image tag found"}), 400
        _dc.images.pull(image)
        container.restart()
        return jsonify({"status": "updated", "name": cname, "image": image})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/backup/create", methods=["POST"])
@require_auth
def api_backup_create():
    """Create a backup of the config directory."""
    config_dir = _db_get("config_dir", "/docker")
    backup_dir = "/data/backups"
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"deployrr_backup_{timestamp}.tar.gz"
    backup_path = os.path.join(backup_dir, backup_name)
    try:
        result = subprocess.run(
            ["tar", "-czf", backup_path, "-C", os.path.dirname(config_dir), os.path.basename(config_dir)],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            size = os.path.getsize(backup_path)
            return jsonify({"status": "success", "name": backup_name, "path": backup_path, "size": size})
        return jsonify({"error": result.stderr}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/backup/list")
def api_backup_list():
    """List available backups."""
    backup_dir = "/data/backups"
    backups = []
    if os.path.exists(backup_dir):
        for f in sorted(os.listdir(backup_dir), reverse=True):
            if f.endswith(".tar.gz"):
                path = os.path.join(backup_dir, f)
                backups.append({
                    "name": f,
                    "path": path,
                    "size": os.path.getsize(path),
                    "created": datetime.fromtimestamp(os.path.getctime(path)).isoformat()
                })
    return jsonify({"backups": backups})

@app.route("/api/auth/verify", methods=["POST"])
def api_auth_verify():
    """Verify token (returns 200 if valid, 401 if not)."""
    data = request.json or {}
    token = data.get("token", "")
    if _NO_AUTH:
        return jsonify({"valid": True, "no_auth": True})
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if token_hash == _get_token_hash():
        return jsonify({"valid": True})
    return jsonify({"valid": False}), 401

@app.route("/api/stacks")
def api_stacks():
    """Get available docker-compose stacks."""
    stacks = []
    try:
        for root, dirs, files in os.walk("/app/stacks"):
            for f in files:
                if f.endswith(".yml") or f.endswith(".yaml"):
                    stacks.append({"name": f, "path": os.path.join(root, f)})
    except Exception:
        pass
    return jsonify({"stacks": stacks})

@app.route("/api/stack/add", methods=["POST"])
def api_stack_add():
    """Add a new docker-compose stack."""
    try:
        data = request.json
        stack_name = data.get("name")
        stack_content = data.get("content")

        if not stack_name or not stack_content:
            return jsonify({"error": "Missing name or content"}), 400

        stack_dir = "/app/stacks"
        os.makedirs(stack_dir, exist_ok=True)

        stack_path = os.path.join(stack_dir, f"{stack_name}.yml")
        with open(stack_path, "w") as f:
            f.write(stack_content)

        return jsonify({"status": "added", "path": stack_path})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/update/check")
def api_update_check():
    """Check for Deployrr updates."""
    return jsonify({"update_available": False, "version": "3.1.0"})

@app.route("/api/update/all", methods=["POST"])
def api_update_all():
    """Update all containers."""
    if not DOCKER_OK:
        return jsonify({"error": "Docker not available"}), 500

    try:
        containers = _dc.containers.list()
        updated = []
        for c in containers:
            try:
                c.restart()
                updated.append(c.name)
            except Exception:
                pass
        return jsonify({"updated": updated})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/backup", methods=["POST"])
def api_backup():
    """Create system backup."""
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"/app/backups/backup_{timestamp}.tar.gz"

        os.makedirs("/app/backups", exist_ok=True)
        subprocess.run(["tar", "-czf", backup_path, "/app/stacks"], timeout=60)

        return jsonify({"status": "backed up", "path": backup_path})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/restore", methods=["POST"])
def api_restore():
    """Restore from backup."""
    try:
        data = request.json
        backup_path = data.get("path")

        if not backup_path:
            return jsonify({"error": "Missing backup path"}), 400

        subprocess.run(["tar", "-xzf", backup_path, "-C", "/"], timeout=60)
        return jsonify({"status": "restored"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/system/backups")
def api_system_backups():
    """List available backups."""
    backups = []
    try:
        backup_dir = "/app/backups"
        if os.path.exists(backup_dir):
            for f in os.listdir(backup_dir):
                if f.endswith(".tar.gz"):
                    path = os.path.join(backup_dir, f)
                    backups.append({"name": f, "path": path, "size": os.path.getsize(path)})
    except Exception:
        pass
    return jsonify({"backups": backups})

@app.route("/api/weather")
def api_weather():
    """Get 5-day weather forecast."""
    global _weather_cache

    try:
        # Check cache
        if _weather_cache["data"] and (time.time() - _weather_cache["ts"]) < CACHE_WEATHER:
            return jsonify(_weather_cache["data"])

        # Get location from ipapi.co
        geo_resp = requests.get("https://ipapi.co/json/", timeout=5)
        geo = geo_resp.json()
        lat, lon = geo.get("latitude", 0), geo.get("longitude", 0)

        # Get weather from open-meteo
        weather_resp = requests.get(
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum&timezone=auto",
            timeout=5
        )
        weather = weather_resp.json()

        result = {
            "location": f"{geo.get('city', 'Unknown')}, {geo.get('country_name', '')}",
            "daily": []
        }

        if "daily" in weather:
            daily = weather["daily"]
            for i in range(min(5, len(daily.get("time", [])))):
                result["daily"].append({
                    "date": daily["time"][i],
                    "code": int(daily["weather_code"][i]),
                    "icon": _wmo_to_icon(daily["weather_code"][i]),
                    "desc": _wmo_to_desc(daily["weather_code"][i]),
                    "temp_max": daily["temperature_2m_max"][i],
                    "temp_min": daily["temperature_2m_min"][i],
                    "precip": daily["precipitation_sum"][i]
                })

        _weather_cache["data"] = result
        _weather_cache["ts"] = time.time()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/rss")
def api_rss():
    """Fetch RSS feeds."""
    global _rss_cache

    try:
        # Check cache
        cache_key = "feeds"
        if cache_key in _rss_cache and (time.time() - _rss_cache[cache_key].get("ts", 0)) < CACHE_RSS:
            return jsonify(_rss_cache[cache_key]["data"])

        feeds = {
            "r/selfhosted": "https://www.reddit.com/r/selfhosted/.rss",
            "r/homelab": "https://www.reddit.com/r/homelab/.rss",
            "linuxserver.io": "https://blog.linuxserver.io/feed/",
            "noted.lol": "https://noted.lol/feed.xml",
            "selfh.st": "https://selfh.st/news/feed.xml"
        }

        result = {"sources": []}
        for name, url in feeds.items():
            try:
                resp = requests.get(url, timeout=5)
                root = ET.fromstring(resp.content)

                articles = []
                for item in root.findall(".//item"):
                    title_elem = item.find("title")
                    link_elem = item.find("link")
                    pubdate_elem = item.find("pubDate")

                    if title_elem is not None and link_elem is not None:
                        articles.append({
                            "title": title_elem.text or "Untitled",
                            "link": link_elem.text or "#",
                            "pubdate": pubdate_elem.text if pubdate_elem is not None else ""
                        })

                result["sources"].append({
                    "name": name,
                    "articles": articles[:5]  # Limit to 5 per source
                })
            except Exception:
                pass

        _rss_cache[cache_key] = {"data": result, "ts": time.time()}
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/widget/<app_id>")
def api_widget(app_id):
    """Get widget data for an application."""
    if app_id not in APP_REGISTRY:
        return jsonify({"error": "App not found"}), 404

    try:
        app_info = APP_REGISTRY[app_id]

        if not DOCKER_OK:
            return jsonify({
                "app_id": app_id,
                "name": app_info["name"],
                "status": "unknown",
                "cpu": 0,
                "memory": 0
            })

        containers = _dc.containers.list(all=True)
        container = None

        for c in containers:
            if app_id.lower() in c.name.lower() or app_info["name"].lower() in c.name.lower():
                container = c
                break

        if not container:
            return jsonify({
                "app_id": app_id,
                "name": app_info["name"],
                "status": "not_found",
                "cpu": 0,
                "memory": 0
            })

        is_running = container.attrs.get("State", {}).get("Running", False)
        cpu_percent = 0
        mem_mb = 0

        if is_running:
            try:
                stats = container.stats(stream=False)
                cpu_percent = _calc_cpu_percent(stats)
                mem_mb = stats.get("memory_stats", {}).get("usage", 0) / (1024 * 1024)
            except Exception:
                pass

        return jsonify({
            "app_id": app_id,
            "name": app_info["name"],
            "status": "running" if is_running else "stopped",
            "cpu": cpu_percent,
            "memory": mem_mb
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/widget_config", methods=["GET"])
def api_widget_config_get():
    """Get widget configuration."""
    config = _load_widget_config()
    return jsonify(config)

@app.route("/api/widget_config", methods=["POST"])
def api_widget_config_post():
    """Save widget configuration."""
    try:
        config = request.json
        _save_widget_config(config)
        return jsonify({"status": "saved"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/tailscale")
def api_tailscale():
    """Get Tailscale status."""
    if not DOCKER_OK:
        return jsonify({"error": "Docker not available"}), 500

    try:
        containers = _dc.containers.list(all=True)
        tailscale_container = None

        for c in containers:
            if "tailscale" in c.name.lower():
                tailscale_container = c
                break

        if not tailscale_container:
            return jsonify({"error": "Tailscale container not found"}), 404

        is_running = tailscale_container.attrs.get("State", {}).get("Running", False)
        result = {"status": "running" if is_running else "stopped", "container_name": tailscale_container.name}

        if is_running:
            try:
                status_output = tailscale_container.exec_run("tailscale status")
                result["output"] = status_output.output.decode('utf-8', errors='replace')
            except Exception:
                pass

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/home")
def api_home():
    """Home tab data: categorized apps with status and ports."""
    if not DOCKER_OK:
        return jsonify({"categories": []}), 500

    try:
        containers = _dc.containers.list(all=True)
        container_map = {c.name.lower(): c for c in containers}

        # Group apps by category
        categories = {}

        for app_id, app_info in APP_REGISTRY.items():
            cat = app_info.get("category", "MISC")
            if cat not in categories:
                categories[cat] = []

            # Find container
            container = None
            port = None
            is_running = False
            cpu_percent = 0
            mem_mb = 0

            for c_name, c in container_map.items():
                if app_id.lower() in c_name or app_info["name"].lower() in c_name:
                    container = c
                    is_running = c.attrs.get('State', {}).get('Running', False)

                    ports = c.ports or {}
                    if ports:
                        for pkey in ports:
                            if ports[pkey]:
                                port = ports[pkey][0].get('HostPort')
                                break

                    try:
                        stats = c.stats(stream=False)
                        cpu_percent = _calc_cpu_percent(stats)
                        mem_mb = stats.get('memory_stats', {}).get('usage', 0) / (1024 * 1024)
                    except Exception:
                        pass
                    break

            categories[cat].append({
                "app_id": app_id,
                "name": app_info["name"],
                "description": app_info.get("description", ""),
                "icon": app_info.get("icon", ""),
                "emoji": app_info.get("emoji", ""),
                "ports": app_info.get("ports", []),
                "port_mapped": port,
                "status": "running" if is_running else "stopped",
                "cpu_percent": cpu_percent,
                "memory_mb": mem_mb,
                "container_found": container is not None
            })

        return jsonify({"categories": categories})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =============================================================================
# HELPERS
# =============================================================================

def _format_uptime(seconds):
    """Format uptime as human-readable string."""
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{days}d {hours}h {minutes}m"

def _calc_cpu_percent(stats):
    """Calculate CPU percentage from Docker stats."""
    try:
        cpu_delta = stats.get('cpu_stats', {}).get('cpu_usage', {}).get('total_usage', 0) - stats.get('precpu_stats', {}).get('cpu_usage', {}).get('total_usage', 0)
        system_delta = stats.get('cpu_stats', {}).get('system_cpu_usage', 0) - stats.get('precpu_stats', {}).get('system_cpu_usage', 0)
        cpu_count = len(stats.get('cpu_stats', {}).get('cpu_usage', {}).get('percpu_usage', []))
        return (cpu_delta / system_delta * cpu_count * 100.0) if system_delta > 0 else 0
    except Exception:
        return 0

def _extract_ports(container):
    """Extract ports from container."""
    ports = []
    try:
        port_info = container.ports or {}
        for key, val in port_info.items():
            if val:
                ports.append({
                    "container_port": key,
                    "host_port": val[0].get("HostPort"),
                    "host_ip": val[0].get("HostIp")
                })
    except Exception:
        pass
    return ports

def _wmo_to_icon(code):
    """Convert WMO weather code to emoji icon."""
    code = int(code)
    if code == 0:
        return "☀️"
    elif code in (1, 2, 3):
        return "🌤️"
    elif code in (45, 48):
        return "🌫️"
    elif code in (51, 53, 55):
        return "🌦️"
    elif code in (61, 63, 65):
        return "🌧️"
    elif code in (71, 73, 75):
        return "❄️"
    elif code in (80, 81, 82):
        return "⛈️"
    elif code == 95:
        return "⛈️"
    else:
        return "🌤️"

def _wmo_to_desc(code):
    """Convert WMO weather code to description."""
    code = int(code)
    if code == 0:
        return "Clear"
    elif code in (1, 2, 3):
        return "Partly cloudy"
    elif code in (45, 48):
        return "Foggy"
    elif code in (51, 53, 55):
        return "Drizzle"
    elif code in (61, 63, 65):
        return "Rain"
    elif code in (71, 73, 75):
        return "Snow"
    elif code in (80, 81, 82):
        return "Showers"
    elif code == 95:
        return "Thunderstorm"
    else:
        return "Unknown"

def _load_widget_config():
    """Load widget configuration from JSON file."""
    config_path = "/app/widget_config.json"
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_widget_config(config):
    """Save widget configuration to JSON file."""
    config_path = "/app/widget_config.json"
    os.makedirs(os.path.dirname(config_path) or ".", exist_ok=True)
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)

# =============================================================================
# HTML SPA
# =============================================================================


_HTML_SPA = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Deployrr</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg:          #0f1117;
            --bg2:         #161822;
            --surface:     rgba(22, 27, 45, 0.65);
            --surface2:    rgba(30, 35, 55, 0.7);
            --glass:       rgba(255,255,255,0.03);
            --glass-border:rgba(255,255,255,0.08);
            --glass-hover: rgba(255,255,255,0.06);
            --border:      rgba(255,255,255,0.06);
            --border2:     rgba(255,255,255,0.12);
            --text:        #e2e8f0;
            --text2:       #94a3b8;
            --text3:       #64748b;
            --orange:      #f97316;
            --orange-dim:  rgba(249,115,22,0.15);
            --green:       #22c55e;
            --green-dim:   rgba(34,197,94,0.15);
            --cyan:        #06b6d4;
            --cyan-dim:    rgba(6,182,212,0.15);
            --red:         #ef4444;
            --red-dim:     rgba(239,68,68,0.15);
            --yellow:      #eab308;
            --yellow-dim:  rgba(234,179,8,0.15);
            --purple:      #8b5cf6;
            --purple-dim:  rgba(139,92,246,0.15);
            --blue:        #3b82f6;
            --mono:        'JetBrains Mono', monospace;
            --ui:          'Inter', -apple-system, sans-serif;
            --r:           16px;
            --r-sm:        10px;
            --sb-w:        240px;
            --blur:        12px;
        }

        * { margin:0; padding:0; box-sizing:border-box; }

        html, body {
            width:100%; height:100%;
            background: var(--bg);
            color: var(--text);
            font-family: var(--ui);
            overflow: hidden;
            font-size: 14px;
            -webkit-font-smoothing: antialiased;
        }

        /* Background gradient mesh */
        body::before {
            content:'';
            position:fixed; top:0; left:0; right:0; bottom:0;
            background:
                radial-gradient(ellipse at 20% 50%, rgba(249,115,22,0.06) 0%, transparent 50%),
                radial-gradient(ellipse at 80% 20%, rgba(6,182,212,0.04) 0%, transparent 50%),
                radial-gradient(ellipse at 50% 80%, rgba(139,92,246,0.03) 0%, transparent 50%);
            pointer-events:none;
            z-index:0;
        }

        #app {
            display:flex; width:100%; height:100%;
            position:relative; z-index:1;
        }

        /* ═══ GLASSMORPHISM BASE ═══ */
        .glass {
            background: var(--surface);
            backdrop-filter: blur(var(--blur));
            -webkit-backdrop-filter: blur(var(--blur));
            border: 1px solid var(--glass-border);
            border-radius: var(--r);
            transition: all 0.25s ease;
        }
        .glass:hover {
            background: var(--glass-hover);
            border-color: var(--border2);
        }

        /* ═══ SIDEBAR ═══ */
        #sidebar {
            width: var(--sb-w);
            background: rgba(15,17,23,0.85);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border-right: 1px solid var(--glass-border);
            display:flex; flex-direction:column;
            transition: width 0.2s ease;
            overflow:hidden;
            z-index:10;
        }
        #sidebar.collapsed { --sb-w:64px; }
        #sidebar.collapsed .ni-label,
        #sidebar.collapsed .ns-title,
        #sidebar.collapsed .sf { display:none; }

        .sb-toggle {
            padding:1rem 0.8rem;
            background:none; border:none;
            color:var(--text3); cursor:pointer;
            font-size:1.3rem;
            transition:color 0.2s;
        }
        .sb-toggle:hover { color:var(--orange); }

        .logo {
            padding:1.2rem 1.2rem 0.8rem;
            font-size:1.35rem;
            font-weight:700;
            display:flex; align-items:center; gap:0.6rem;
        }
        .logo-icon {
            width:32px; height:32px;
            background: linear-gradient(135deg, var(--orange), #fb923c);
            border-radius:8px;
            display:flex; align-items:center; justify-content:center;
            font-size:1rem;
        }

        .ns { flex:1; overflow-y:auto; padding:0.5rem 0; }
        .ns-title {
            padding:0.6rem 1.2rem 0.4rem;
            font-size:0.65rem;
            font-weight:600;
            color:var(--text3);
            text-transform:uppercase;
            letter-spacing:1.5px;
        }

        .ni {
            padding:0.7rem 1.2rem;
            cursor:pointer;
            display:flex; align-items:center;
            gap:0.75rem;
            color:var(--text3);
            transition:all 0.2s;
            border-left:3px solid transparent;
            font-size:0.9rem;
            font-weight:500;
        }
        .ni:hover {
            background:rgba(249,115,22,0.06);
            color:var(--text);
        }
        .ni.active {
            background:var(--orange-dim);
            border-left-color:var(--orange);
            color:var(--orange);
        }
        .ni-icon { font-size:1.15rem; min-width:22px; text-align:center; }
        .ni-label { flex:1; }

        .sf {
            border-top:1px solid var(--border);
            padding:0.8rem 1.2rem;
            font-size:0.75rem;
            color:var(--text3);
            font-family:var(--mono);
        }

        /* ═══ MAIN CONTENT ═══ */
        #main { flex:1; display:flex; flex-direction:column; overflow:hidden; }

        #topbar {
            background:rgba(15,17,23,0.6);
            backdrop-filter:blur(20px);
            -webkit-backdrop-filter:blur(20px);
            border-bottom:1px solid var(--glass-border);
            padding:0 1.5rem;
            display:flex; justify-content:space-between; align-items:center;
            height:56px;
            z-index:5;
        }
        .topbar-title { font-size:1.15rem; font-weight:600; }
        .topbar-right { display:flex; align-items:center; gap:1.5rem; }
        .live-badge {
            display:flex; align-items:center; gap:0.5rem;
            padding:0.35rem 0.8rem;
            background:var(--green-dim);
            border:1px solid rgba(34,197,94,0.2);
            border-radius:999px;
            font-size:0.75rem; font-weight:600; color:var(--green);
        }
        .live-dot {
            width:6px; height:6px;
            background:var(--green);
            border-radius:50%;
            animation:pulse 2s ease-in-out infinite;
        }
        @keyframes pulse {
            0%,100% { opacity:1; box-shadow:0 0 0 0 rgba(34,197,94,0.4); }
            50% { opacity:0.7; box-shadow:0 0 0 4px rgba(34,197,94,0); }
        }

        #content { flex:1; overflow-y:auto; padding:1.5rem; }

        /* ═══ TAB PANELS ═══ */
        .tab-panel { display:none; }
        .tab-panel.active { display:block; animation:fadeIn 0.2s ease; }
        @keyframes fadeIn { from{opacity:0;transform:translateY(4px)} to{opacity:1;transform:translateY(0)} }

        /* ═══ STAT CARDS (Overview) ═══ */
        .stats-grid {
            display:grid;
            grid-template-columns:repeat(4, 1fr);
            gap:1rem;
            margin-bottom:1.5rem;
        }
        .stat-card {
            background:var(--surface);
            backdrop-filter:blur(var(--blur));
            -webkit-backdrop-filter:blur(var(--blur));
            border:1px solid var(--glass-border);
            border-radius:var(--r);
            padding:1.5rem;
            position:relative;
            overflow:hidden;
            transition:all 0.3s ease;
        }
        .stat-card:hover {
            border-color:var(--border2);
            transform:translateY(-2px);
            box-shadow:0 8px 32px rgba(0,0,0,0.3);
        }
        .stat-card::before {
            content:'';
            position:absolute; top:0; left:0; right:0;
            height:3px;
            background:linear-gradient(90deg, var(--card-accent, var(--green)), transparent);
        }
        .stat-card .label {
            font-size:0.7rem;
            font-weight:600;
            color:var(--text3);
            text-transform:uppercase;
            letter-spacing:1px;
            margin-bottom:1rem;
        }
        .stat-card .value-row {
            display:flex; align-items:flex-end; justify-content:space-between;
        }
        .stat-card .value {
            font-size:2.2rem;
            font-weight:700;
            font-family:var(--mono);
            color:var(--text);
            line-height:1;
        }
        .stat-card .unit {
            font-size:0.85rem;
            color:var(--text3);
            font-weight:500;
            margin-left:0.3rem;
        }

        /* ═══ SVG CIRCULAR GAUGE ═══ */
        .gauge-container {
            display:flex; flex-direction:column;
            align-items:center; gap:0.5rem;
        }
        .gauge-svg { width:90px; height:90px; }
        .gauge-bg { fill:none; stroke:rgba(255,255,255,0.06); stroke-width:7; }
        .gauge-fill {
            fill:none; stroke-width:7;
            stroke-linecap:round;
            transition:stroke-dashoffset 0.6s ease, stroke 0.3s ease;
        }
        .gauge-text {
            font-family:var(--mono);
            font-size:15px;
            font-weight:700;
            fill:var(--text);
            text-anchor:middle;
            dominant-baseline:central;
        }

        /* ═══ PROGRESS BAR ═══ */
        .progress-bar {
            width:100%; height:6px;
            background:rgba(255,255,255,0.06);
            border-radius:3px;
            margin-top:1rem;
            overflow:hidden;
        }
        .progress-fill {
            height:100%; border-radius:3px;
            background:var(--green);
            transition:width 0.5s ease;
        }

        /* ═══ GRID LAYOUTS ═══ */
        .g4 { display:grid; grid-template-columns:repeat(4,1fr); gap:1rem; margin-bottom:1.5rem; }
        .g3 { display:grid; grid-template-columns:repeat(3,1fr); gap:1rem; margin-bottom:1.5rem; }
        .g2 { display:grid; grid-template-columns:repeat(2,1fr); gap:1rem; margin-bottom:1.5rem; }
        .g1 { display:grid; grid-template-columns:1fr; gap:1rem; margin-bottom:1.5rem; }

        /* ═══ SECTION HEADERS ═══ */
        .section-title {
            font-size:0.7rem;
            font-weight:600;
            color:var(--text3);
            text-transform:uppercase;
            letter-spacing:1.5px;
            margin:2rem 0 1rem;
            padding-bottom:0.75rem;
            border-bottom:1px solid var(--border);
        }

        /* ═══ GLASS CARDS ═══ */
        .card {
            background:var(--surface);
            backdrop-filter:blur(var(--blur));
            -webkit-backdrop-filter:blur(var(--blur));
            border:1px solid var(--glass-border);
            border-radius:var(--r);
            padding:1.2rem;
            transition:all 0.25s ease;
        }
        .card:hover {
            background:var(--glass-hover);
            border-color:var(--border2);
            box-shadow:0 4px 24px rgba(0,0,0,0.2);
        }
        .card-title {
            font-size:0.7rem; font-weight:600;
            color:var(--text3); text-transform:uppercase;
            letter-spacing:1px; margin-bottom:0.75rem;
        }
        .card-value {
            font-size:1.8rem; font-weight:700;
            color:var(--text); font-family:var(--mono);
        }
        .card-unit { font-size:0.85rem; color:var(--text3); margin-left:0.2rem; }
        .card-bar { width:100%; height:4px; background:rgba(255,255,255,0.06); border-radius:2px; margin-top:0.8rem; overflow:hidden; }
        .card-bar-fill { height:100%; background:var(--green); border-radius:2px; transition:width 0.3s; }

        /* ═══ LIST ═══ */
        .list {
            background:var(--surface);
            backdrop-filter:blur(var(--blur));
            -webkit-backdrop-filter:blur(var(--blur));
            border:1px solid var(--glass-border);
            border-radius:var(--r);
            overflow:hidden;
        }
        .list-item {
            padding:1rem 1.2rem;
            border-bottom:1px solid var(--border);
            display:flex; justify-content:space-between; align-items:center;
            transition:background 0.2s;
        }
        .list-item:last-child { border-bottom:none; }
        .list-item:hover { background:rgba(255,255,255,0.02); }
        .list-label { font-size:0.9rem; color:var(--text2); }
        .list-value { font-size:0.9rem; color:var(--text); font-family:var(--mono); }

        /* ═══ BADGES ═══ */
        .badge {
            display:inline-flex; align-items:center; gap:0.35rem;
            padding:0.3rem 0.7rem;
            border-radius:6px;
            font-size:0.7rem; font-weight:600;
            letter-spacing:0.5px;
        }
        .badge-green { background:var(--green-dim); color:var(--green); }
        .badge-red { background:var(--red-dim); color:var(--red); }
        .badge-orange { background:var(--orange-dim); color:var(--orange); }
        .badge-cyan { background:var(--cyan-dim); color:var(--cyan); }

        /* ═══ BUTTONS ═══ */
        .btn {
            padding:0.6rem 1.2rem;
            border:none; border-radius:var(--r-sm);
            font-family:var(--ui);
            font-size:0.85rem; font-weight:600;
            cursor:pointer;
            transition:all 0.25s ease;
            display:inline-flex; align-items:center; gap:0.5rem;
        }
        .btn-primary {
            background:linear-gradient(135deg, var(--orange), #fb923c);
            color:white;
            box-shadow:0 2px 8px rgba(249,115,22,0.25);
        }
        .btn-primary:hover {
            transform:translateY(-1px);
            box-shadow:0 4px 16px rgba(249,115,22,0.4);
        }
        .btn-secondary {
            background:var(--surface2);
            color:var(--text);
            border:1px solid var(--glass-border);
        }
        .btn-secondary:hover {
            border-color:var(--border2);
            background:rgba(255,255,255,0.08);
        }
        .btn-sm { padding:0.4rem 0.8rem; font-size:0.8rem; }

        /* ═══ FEED CARDS ═══ */
        .feed-card {
            background:var(--surface);
            backdrop-filter:blur(var(--blur));
            border:1px solid var(--glass-border);
            border-radius:var(--r);
            padding:1.2rem;
            margin-bottom:0.8rem;
        }
        .feed-source {
            font-size:0.7rem; color:var(--orange);
            font-weight:600; margin-bottom:0.5rem;
            text-transform:uppercase; letter-spacing:0.5px;
        }
        .feed-item { margin-bottom:0.6rem; }
        .feed-item:last-child { margin-bottom:0; }
        .feed-title { font-size:0.9rem; color:var(--text); margin-bottom:0.3rem; }
        .feed-title a { color:var(--cyan); text-decoration:none; transition:color 0.2s; }
        .feed-title a:hover { color:var(--orange); }
        .feed-time { font-size:0.75rem; color:var(--text3); }

        /* ═══ WEATHER ═══ */
        .weather-day {
            background:var(--surface);
            backdrop-filter:blur(var(--blur));
            border:1px solid var(--glass-border);
            border-radius:var(--r);
            padding:1.2rem; text-align:center;
        }
        .weather-icon { font-size:2rem; margin-bottom:0.5rem; }
        .weather-date { font-size:0.8rem; color:var(--text2); margin-bottom:0.5rem; }
        .weather-temps { font-size:1.3rem; font-weight:700; color:var(--text); margin-bottom:0.5rem; }
        .weather-desc { font-size:0.85rem; color:var(--text2); }

        /* ═══ TAILSCALE ═══ */
        .peer-list {
            background:var(--surface);
            backdrop-filter:blur(var(--blur));
            border:1px solid var(--glass-border);
            border-radius:var(--r);
            padding:1rem;
        }
        .peer-item {
            padding:0.8rem;
            border-bottom:1px solid var(--border);
            display:flex; justify-content:space-between; align-items:center;
        }
        .peer-item:last-child { border-bottom:none; }
        .peer-name { font-weight:600; color:var(--text); }
        .peer-status {
            font-size:0.7rem; padding:0.3rem 0.7rem;
            background:var(--green-dim);
            border-radius:6px; color:var(--green);
            font-weight:600;
        }
        .peer-status.offline { background:var(--red-dim); color:var(--red); }

        .quick-commands { margin-top:1.2rem; }
        .cmd-btn {
            display:block; width:100%;
            padding:0.8rem;
            background:var(--surface);
            backdrop-filter:blur(var(--blur));
            border:1px solid var(--glass-border);
            border-radius:var(--r-sm);
            color:var(--text); cursor:pointer;
            text-align:left;
            font-family:var(--mono); font-size:0.85rem;
            margin-bottom:0.5rem;
            transition:all 0.2s;
        }
        .cmd-btn:hover {
            border-color:var(--orange);
            background:var(--orange-dim);
            color:var(--orange);
        }

        /* ═══ LOADING ═══ */
        .loading {
            display:inline-block;
            width:1rem; height:1rem;
            border:2px solid rgba(255,255,255,0.06);
            border-top-color:var(--orange);
            border-radius:50%;
            animation:spin 1s linear infinite;
        }
        @keyframes spin { to{transform:rotate(360deg)} }

        /* ═══ AUTH LOGIN ═══ */
        #login-overlay {
            position:fixed; top:0; left:0; width:100%; height:100%;
            background:rgba(15,17,23,0.95);
            backdrop-filter:blur(20px);
            z-index:9999;
            display:flex; align-items:center; justify-content:center;
            flex-direction:column; gap:1.5rem;
        }
        #login-overlay.hidden { display:none; }

        /* ═══ MOBILE ═══ */
        @media (max-width:768px) {
            #sidebar { position:fixed; left:0; top:0; height:100%; z-index:100; transform:translateX(-100%); transition:transform 0.3s; }
            #sidebar.open { transform:translateX(0); }
            #sidebar.collapsed { --sb-w:100%; }
            #overlay { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index:99; }
            #overlay.show { display:block; }
            #content { padding:1rem; }
            .stats-grid, .g4 { grid-template-columns:repeat(2,1fr); }
            .g3, .g2 { grid-template-columns:1fr; }
            #topbar { padding:0 1rem; }
        }

        /* ═══ SCROLLBAR ═══ */
        ::-webkit-scrollbar { width:6px; }
        ::-webkit-scrollbar-track { background:transparent; }
        ::-webkit-scrollbar-thumb { background:rgba(255,255,255,0.08); border-radius:3px; }
        ::-webkit-scrollbar-thumb:hover { background:rgba(255,255,255,0.15); }
    </style>
</head>
<body>
    <!-- Auth Login Overlay -->
    <div id="login-overlay" class="hidden">
        <div style="width:64px;height:64px;background:linear-gradient(135deg,var(--orange),#fb923c);border-radius:16px;display:flex;align-items:center;justify-content:center;font-size:1.8rem;margin-bottom:0.5rem;">🚀</div>
        <div style="font-size:1.8rem;font-weight:700;color:var(--text);">Deployrr</div>
        <div style="color:var(--text3);font-size:0.9rem;">Enter your access token to continue</div>
        <div style="display:flex;gap:0.75rem;width:100%;max-width:420px;">
            <input id="token-input" type="password" placeholder="Your token..." style="flex:1;padding:0.75rem 1rem;background:var(--surface);backdrop-filter:blur(12px);border:1px solid var(--glass-border);border-radius:var(--r-sm);color:var(--text);font-family:var(--mono);font-size:0.9rem;outline:none;" />
            <button onclick="tryLogin()" class="btn btn-primary" style="padding:0.75rem 1.5rem;">Login</button>
        </div>
        <div id="login-error" style="color:var(--red);font-size:0.85rem;display:none;">Invalid token. Check container logs for your token.</div>
    </div>

    <div id="app">
        <div id="overlay"></div>

        <!-- SIDEBAR -->
        <div id="sidebar">
            <div class="logo">
                <div class="logo-icon">🚀</div>
                <span>Deploy<span style="color:var(--orange);">rr</span></span>
            </div>

            <div class="ns">
                <div class="ns-title">Monitor</div>
                <div class="ni active" onclick="switchTab(event,'overview')">
                    <div class="ni-icon">📊</div><div class="ni-label">Overview</div>
                </div>
                <div class="ni" onclick="switchTab(event,'storage')">
                    <div class="ni-icon">💾</div><div class="ni-label">Storage</div>
                </div>
                <div class="ni" onclick="switchTab(event,'network')">
                    <div class="ni-icon">🌐</div><div class="ni-label">Network</div>
                </div>
                <div class="ni" onclick="switchTab(event,'containers')">
                    <div class="ni-icon">🐳</div><div class="ni-label">Containers</div>
                </div>
                <div class="ni" onclick="switchTab(event,'hardware')">
                    <div class="ni-icon">🖥️</div><div class="ni-label">Hardware</div>
                </div>
                <div class="ni" onclick="switchTab(event,'logs')">
                    <div class="ni-icon">📋</div><div class="ni-label">Logs</div>
                </div>

                <div class="ns-title">Feeds</div>
                <div class="ni" onclick="switchTab(event,'rss')">
                    <div class="ni-icon">📡</div><div class="ni-label">RSS</div>
                </div>
                <div class="ni" onclick="switchTab(event,'weather')">
                    <div class="ni-icon">🌤️</div><div class="ni-label">Weather</div>
                </div>
                <div class="ni" onclick="switchTab(event,'tailscale')">
                    <div class="ni-icon">🔐</div><div class="ni-label">Tailscale</div>
                </div>

                <div class="ns-title">Manage</div>
                <div class="ni" onclick="switchTab(event,'deploy')">
                    <div class="ni-icon">🚀</div><div class="ni-label">Deploy</div>
                </div>
                <div class="ni" onclick="switchTab(event,'stack')">
                    <div class="ni-icon">📦</div><div class="ni-label">Stack</div>
                </div>
                <div class="ni" onclick="switchTab(event,'updates')">
                    <div class="ni-icon">🔄</div><div class="ni-label">Updates</div>
                </div>
                <div class="ni" onclick="switchTab(event,'backup')">
                    <div class="ni-icon">💾</div><div class="ni-label">Backup</div>
                </div>
                <div class="ni" onclick="switchTab(event,'settings')">
                    <div class="ni-icon">⚙️</div><div class="ni-label">Settings</div>
                </div>
            </div>

            <div class="sf">v3.1.0</div>
        </div>

        <!-- MAIN CONTENT -->
        <div id="main">
            <div id="topbar">
                <div style="display:flex;align-items:center;gap:1rem;">
                    <button class="sb-toggle" onclick="toggleSidebar()" style="display:none;padding:0;font-size:1.2rem;">☰</button>
                    <div class="topbar-title" id="title">Overview</div>
                </div>
                <div class="topbar-right">
                    <div class="live-badge" id="live-badge"><div class="live-dot"></div>Live</div>
                    <span style="font-family:var(--mono);font-size:0.85rem;color:var(--text2);" id="time">--:--</span>
                </div>
            </div>

            <div id="content">
                <!-- OVERVIEW TAB -->
                <div id="overview" class="tab-panel active">
                    <div class="stats-grid">
                        <div class="stat-card" style="--card-accent:var(--cyan);">
                            <div class="label">CPU Usage</div>
                            <div class="value-row">
                                <div>
                                    <span class="value" id="cpu-val">--</span><span class="unit">%</span>
                                </div>
                                <svg class="gauge-svg" viewBox="0 0 100 100" id="cpu-gauge">
                                    <circle class="gauge-bg" cx="50" cy="50" r="40"/>
                                    <circle class="gauge-fill" cx="50" cy="50" r="40"
                                        stroke-dasharray="251.3" stroke-dashoffset="251.3"
                                        stroke="var(--cyan)" id="cpu-ring"/>
                                    <text class="gauge-text" x="50" y="50" id="cpu-gauge-text">--%</text>
                                </svg>
                            </div>
                            <div class="progress-bar"><div class="progress-fill" id="cpu-bar" style="background:var(--cyan);"></div></div>
                        </div>

                        <div class="stat-card" style="--card-accent:var(--purple);">
                            <div class="label">Memory</div>
                            <div class="value-row">
                                <div>
                                    <span class="value" id="mem-val">--</span><span class="unit">%</span>
                                </div>
                                <svg class="gauge-svg" viewBox="0 0 100 100" id="mem-gauge">
                                    <circle class="gauge-bg" cx="50" cy="50" r="40"/>
                                    <circle class="gauge-fill" cx="50" cy="50" r="40"
                                        stroke-dasharray="251.3" stroke-dashoffset="251.3"
                                        stroke="var(--purple)" id="mem-ring"/>
                                    <text class="gauge-text" x="50" y="50" id="mem-gauge-text">--%</text>
                                </svg>
                            </div>
                            <div class="progress-bar"><div class="progress-fill" id="mem-bar" style="background:var(--purple);"></div></div>
                        </div>

                        <div class="stat-card" style="--card-accent:var(--orange);">
                            <div class="label">Load (1m)</div>
                            <div class="value-row">
                                <div>
                                    <span class="value" id="load-val">--</span>
                                </div>
                                <svg class="gauge-svg" viewBox="0 0 100 100" id="load-gauge">
                                    <circle class="gauge-bg" cx="50" cy="50" r="40"/>
                                    <circle class="gauge-fill" cx="50" cy="50" r="40"
                                        stroke-dasharray="251.3" stroke-dashoffset="251.3"
                                        stroke="var(--orange)" id="load-ring"/>
                                    <text class="gauge-text" x="50" y="50" id="load-gauge-text">--</text>
                                </svg>
                            </div>
                            <div class="progress-bar"><div class="progress-fill" id="load-bar" style="background:var(--orange);"></div></div>
                        </div>

                        <div class="stat-card" style="--card-accent:var(--green);">
                            <div class="label">Uptime</div>
                            <div class="value-row">
                                <div>
                                    <span class="value" id="uptime-val" style="font-size:1.6rem;">--</span>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- STORAGE TAB -->
                <div id="storage" class="tab-panel">
                    <h3 class="section-title">Disk Usage</h3>
                    <div id="storage-list" class="list"></div>
                </div>

                <!-- NETWORK TAB -->
                <div id="network" class="tab-panel">
                    <h3 class="section-title">Network Interfaces</h3>
                    <div id="network-list" class="list"></div>
                    <h3 class="section-title">Network Stats</h3>
                    <div class="g2">
                        <div class="card">
                            <div class="card-title">Bytes Received</div>
                            <div class="card-value" id="bytes-recv" style="font-size:1.2rem;">-- GB</div>
                        </div>
                        <div class="card">
                            <div class="card-title">Bytes Sent</div>
                            <div class="card-value" id="bytes-sent" style="font-size:1.2rem;">-- GB</div>
                        </div>
                    </div>
                </div>

                <!-- CONTAINERS TAB -->
                <div id="containers" class="tab-panel">
                    <h3 class="section-title">Running Containers</h3>
                    <div id="containers-list" class="list"></div>
                </div>

                <!-- HARDWARE TAB -->
                <div id="hardware" class="tab-panel">
                    <div class="g2">
                        <div class="card">
                            <div class="card-title">CPU Cores</div>
                            <div class="card-value" id="hw-cores">--</div>
                        </div>
                        <div class="card">
                            <div class="card-title">CPU Frequency</div>
                            <div class="card-value" id="hw-freq" style="font-size:1.2rem;">-- GHz</div>
                        </div>
                    </div>
                    <h3 class="section-title">Memory</h3>
                    <div class="g2">
                        <div class="card">
                            <div class="card-title">Total Memory</div>
                            <div class="card-value" id="hw-mem-total" style="font-size:1.2rem;">-- GB</div>
                        </div>
                        <div class="card">
                            <div class="card-title">System Info</div>
                            <div class="card-value" id="hw-system" style="font-size:0.9rem;">--</div>
                        </div>
                    </div>
                </div>

                <!-- LOGS TAB -->
                <div id="logs" class="tab-panel">
                    <h3 class="section-title">System Logs</h3>
                    <div style="background:var(--surface);backdrop-filter:blur(var(--blur));border:1px solid var(--glass-border);border-radius:var(--r);padding:1rem;font-family:var(--mono);font-size:0.8rem;color:var(--text2);max-height:400px;overflow-y:auto;white-space:pre-wrap;word-break:break-word;" id="logs-output">Loading...</div>
                </div>

                <!-- RSS TAB -->
                <div id="rss" class="tab-panel">
                    <h3 class="section-title">Latest News</h3>
                    <div id="rss-feeds"></div>
                </div>

                <!-- WEATHER TAB -->
                <div id="weather" class="tab-panel">
                    <h3 class="section-title">5-Day Forecast</h3>
                    <div id="weather-cards" class="g3"></div>
                </div>

                <!-- TAILSCALE TAB -->
                <div id="tailscale" class="tab-panel">
                    <h3 class="section-title">Tailscale Status</h3>
                    <div class="card" style="margin-bottom:1.2rem;">
                        <div class="card-title">Connection Status</div>
                        <div id="tailscale-status" style="margin-top:0.5rem;">Checking...</div>
                    </div>
                    <h3 class="section-title">Peers</h3>
                    <div id="tailscale-peers" class="peer-list">Checking...</div>
                    <div class="quick-commands">
                        <h3 class="section-title">Quick Commands</h3>
                        <button class="cmd-btn" onclick="copyToClipboard('tailscale ip -4')">tailscale ip -4</button>
                        <button class="cmd-btn" onclick="copyToClipboard('tailscale status')">tailscale status</button>
                        <button class="cmd-btn" onclick="copyToClipboard('tailscale logout')">tailscale logout</button>
                    </div>
                </div>

                <!-- DEPLOY TAB -->
                <div id="deploy" class="tab-panel">
                    <div style="display:flex;gap:1rem;margin-bottom:1.5rem;align-items:center;flex-wrap:wrap;">
                        <input id="deploy-search" type="text" placeholder="Search apps..."
                               style="flex:1;min-width:200px;padding:0.7rem 1rem;background:var(--surface);backdrop-filter:blur(12px);border:1px solid var(--glass-border);border-radius:var(--r-sm);color:var(--text);font-size:0.9rem;outline:none;"
                               oninput="filterCatalog()" />
                        <select id="deploy-category" onchange="filterCatalog()"
                                style="padding:0.7rem 1rem;background:var(--surface);border:1px solid var(--glass-border);border-radius:var(--r-sm);color:var(--text);font-size:0.9rem;">
                            <option value="">All Categories</option>
                        </select>
                    </div>
                    <div id="deploy-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1rem;"></div>
                </div>

                <!-- STACK TAB -->
                <div id="stack" class="tab-panel">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;">
                        <h3 style="font-size:1rem;font-weight:600;">Active Compose Stack</h3>
                        <button onclick="loadStackCompose()" class="btn btn-secondary btn-sm">Refresh</button>
                    </div>
                    <div id="stack-compose" style="background:var(--surface);backdrop-filter:blur(12px);border:1px solid var(--glass-border);border-radius:var(--r);padding:1.2rem;font-family:var(--mono);font-size:0.8rem;white-space:pre-wrap;overflow-x:auto;max-height:60vh;overflow-y:auto;color:var(--text2);">Loading...</div>
                    <div style="margin-top:1.5rem;">
                        <h3 style="font-size:1rem;font-weight:600;margin-bottom:0.75rem;">Deploy History</h3>
                        <div id="deploy-history" style="display:flex;flex-direction:column;gap:0.5rem;"></div>
                    </div>
                </div>

                <!-- UPDATES TAB -->
                <div id="updates" class="tab-panel">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;">
                        <h3 style="font-size:1rem;font-weight:600;">Container Updates</h3>
                        <button onclick="checkUpdates()" class="btn btn-primary btn-sm">Check Updates</button>
                    </div>
                    <div id="updates-list" style="display:flex;flex-direction:column;gap:0.75rem;"></div>
                </div>

                <!-- BACKUP TAB -->
                <div id="backup" class="tab-panel">
                    <div style="display:flex;gap:1rem;margin-bottom:1.5rem;flex-wrap:wrap;">
                        <button onclick="createBackup()" class="btn btn-primary">💾 Create Backup</button>
                        <button onclick="loadBackups()" class="btn btn-secondary">Refresh</button>
                    </div>
                    <div id="backup-status" style="margin-bottom:1rem;font-size:0.9rem;color:var(--text2);"></div>
                    <div id="backup-list" style="display:flex;flex-direction:column;gap:0.75rem;"></div>
                </div>

                <!-- SETTINGS TAB -->
                <div id="settings" class="tab-panel">
                    <div style="max-width:600px;">
                        <h3 class="section-title">Configuration</h3>
                        <div style="display:flex;flex-direction:column;gap:1.2rem;">
                            <div>
                                <label style="display:block;font-size:0.7rem;color:var(--text3);margin-bottom:0.5rem;text-transform:uppercase;letter-spacing:1px;font-weight:600;">Config Directory</label>
                                <input id="setting-config_dir" type="text" placeholder="/docker"
                                       style="width:100%;padding:0.7rem 1rem;background:var(--surface);border:1px solid var(--glass-border);border-radius:var(--r-sm);color:var(--text);font-family:var(--mono);outline:none;" />
                            </div>
                            <div>
                                <label style="display:block;font-size:0.7rem;color:var(--text3);margin-bottom:0.5rem;text-transform:uppercase;letter-spacing:1px;font-weight:600;">Media Directory</label>
                                <input id="setting-media_dir" type="text" placeholder="/mnt/media"
                                       style="width:100%;padding:0.7rem 1rem;background:var(--surface);border:1px solid var(--glass-border);border-radius:var(--r-sm);color:var(--text);font-family:var(--mono);outline:none;" />
                            </div>
                            <div>
                                <label style="display:block;font-size:0.7rem;color:var(--text3);margin-bottom:0.5rem;text-transform:uppercase;letter-spacing:1px;font-weight:600;">Timezone</label>
                                <input id="setting-tz" type="text" placeholder="America/New_York"
                                       style="width:100%;padding:0.7rem 1rem;background:var(--surface);border:1px solid var(--glass-border);border-radius:var(--r-sm);color:var(--text);font-family:var(--mono);outline:none;" />
                            </div>
                            <div style="display:flex;gap:1rem;">
                                <div style="flex:1;">
                                    <label style="display:block;font-size:0.7rem;color:var(--text3);margin-bottom:0.5rem;text-transform:uppercase;letter-spacing:1px;font-weight:600;">PUID</label>
                                    <input id="setting-puid" type="text" placeholder="1000"
                                           style="width:100%;padding:0.7rem 1rem;background:var(--surface);border:1px solid var(--glass-border);border-radius:var(--r-sm);color:var(--text);font-family:var(--mono);outline:none;" />
                                </div>
                                <div style="flex:1;">
                                    <label style="display:block;font-size:0.7rem;color:var(--text3);margin-bottom:0.5rem;text-transform:uppercase;letter-spacing:1px;font-weight:600;">PGID</label>
                                    <input id="setting-pgid" type="text" placeholder="1000"
                                           style="width:100%;padding:0.7rem 1rem;background:var(--surface);border:1px solid var(--glass-border);border-radius:var(--r-sm);color:var(--text);font-family:var(--mono);outline:none;" />
                                </div>
                            </div>
                            <div>
                                <button onclick="saveSettings()" class="btn btn-primary">Save Settings</button>
                                <span id="settings-status" style="margin-left:1rem;font-size:0.85rem;color:var(--green);"></span>
                            </div>
                        </div>
                        <div style="margin-top:2rem;padding-top:1.5rem;border-top:1px solid var(--border);">
                            <h3 class="section-title">About</h3>
                            <div style="font-size:0.9rem;color:var(--text2);line-height:2;">
                                <div>Version: <span style="color:var(--orange);font-family:var(--mono);font-weight:600;">3.1.0</span></div>
                                <div>Auth: <span id="settings-auth-status" style="color:var(--green);">checking...</span></div>
                                <div>Token hint: <span id="settings-token-hint" style="font-family:var(--mono);color:var(--text);">...</span></div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        let _token = localStorage.getItem('deployrr_token') || '';
        const API = '';

        // ═══ SVG GAUGE HELPER ═══
        function updateGauge(ringId, textId, value, max) {
            const pct = Math.min(value / max * 100, 100);
            const circumference = 2 * Math.PI * 40; // r=40
            const offset = circumference - (pct / 100) * circumference;
            const ring = document.getElementById(ringId);
            const text = document.getElementById(textId);
            if (ring) {
                ring.style.strokeDashoffset = offset;
                // Color thresholds like PegaProx
                if (pct < 50) ring.style.stroke = 'var(--green)';
                else if (pct < 80) ring.style.stroke = 'var(--yellow)';
                else ring.style.stroke = 'var(--red)';
            }
            if (text) text.textContent = Math.round(pct) + '%';
        }

        // Auth management
        async function tryLogin() {
            const token = document.getElementById('token-input').value.trim();
            const res = await fetch('/api/auth/verify', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({token})
            });
            if (res.ok) {
                _token = token;
                localStorage.setItem('deployrr_token', token);
                document.getElementById('login-overlay').classList.add('hidden');
            } else {
                document.getElementById('login-error').style.display = 'block';
            }
        }

        async function checkAuth() {
            if (!_token) {
                const res = await fetch('/api/auth/verify', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({token: ''})
                });
                if (res.ok) return;
                document.getElementById('login-overlay').classList.remove('hidden');
            } else {
                const res = await fetch('/api/auth/verify', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({token: _token})
                });
                if (!res.ok) {
                    localStorage.removeItem('deployrr_token');
                    _token = '';
                    document.getElementById('login-overlay').classList.remove('hidden');
                }
            }
        }

        function authHeaders() {
            return _token ? {'Authorization': `Bearer ${_token}`, 'Content-Type': 'application/json'} : {'Content-Type': 'application/json'};
        }

        // Time update
        function updateTime() {
            const now = new Date();
            const h = String(now.getHours()).padStart(2, '0');
            const m = String(now.getMinutes()).padStart(2, '0');
            document.getElementById('time').textContent = `${h}:${m}`;
        }
        setInterval(updateTime, 1000);
        updateTime();

        // Tab switching
        let currentTab = 'overview';
        function switchTab(e, tab) {
            e.preventDefault();
            const sidebar = document.getElementById('sidebar');
            const overlay = document.getElementById('overlay');
            if (window.innerWidth <= 768) {
                sidebar.classList.remove('open');
                overlay.classList.remove('show');
            }
            document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.ni').forEach(n => n.classList.remove('active'));
            document.getElementById(tab).classList.add('active');
            e.currentTarget.classList.add('active');
            currentTab = tab;
            document.getElementById('title').textContent = {
                overview:'Overview', storage:'Storage', network:'Network',
                containers:'Containers', hardware:'Hardware', logs:'Logs',
                rss:'RSS Feeds', weather:'Weather', tailscale:'Tailscale',
                deploy:'Deploy Apps', stack:'Stack Manager', updates:'Updates',
                backup:'Backup', settings:'Settings'
            }[tab] || tab;
            if (tab === 'overview') loadOverview();
            else if (tab === 'storage') loadStorage();
            else if (tab === 'network') loadNetwork();
            else if (tab === 'containers') loadContainers();
            else if (tab === 'hardware') loadHardware();
            else if (tab === 'logs') loadLogs();
            else if (tab === 'rss') loadRSS();
            else if (tab === 'weather') loadWeather();
            else if (tab === 'tailscale') loadTailscale();
            else if (tab === 'deploy') loadCatalog();
            else if (tab === 'stack') loadStackCompose();
            else if (tab === 'backup') loadBackups();
            else if (tab === 'settings') loadSettings();
        }

        function toggleSidebar() {
            document.getElementById('sidebar').classList.toggle('collapsed');
        }

        document.getElementById('overlay')?.addEventListener('click', () => {
            document.getElementById('sidebar').classList.remove('open');
            document.getElementById('overlay').classList.remove('show');
        });

        // Overview
        async function loadOverview() {
            try {
                const res = await fetch(`${API}/api/overview`);
                const data = await res.json();
                const cpu = Math.round(data.cpu_percent);
                const mem = Math.round(data.memory.percent);
                const load = data.load_avg['1m'];

                document.getElementById('cpu-val').textContent = cpu;
                document.getElementById('cpu-bar').style.width = cpu + '%';
                updateGauge('cpu-ring', 'cpu-gauge-text', cpu, 100);

                document.getElementById('mem-val').textContent = mem;
                document.getElementById('mem-bar').style.width = mem + '%';
                updateGauge('mem-ring', 'mem-gauge-text', mem, 100);

                document.getElementById('load-val').textContent = load.toFixed(2);
                document.getElementById('load-bar').style.width = Math.min(load * 25, 100) + '%';
                updateGauge('load-ring', 'load-gauge-text', load, 4);

                document.getElementById('uptime-val').textContent = data.uptime_display;
            } catch (e) { console.error(e); }
        }

        // Storage
        async function loadStorage() {
            try {
                const res = await fetch(`${API}/api/storage`);
                const data = await res.json();
                const list = document.getElementById('storage-list');
                list.innerHTML = '';
                for (const [mount, info] of Object.entries(data.disks || {})) {
                    const pct = info.percent || 0;
                    const col = pct < 60 ? 'var(--green)' : pct < 80 ? 'var(--yellow)' : 'var(--red)';
                    const bg = pct < 60 ? 'var(--green-dim)' : pct < 80 ? 'var(--yellow-dim)' : 'var(--red-dim)';
                    list.innerHTML += `
                        <div class="list-item" style="flex-wrap:wrap;gap:0.5rem;">
                            <div style="flex:1;min-width:150px;">
                                <div style="font-weight:600;color:var(--text);font-family:var(--mono);">${mount}</div>
                                <div style="font-size:0.8rem;color:var(--text3);">${info.fstype}</div>
                            </div>
                            <div style="display:flex;align-items:center;gap:1rem;">
                                <span style="font-size:0.85rem;color:var(--text2);font-family:var(--mono);">${(info.used/1073741824).toFixed(1)} / ${(info.total/1073741824).toFixed(1)} GB</span>
                                <span class="badge" style="background:${bg};color:${col};">${pct.toFixed(0)}%</span>
                            </div>
                            <div style="width:100%;height:4px;background:rgba(255,255,255,0.06);border-radius:2px;overflow:hidden;">
                                <div style="height:100%;width:${pct}%;background:${col};border-radius:2px;transition:width 0.3s;"></div>
                            </div>
                        </div>`;
                }
            } catch (e) { console.error(e); }
        }

        // Network
        async function loadNetwork() {
            try {
                const res = await fetch(`${API}/api/network`);
                const data = await res.json();
                const list = document.getElementById('network-list');
                list.innerHTML = '';
                for (const [iface, addrs] of Object.entries(data.interfaces || {})) {
                    for (const addr of addrs) {
                        list.innerHTML += `
                            <div class="list-item">
                                <div>
                                    <div style="font-weight:600;color:var(--text);font-family:var(--mono);">${iface}</div>
                                    <div style="font-size:0.85rem;color:var(--text3);">${addr.address}</div>
                                </div>
                                <span class="badge badge-cyan">${addr.family || 'IPv4'}</span>
                            </div>`;
                    }
                }
                document.getElementById('bytes-recv').textContent = (data.io.bytes_recv/1073741824).toFixed(2) + ' GB';
                document.getElementById('bytes-sent').textContent = (data.io.bytes_sent/1073741824).toFixed(2) + ' GB';
            } catch (e) { console.error(e); }
        }

        // Containers
        async function loadContainers() {
            try {
                const res = await fetch(`${API}/api/containers`);
                const data = await res.json();
                const list = document.getElementById('containers-list');
                list.innerHTML = '';
                for (const c of data.containers || []) {
                    const running = c.state.Running;
                    list.innerHTML += `
                        <div class="list-item">
                            <div>
                                <div style="font-weight:600;color:var(--text);font-family:var(--mono);">${c.name}</div>
                                <div style="font-size:0.8rem;color:var(--text3);margin-top:0.2rem;">${c.image}</div>
                            </div>
                            <span class="badge ${running ? 'badge-green' : 'badge-red'}">${running ? 'RUNNING' : 'STOPPED'}</span>
                        </div>`;
                }
            } catch (e) { console.error(e); }
        }

        // Hardware
        async function loadHardware() {
            try {
                const res = await fetch(`${API}/api/hardware`);
                const data = await res.json();
                document.getElementById('hw-cores').textContent = data.cpu.count;
                document.getElementById('hw-freq').textContent = ((data.cpu.freq?.current || 0) / 1000).toFixed(2) + ' GHz';
                document.getElementById('hw-mem-total').textContent = (data.memory.total / 1073741824).toFixed(1) + ' GB';
                document.getElementById('hw-system').textContent = `${data.platform.system} ${data.platform.release}`;
            } catch (e) { console.error(e); }
        }

        // Logs
        async function loadLogs() {
            try {
                const res = await fetch(`${API}/api/logs?lines=50`);
                const data = await res.json();
                document.getElementById('logs-output').textContent = data.logs || 'No logs';
            } catch (e) { console.error(e); }
        }

        // RSS
        async function loadRSS() {
            try {
                const res = await fetch(`${API}/api/rss`);
                const data = await res.json();
                const feeds = document.getElementById('rss-feeds');
                feeds.innerHTML = '';
                for (const source of data.sources || []) {
                    let html = `<div class="feed-card"><div class="feed-source">${source.name}</div>`;
                    for (const article of source.articles) {
                        html += `<div class="feed-item"><div class="feed-title"><a href="${article.link}" target="_blank">${article.title}</a></div><div class="feed-time">${article.pubdate.split('T')[0] || ''}</div></div>`;
                    }
                    html += '</div>';
                    feeds.innerHTML += html;
                }
            } catch (e) { console.error(e); }
        }

        // Weather
        async function loadWeather() {
            try {
                const res = await fetch(`${API}/api/weather`);
                const data = await res.json();
                const cards = document.getElementById('weather-cards');
                cards.innerHTML = '';
                for (const day of data.daily || []) {
                    const date = new Date(day.date).toLocaleDateString('en-US', { weekday:'short', month:'short', day:'numeric' });
                    cards.innerHTML += `<div class="weather-day"><div class="weather-icon">${day.icon}</div><div class="weather-date">${date}</div><div class="weather-temps">${Math.round(day.temp_max)}°</div><div style="font-size:0.85rem;color:var(--text2);">${Math.round(day.temp_min)}°</div><div class="weather-desc">${day.desc}</div><div class="weather-desc">${Math.round(day.precip)}% rain</div></div>`;
                }
            } catch (e) { console.error(e); }
        }

        // Tailscale
        async function loadTailscale() {
            try {
                const res = await fetch(`${API}/api/tailscale`);
                const data = await res.json();
                const status_div = document.getElementById('tailscale-status');
                if (data.error) { status_div.textContent = 'Error: ' + data.error; document.getElementById('tailscale-peers').textContent = 'Unable to connect'; return; }
                status_div.innerHTML = `<span style="color:var(--${data.status === 'running' ? 'green' : 'red'})">${data.status === 'running' ? '● Connected' : '○ Disconnected'}</span>`;
                const peers_div = document.getElementById('tailscale-peers');
                if (data.output) { peers_div.innerHTML = '<div style="font-family:var(--mono);font-size:0.85rem;white-space:pre-wrap;word-break:break-word;color:var(--text2);">' + data.output + '</div>'; }
                else { peers_div.textContent = 'No status available'; }
            } catch (e) { console.error(e); }
        }

        // Deploy Tab
        let _catalogData = [];
        async function loadCatalog() {
            const res = await fetch('/api/catalog/apps');
            const data = await res.json();
            _catalogData = data.apps || [];
            const sel = document.getElementById('deploy-category');
            if (sel.querySelectorAll('option[value!=""]').length === 0) {
                (data.categories || []).forEach(cat => { const opt = document.createElement('option'); opt.value = cat; opt.textContent = cat; sel.appendChild(opt); });
            }
            renderCatalog(_catalogData);
        }

        function filterCatalog() {
            const q = document.getElementById('deploy-search').value.toLowerCase();
            const cat = document.getElementById('deploy-category').value;
            let apps = _catalogData;
            if (q) apps = apps.filter(a => a.name.toLowerCase().includes(q) || a.description.toLowerCase().includes(q) || a.id.includes(q));
            if (cat) apps = apps.filter(a => a.category === cat);
            renderCatalog(apps);
        }

        function renderCatalog(apps) {
            const grid = document.getElementById('deploy-grid');
            if (!apps.length) { grid.innerHTML = '<div style="color:var(--text3);padding:2rem;text-align:center;">No apps found</div>'; return; }
            grid.innerHTML = apps.map(a => `
                <div style="background:var(--surface);backdrop-filter:blur(12px);border:1px solid var(--glass-border);border-radius:var(--r);padding:1.2rem;display:flex;flex-direction:column;gap:0.75rem;transition:all 0.25s;border-top:3px solid ${a.deployed ? 'var(--green)' : 'rgba(255,255,255,0.04)'};" onmouseover="this.style.borderColor='var(--border2)'" onmouseout="this.style.borderColor='var(--glass-border)'">
                    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:0.5rem;">
                        <div>
                            <div style="font-weight:600;font-size:0.95rem;">${a.icon || ''} ${a.name}</div>
                            <div style="font-size:0.65rem;color:var(--text3);margin-top:0.3rem;text-transform:uppercase;letter-spacing:1px;">${a.category}</div>
                        </div>
                        <span class="badge ${a.deployed ? 'badge-green' : ''}" style="${a.deployed ? '' : 'background:rgba(255,255,255,0.05);color:var(--text3);'}">${a.deployed ? 'RUNNING' : 'AVAILABLE'}</span>
                    </div>
                    <div style="font-size:0.85rem;color:var(--text2);flex:1;">${a.description}</div>
                    <div style="font-size:0.75rem;color:var(--text3);font-family:var(--mono);background:rgba(0,0,0,0.2);padding:0.4rem 0.6rem;border-radius:6px;">${a.image}</div>
                    ${(a.ports||[]).length ? `<div style="font-size:0.75rem;color:var(--text3);">Ports: ${a.ports.join(', ')}</div>` : ''}
                    <button onclick="deployApp('${a.id}','${a.name}')"
                            class="btn ${a.deployed ? 'btn-secondary' : 'btn-primary'} btn-sm" style="width:100%;justify-content:center;margin-top:auto;">
                        ${a.deployed ? '↻ Redeploy' : '🚀 Deploy'}
                    </button>
                </div>
            `).join('');
        }

        async function deployApp(appId, appName) {
            if (!confirm(`Deploy ${appName}?`)) return;
            const btn = event.target;
            btn.textContent = '⏳ Deploying...'; btn.disabled = true;
            try {
                const res = await fetch('/api/deploy', { method:'POST', headers:authHeaders(), body:JSON.stringify({app_id:appId}) });
                const data = await res.json();
                if (data.status === 'success') { btn.textContent = '✓ Deployed!'; btn.style.background = 'var(--green-dim)'; btn.style.color = 'var(--green)'; setTimeout(() => loadCatalog(), 2000); }
                else { alert('Deploy failed: ' + (data.error || data.stderr || 'Unknown error')); btn.textContent = '✗ Failed'; setTimeout(() => { btn.textContent = '🚀 Deploy'; btn.style.background = ''; btn.style.color = ''; btn.disabled = false; }, 3000); }
            } catch (e) { alert('Error: ' + e.message); btn.disabled = false; btn.textContent = '🚀 Deploy'; }
        }

        // Stack Manager
        async function loadStackCompose() {
            const res = await fetch('/api/stack/compose'); const data = await res.json();
            const el = document.getElementById('stack-compose');
            if (data.error) { el.textContent = 'Error: ' + data.error; return; }
            el.textContent = data.exists ? data.content : `No compose file found at: ${data.path}\n\nDeploy apps from the Deploy tab to create one.`;
            loadDeployHistory();
        }

        async function loadDeployHistory() {
            const res = await fetch('/api/history'); const data = await res.json();
            const el = document.getElementById('deploy-history');
            if (!data.history || !data.history.length) { el.innerHTML = '<div style="color:var(--text3);font-size:0.85rem;">No deployment history yet</div>'; return; }
            el.innerHTML = data.history.map(h => `
                <div style="background:var(--surface);backdrop-filter:blur(12px);border:1px solid var(--glass-border);border-radius:var(--r-sm);padding:0.85rem 1.1rem;display:flex;justify-content:space-between;align-items:center;font-size:0.85rem;border-left:3px solid ${h.status==='success'?'var(--green)':'var(--red)'};">
                    <div><span style="font-weight:600;font-family:var(--mono);">${h.app_name}</span> <span style="color:var(--text3);margin-left:0.5rem;">${h.action}</span></div>
                    <div style="display:flex;gap:0.8rem;align-items:center;">
                        <span class="badge ${h.status==='success'?'badge-green':'badge-red'}">${h.status.toUpperCase()}</span>
                        <span style="color:var(--text3);font-size:0.8rem;font-family:var(--mono);">${h.timestamp.split('T')[0]||h.timestamp}</span>
                    </div>
                </div>`).join('');
        }

        // Updates
        async function checkUpdates() {
            const el = document.getElementById('updates-list');
            el.innerHTML = '<div style="color:var(--text3);">Checking containers...</div>';
            const res = await fetch('/api/updates/check'); const data = await res.json();
            if (data.error) { el.innerHTML = `<div style="color:var(--red);">${data.error}</div>`; return; }
            if (!data.containers || !data.containers.length) { el.innerHTML = '<div style="color:var(--text3);">No running containers found</div>'; return; }
            el.innerHTML = data.containers.map(c => `
                <div style="background:var(--surface);backdrop-filter:blur(12px);border:1px solid var(--glass-border);border-radius:var(--r-sm);padding:1rem 1.2rem;display:flex;justify-content:space-between;align-items:center;gap:1rem;border-left:3px solid ${c.status==='running'?'var(--green)':'var(--red)'};">
                    <div><div style="font-weight:600;font-size:0.9rem;font-family:var(--mono);">${c.name}</div><div style="font-size:0.75rem;color:var(--text3);margin-top:0.2rem;">${c.image}</div></div>
                    <div style="display:flex;gap:0.75rem;align-items:center;flex-shrink:0;">
                        <span class="badge ${c.status==='running'?'badge-green':'badge-red'}">${c.status.toUpperCase()}</span>
                        <button onclick="pullUpdate('${c.name}')" class="btn btn-primary btn-sm">Pull & Restart</button>
                    </div>
                </div>`).join('');
        }

        async function pullUpdate(name) {
            if (!confirm(`Pull latest image for ${name} and restart?`)) return;
            const res = await fetch(`/api/updates/pull/${name}`, {method:'POST', headers:authHeaders()});
            const data = await res.json();
            alert(data.error ? `Error: ${data.error}` : `✓ Updated and restarted: ${name}`);
            checkUpdates();
        }

        // Backup
        async function loadBackups() {
            const res = await fetch('/api/backup/list'); const data = await res.json();
            const el = document.getElementById('backup-list');
            if (!data.backups || !data.backups.length) { el.innerHTML = '<div style="color:var(--text3);">No backups yet</div>'; return; }
            const fmt = b => (b.size / 1e6).toFixed(1) + ' MB';
            el.innerHTML = data.backups.map(b => `
                <div style="background:var(--surface);backdrop-filter:blur(12px);border:1px solid var(--glass-border);border-radius:var(--r-sm);padding:1rem 1.2rem;display:flex;justify-content:space-between;align-items:center;gap:1rem;border-left:3px solid var(--green);">
                    <div><div style="font-weight:600;font-size:0.85rem;font-family:var(--mono);color:var(--text);">${b.name}</div><div style="font-size:0.8rem;color:var(--text3);margin-top:0.3rem;">${fmt(b)} · ${b.created.split('T')[0]}</div></div>
                    <span class="badge badge-green">SAVED</span>
                </div>`).join('');
        }

        async function createBackup() {
            const status = document.getElementById('backup-status');
            status.textContent = '⏳ Creating backup...'; status.style.color = 'var(--text2)';
            const res = await fetch('/api/backup/create', {method:'POST', headers:authHeaders()});
            const data = await res.json();
            if (data.status === 'success') { status.textContent = `✓ Backup created: ${data.name} (${(data.size/1e6).toFixed(1)} MB)`; status.style.color = 'var(--green)'; loadBackups(); }
            else { status.textContent = '✗ Backup failed: ' + (data.error || 'unknown error'); status.style.color = 'var(--red)'; }
        }

        // Settings
        async function loadSettings() {
            const res = await fetch('/api/settings'); const data = await res.json();
            ['config_dir','media_dir','tz','puid','pgid'].forEach(key => { const el = document.getElementById('setting-'+key); if (el) el.value = data[key] || ''; });
            const authEl = document.getElementById('settings-auth-status');
            if (authEl) authEl.textContent = data.no_auth ? 'Disabled (LAN mode)' : 'Enabled (token)';
            const hintEl = document.getElementById('settings-token-hint');
            if (hintEl) hintEl.textContent = data.token_hint || '(not set)';
        }

        async function saveSettings() {
            const payload = {};
            ['config_dir','media_dir','tz','puid','pgid'].forEach(key => { const el = document.getElementById('setting-'+key); if (el && el.value) payload[key] = el.value; });
            const res = await fetch('/api/settings', { method:'POST', headers:authHeaders(), body:JSON.stringify(payload) });
            const data = await res.json();
            const el = document.getElementById('settings-status');
            if (el) { el.textContent = data.status === 'saved' ? '✓ Settings saved' : '✗ Error'; setTimeout(() => { el.textContent = ''; }, 3000); }
        }

        function copyToClipboard(text) { navigator.clipboard.writeText(text).then(() => { alert('Copied: ' + text); }); }

        // ── SSE live stream for overview gauges ──────────────────────────
        let evtSource = null;
        function startSSE() {
            if (evtSource) return;
            evtSource = new EventSource(API + '/api/stream');
            const liveBadge = document.getElementById('live-badge');
            evtSource.onopen = () => { if (liveBadge) liveBadge.style.display = 'inline-flex'; };
            evtSource.onmessage = (e) => {
                try {
                    const d = JSON.parse(e.data);
                    updateGauge('cpu-ring','cpu-gauge-text', d.cpu_percent, 100);
                    updateGauge('mem-ring','mem-gauge-text', d.mem_percent, 100);
                    const loadEl = document.getElementById('load-val');
                    if (loadEl) loadEl.textContent = d.load_1m + ' / ' + d.load_5m + ' / ' + d.load_15m;
                    const upEl = document.getElementById('uptime-val');
                    if (upEl) upEl.textContent = d.uptime;
                    const memDetail = document.getElementById('mem-detail');
                    if (memDetail) memDetail.textContent = d.mem_used_gb + ' / ' + d.mem_total_gb + ' GB';
                } catch(err) {}
            };
            evtSource.onerror = () => {
                if (liveBadge) liveBadge.style.display = 'none';
                evtSource.close(); evtSource = null;
                setTimeout(startSSE, 5000);
            };
        }

        // Polling for non-SSE tabs (every 8s)
        setInterval(() => {
            if (currentTab === 'storage') loadStorage();
            else if (currentTab === 'network') loadNetwork();
            else if (currentTab === 'containers') loadContainers();
        }, 8000);

        // Initial load
        checkAuth();
        loadSettings();
        loadOverview();
        startSSE();
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9999, debug=False)
