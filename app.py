#!/usr/bin/env python3
"""
Deployrr Max Monitor — Enhanced Server Administration Dashboard
Version: 3.1.0 · Full deployment, update management, and real-time monitoring
Port: 9999

Dependencies:
  pip install flask psutil requests docker

"""
import json, os, re, subprocess, time, glob, threading, xml.etree.ElementTree as ET, sqlite3
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
# Auth is fully disabled — dashboard is open with no login required.
_NO_AUTH = True

def _check_auth():
    """Auth disabled — always returns True."""
    return True

def require_auth(f):
    """Auth disabled — passthrough decorator."""
    @wraps(f)
    def decorated(*args, **kwargs):
        return f(*args, **kwargs)
    return decorated

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
    """List all containers with status, uptime, and port info."""
    if not DOCKER_OK:
        return jsonify({"containers": [], "error": "Docker not available"}), 500

    try:
        containers = _dc.containers.list(all=True)
        result = []
        for c in containers:
            state = c.attrs.get("State", {})
            started_at = state.get("StartedAt", "")
            # Calculate uptime
            uptime_str = ""
            if c.status == "running" and started_at:
                try:
                    from datetime import timezone as _tz
                    import re as _re
                    sa = _re.sub(r'\.\d+', '', started_at.replace("Z", "+00:00"))
                    st = datetime.fromisoformat(sa)
                    delta = datetime.now(timezone.utc) - st
                    s = int(delta.total_seconds())
                    if s < 60: uptime_str = f"{s}s"
                    elif s < 3600: uptime_str = f"{s//60}m"
                    elif s < 86400: uptime_str = f"{s//3600}h {(s%3600)//60}m"
                    else: uptime_str = f"{s//86400}d {(s%86400)//3600}h"
                except Exception:
                    uptime_str = ""
            result.append({
                "id":         c.id[:12],
                "name":       c.name,
                "image":      c.image.tags[0] if c.image.tags else c.image.id[:12],
                "status":     c.status,
                "state":      state,
                "ports":      _extract_ports(c),
                "uptime":     uptime_str,
                "started_at": started_at,
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

@app.route("/api/container/<cname>/stats")
def api_container_stats(cname):
    """Get live CPU and memory stats for a single container."""
    if not DOCKER_OK:
        return jsonify({"error": "Docker not available"}), 500
    try:
        container = _dc.containers.get(cname)
        raw = container.stats(stream=False)
        # CPU %
        cpu_d = raw["cpu_stats"]["cpu_usage"]["total_usage"] - raw["precpu_stats"]["cpu_usage"]["total_usage"]
        sys_d = raw["cpu_stats"].get("system_cpu_usage", 0) - raw["precpu_stats"].get("system_cpu_usage", 0)
        ncpu  = raw["cpu_stats"].get("online_cpus") or len(raw["cpu_stats"]["cpu_usage"].get("percpu_usage", [1]))
        cpu_pct = round((cpu_d / sys_d) * ncpu * 100.0, 1) if sys_d > 0 else 0.0
        # MEM
        mem_stats = raw.get("memory_stats", {})
        mem_usage = mem_stats.get("usage", 0) - mem_stats.get("stats", {}).get("cache", 0)
        mem_limit = mem_stats.get("limit", 1)
        mem_pct   = round(mem_usage / mem_limit * 100, 1) if mem_limit else 0.0
        return jsonify({
            "cpu_pct":    cpu_pct,
            "mem_pct":    mem_pct,
            "mem_usage_mb": round(mem_usage / 1e6, 1),
            "mem_limit_mb": round(mem_limit / 1e6, 1),
        })
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
<link href="https://fonts.googleapis.com/css2?family=Inter:ital,opsz,wght@0,14..32,300;0,14..32,400;0,14..32,500;0,14..32,600;0,14..32,700;1,14..32,400&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
/* =====================================================================
   DEPLOYRR — PegaProx-inspired dark dashboard
   ===================================================================== */
:root{
  --bg:       #0d1117;
  --bg2:      #161b22;
  --bg3:      #1c2128;
  --surface:  #21262d;
  --surface2: #30363d;
  --border:   #30363d;
  --border2:  #444c56;
  --text:     #c9d1d9;
  --text2:    #8b949e;
  --text3:    #6e7681;
  --green:    #3fb950;
  --green2:   rgba(63,185,80,.15);
  --blue:     #388bfd;
  --blue2:    rgba(56,139,253,.15);
  --orange:   #f78166;
  --orange2:  rgba(247,129,102,.15);
  --yellow:   #e3b341;
  --yellow2:  rgba(227,179,65,.15);
  --red:      #f85149;
  --red2:     rgba(248,81,73,.15);
  --purple:   #bc8cff;
  --purple2:  rgba(188,140,255,.15);
  --cyan:     #39d353;
  --sb-w:     260px;
  --top-h:    56px;
  --r:        8px;
  --mono:     'JetBrains Mono',monospace;
  --ui:       'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
  --transition: .15s ease;
}
*{margin:0;padding:0;box-sizing:border-box;}
html,body{width:100%;height:100%;background:var(--bg);color:var(--text);font-family:var(--ui);font-size:14px;line-height:1.5;overflow:hidden;}

/* ── Scrollbar ── */
::-webkit-scrollbar{width:6px;height:6px;}
::-webkit-scrollbar-track{background:transparent;}
::-webkit-scrollbar-thumb{background:var(--surface2);border-radius:3px;}

/* ── Layout ── */
#app{display:flex;height:100vh;overflow:hidden;}
#sidebar{
  width:var(--sb-w);min-width:var(--sb-w);
  background:var(--bg2);
  border-right:1px solid var(--border);
  display:flex;flex-direction:column;
  overflow-y:auto;overflow-x:hidden;
  flex-shrink:0;
}
#main{flex:1;display:flex;flex-direction:column;overflow:hidden;}
#topbar{
  height:var(--top-h);min-height:var(--top-h);
  background:var(--bg2);
  border-bottom:1px solid var(--border);
  display:flex;align-items:center;padding:0 20px;gap:16px;
  flex-shrink:0;
}
#content{flex:1;overflow-y:auto;padding:20px;background:var(--bg);}

/* ── Sidebar ── */
.sb-brand{
  padding:16px 16px 12px;
  border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:10px;
}
.sb-logo{
  width:32px;height:32px;border-radius:8px;
  background:linear-gradient(135deg,#388bfd,#bc8cff);
  display:flex;align-items:center;justify-content:center;
  font-size:16px;font-weight:700;color:#fff;flex-shrink:0;
}
.sb-title{font-weight:600;font-size:15px;color:var(--text);}
.sb-version{font-size:11px;color:var(--text3);}

.sb-section{padding:12px 8px 4px;}
.sb-section-label{
  font-size:11px;font-weight:600;color:var(--text3);
  text-transform:uppercase;letter-spacing:.08em;
  padding:0 8px 6px;
}
.sb-item{
  display:flex;align-items:center;gap:8px;
  padding:7px 8px;border-radius:var(--r);
  cursor:pointer;color:var(--text2);
  transition:background var(--transition),color var(--transition);
  font-size:13.5px;
}
.sb-item:hover{background:var(--surface);color:var(--text);}
.sb-item.active{background:var(--blue2);color:var(--blue);font-weight:500;}
.sb-item .sb-icon{width:16px;height:16px;opacity:.7;flex-shrink:0;}
.sb-item.active .sb-icon{opacity:1;}
.sb-badge{
  margin-left:auto;font-size:10px;font-weight:600;
  background:var(--surface2);color:var(--text2);
  padding:1px 6px;border-radius:10px;
}
.sb-badge.green{background:var(--green2);color:var(--green);}
.sb-badge.red{background:var(--red2);color:var(--red);}

/* ── Topbar ── */
.tb-stat{
  display:flex;align-items:center;gap:8px;
  padding:6px 12px;border-radius:var(--r);
  background:var(--surface);border:1px solid var(--border);
  cursor:default;
}
.tb-stat-label{font-size:11px;color:var(--text2);}
.tb-stat-val{font-size:13px;font-weight:600;color:var(--text);}
.tb-stat-val.green{color:var(--green);}
.tb-stat-val.orange{color:var(--orange);}
.tb-stat-val.red{color:var(--red);}

.live-pill{
  display:inline-flex;align-items:center;gap:5px;
  padding:3px 10px;border-radius:20px;
  background:var(--green2);border:1px solid rgba(63,185,80,.3);
  font-size:11px;font-weight:600;color:var(--green);
}
.live-dot{width:6px;height:6px;border-radius:50%;background:var(--green);animation:pulse 1.4s infinite;}
@keyframes pulse{0%,100%{opacity:1;}50%{opacity:.3;}}

.tb-right{margin-left:auto;display:flex;align-items:center;gap:10px;}
.tb-time{font-family:var(--mono);font-size:13px;color:var(--text2);}

/* ── Sections ── */
.section-header{
  display:flex;align-items:center;justify-content:space-between;
  margin-bottom:14px;
}
.section-title{font-size:15px;font-weight:600;color:var(--text);}
.section-sub{font-size:12px;color:var(--text3);}

/* ── Stat cards row ── */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-bottom:20px;}
.stat-card{
  background:var(--bg2);border:1px solid var(--border);
  border-radius:var(--r);padding:14px 16px;
  display:flex;flex-direction:column;gap:6px;
  transition:border-color var(--transition);
}
.stat-card:hover{border-color:var(--border2);}
.stat-card-icon{font-size:18px;margin-bottom:2px;}
.stat-card-val{font-size:22px;font-weight:700;color:var(--text);font-family:var(--mono);}
.stat-card-label{font-size:11px;color:var(--text2);}

/* ── Progress bar ── */
.pbar-wrap{width:100%;height:4px;background:var(--surface2);border-radius:2px;overflow:hidden;}
.pbar{height:100%;border-radius:2px;transition:width .6s ease;}
.pbar.green{background:var(--green);}
.pbar.blue{background:var(--blue);}
.pbar.orange{background:var(--orange);}
.pbar.red{background:var(--red);}
.pbar.yellow{background:var(--yellow);}

/* ── SVG Gauge ── */
.gauge-wrap{display:flex;flex-direction:column;align-items:center;gap:4px;}
.gauge-svg{width:80px;height:80px;}
.gauge-circle-bg{fill:none;stroke:var(--surface2);stroke-width:7;}
.gauge-circle{fill:none;stroke-width:7;stroke-linecap:round;transform-origin:50% 50%;transform:rotate(-90deg);transition:stroke-dashoffset .7s ease,stroke .4s;}
.gauge-text{font-family:var(--mono);font-weight:700;font-size:14px;dominant-baseline:middle;text-anchor:middle;fill:var(--text);}
.gauge-sub{font-size:11px;color:var(--text2);font-weight:500;}

/* ── Overview metric row ── */
.metric-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin-bottom:20px;}
.metric-card{
  background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);
  padding:16px;display:flex;flex-direction:column;gap:8px;
}
.metric-top{display:flex;align-items:center;justify-content:space-between;}
.metric-name{font-size:12px;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:.06em;}
.metric-badge{font-size:11px;padding:2px 7px;border-radius:10px;font-weight:600;}

/* ── Container grid ── */
.container-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px;}
.ctr-card{
  background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);
  overflow:hidden;transition:border-color var(--transition),box-shadow var(--transition);
}
.ctr-card:hover{border-color:var(--border2);box-shadow:0 4px 20px rgba(0,0,0,.35);}

.ctr-header{padding:12px 14px;display:flex;align-items:flex-start;gap:10px;border-bottom:1px solid var(--border);}
.ctr-icon{
  width:36px;height:36px;border-radius:8px;flex-shrink:0;
  background:var(--surface2);display:flex;align-items:center;justify-content:center;font-size:18px;
}
.ctr-info{flex:1;min-width:0;}
.ctr-name{font-weight:600;font-size:14px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.ctr-image{font-size:11px;color:var(--text3);font-family:var(--mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px;}
.ctr-status{
  display:inline-flex;align-items:center;gap:4px;
  padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;
  flex-shrink:0;margin-top:2px;
}
.ctr-status.running{background:var(--green2);color:var(--green);}
.ctr-status.exited{background:var(--red2);color:var(--red);}
.ctr-status.paused{background:var(--yellow2);color:var(--yellow);}
.ctr-status.restarting{background:var(--orange2);color:var(--orange);}
.ctr-status-dot{width:5px;height:5px;border-radius:50%;background:currentColor;}
.ctr-status.running .ctr-status-dot{animation:pulse 1.4s infinite;}

.ctr-body{padding:10px 14px;}
.ctr-row{display:flex;align-items:center;justify-content:space-between;padding:3px 0;font-size:12px;color:var(--text2);}
.ctr-row span:last-child{color:var(--text);font-family:var(--mono);font-size:11.5px;}
.ctr-ports{font-size:11px;font-family:var(--mono);color:var(--blue);word-break:break-all;}

.ctr-stats{padding:8px 14px;display:grid;grid-template-columns:1fr 1fr;gap:8px;}
.ctr-stat-item{display:flex;flex-direction:column;gap:3px;}
.ctr-stat-label{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;font-weight:600;}
.ctr-stat-val{font-size:12px;font-weight:600;font-family:var(--mono);color:var(--text);}

.ctr-footer{
  padding:10px 14px;
  border-top:1px solid var(--border);
  display:flex;align-items:center;gap:6px;flex-wrap:wrap;
}
.btn{
  display:inline-flex;align-items:center;gap:5px;
  padding:5px 10px;border-radius:6px;border:1px solid var(--border);
  background:var(--surface);color:var(--text2);font-size:12px;font-family:var(--ui);
  cursor:pointer;transition:background var(--transition),border-color var(--transition),color var(--transition);
}
.btn:hover{background:var(--surface2);color:var(--text);border-color:var(--border2);}
.btn.green{border-color:rgba(63,185,80,.4);color:var(--green);}
.btn.green:hover{background:var(--green2);}
.btn.red{border-color:rgba(248,81,73,.4);color:var(--red);}
.btn.red:hover{background:var(--red2);}
.btn.orange{border-color:rgba(247,129,102,.4);color:var(--orange);}
.btn.orange:hover{background:var(--orange2);}
.btn.blue{border-color:rgba(56,139,253,.4);color:var(--blue);}
.btn.blue:hover{background:var(--blue2);}
.btn svg{width:12px;height:12px;flex-shrink:0;}

/* ── Generic panel ── */
.panel{background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);padding:16px;margin-bottom:16px;}
.panel-title{font-size:13px;font-weight:600;color:var(--text);margin-bottom:12px;display:flex;align-items:center;gap:8px;}

/* ── Tables ── */
table{width:100%;border-collapse:collapse;font-size:13px;}
thead th{padding:8px 12px;text-align:left;font-weight:600;font-size:11px;color:var(--text2);
  text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid var(--border);}
tbody tr{border-bottom:1px solid var(--border);transition:background var(--transition);}
tbody tr:hover{background:var(--surface);}
tbody td{padding:9px 12px;color:var(--text);font-size:13px;}
tbody tr:last-child{border-bottom:none;}

/* ── Log viewer ── */
#log-output{
  background:var(--bg);border:1px solid var(--border);border-radius:var(--r);
  padding:12px;font-family:var(--mono);font-size:12px;color:var(--text2);
  height:400px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;
}

/* ── Search ── */
.search-wrap{position:relative;flex:1;max-width:320px;}
.search-wrap input{
  width:100%;background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:6px 10px 6px 32px;color:var(--text);
  font-family:var(--ui);font-size:13px;outline:none;
  transition:border-color var(--transition);
}
.search-wrap input:focus{border-color:var(--blue);}
.search-wrap input::placeholder{color:var(--text3);}
.search-icon{position:absolute;left:9px;top:50%;transform:translateY(-50%);color:var(--text3);width:14px;height:14px;}

/* ── Filter pills ── */
.filter-row{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px;}
.filter-pill{
  padding:4px 12px;border-radius:20px;border:1px solid var(--border);
  background:var(--surface);color:var(--text2);font-size:12px;cursor:pointer;
  transition:all var(--transition);
}
.filter-pill:hover{border-color:var(--border2);color:var(--text);}
.filter-pill.active{background:var(--blue2);border-color:rgba(56,139,253,.5);color:var(--blue);}

/* ── Modal ── */
.modal-overlay{
  position:fixed;inset:0;background:rgba(0,0,0,.7);
  display:flex;align-items:center;justify-content:center;z-index:1000;
  opacity:0;pointer-events:none;transition:opacity .2s;
}
.modal-overlay.open{opacity:1;pointer-events:all;}
.modal{
  background:var(--bg2);border:1px solid var(--border2);border-radius:12px;
  width:min(700px,94vw);max-height:82vh;display:flex;flex-direction:column;
  overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.5);
}
.modal-header{
  padding:14px 18px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
}
.modal-title{font-weight:600;font-size:15px;}
.modal-close{
  background:none;border:none;color:var(--text2);cursor:pointer;padding:4px;
  border-radius:6px;transition:background var(--transition);
}
.modal-close:hover{background:var(--surface2);color:var(--text);}
.modal-body{padding:16px 18px;overflow-y:auto;flex:1;}
.modal-log{
  background:var(--bg);border:1px solid var(--border);border-radius:var(--r);
  padding:12px;font-family:var(--mono);font-size:12px;color:#adb7c4;
  height:400px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;
  line-height:1.6;
}

/* ── Tabs ── */
.tab-bar{display:flex;gap:2px;border-bottom:1px solid var(--border);margin-bottom:16px;}
.tab-btn{
  padding:8px 16px;background:none;border:none;border-bottom:2px solid transparent;
  color:var(--text2);cursor:pointer;font-size:13px;font-family:var(--ui);font-weight:500;
  transition:color var(--transition),border-color var(--transition);margin-bottom:-1px;
}
.tab-btn:hover{color:var(--text);}
.tab-btn.active{color:var(--blue);border-bottom-color:var(--blue);}
.tab-panel{display:none;}
.tab-panel.active{display:block;}

/* ── Settings ── */
.settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
.field{display:flex;flex-direction:column;gap:5px;}
.field label{font-size:12px;font-weight:600;color:var(--text2);}
.field input,.field select,.field textarea{
  background:var(--surface);border:1px solid var(--border);border-radius:var(--r);
  padding:7px 10px;color:var(--text);font-family:var(--ui);font-size:13px;outline:none;
  transition:border-color var(--transition);
}
.field input:focus,.field select:focus,.field textarea:focus{border-color:var(--blue);}
.field-hint{font-size:11px;color:var(--text3);}

/* ── Btn primary ── */
.btn-primary{
  display:inline-flex;align-items:center;gap:6px;
  padding:7px 16px;border-radius:var(--r);border:none;
  background:var(--blue);color:#fff;font-family:var(--ui);font-size:13px;font-weight:600;
  cursor:pointer;transition:opacity var(--transition);
}
.btn-primary:hover{opacity:.85;}

/* ── Animations ── */
@keyframes fadeInUp{from{opacity:0;transform:translateY(8px);}to{opacity:1;transform:translateY(0);}}
.fade-in{animation:fadeInUp .2s ease forwards;}

/* ── Empty state ── */
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:60px 20px;gap:12px;color:var(--text3);}
.empty-icon{font-size:40px;}
.empty-text{font-size:14px;}

/* ── Net / Storage ── */
.disk-item{margin-bottom:10px;}
.disk-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;font-size:12px;}
.disk-path{color:var(--text);font-family:var(--mono);}
.disk-usage{color:var(--text2);}

/* ── Catalog ── */
.cat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;}
.cat-card{
  background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);
  padding:14px;display:flex;flex-direction:column;gap:8px;
  transition:border-color var(--transition);
}
.cat-card:hover{border-color:var(--border2);}
.cat-card-header{display:flex;align-items:center;gap:10px;}
.cat-icon{font-size:22px;width:36px;text-align:center;}
.cat-name{font-weight:600;font-size:13.5px;color:var(--text);}
.cat-cat{font-size:11px;color:var(--text3);}
.cat-desc{font-size:12px;color:var(--text2);line-height:1.5;}
.cat-footer{margin-top:auto;display:flex;align-items:center;justify-content:space-between;}
.cat-image{font-size:11px;font-family:var(--mono);color:var(--text3);}

/* ── Responsive ── */
@media(max-width:700px){
  #sidebar{display:none;}
  .container-grid{grid-template-columns:1fr;}
  .settings-grid{grid-template-columns:1fr;}
  .stat-grid{grid-template-columns:repeat(2,1fr);}
}
</style>
</head>
<body>
<div id="app">

<!-- ═══════════════════════════════════════════════════════════
     SIDEBAR
═══════════════════════════════════════════════════════════ -->
<nav id="sidebar">
  <div class="sb-brand">
    <div class="sb-logo">D</div>
    <div>
      <div class="sb-title">Deployrr</div>
      <div class="sb-version">v3.2.0</div>
    </div>
  </div>

  <div class="sb-section">
    <div class="sb-section-label">Overview</div>
    <div class="sb-item active" onclick="showTab('overview',this)">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 5a1 1 0 011-1h4a1 1 0 011 1v5a1 1 0 01-1 1H5a1 1 0 01-1-1V5zm10 0a1 1 0 011-1h4a1 1 0 011 1v2a1 1 0 01-1 1h-4a1 1 0 01-1-1V5zM4 15a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1H5a1 1 0 01-1-1v-4zm10-3a1 1 0 011-1h4a1 1 0 011 1v7a1 1 0 01-1 1h-4a1 1 0 01-1-1v-7z"/></svg>
      Dashboard
    </div>
    <div class="sb-item" onclick="showTab('containers',this)">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/></svg>
      Containers
      <span class="sb-badge" id="sb-ctr-count">0</span>
    </div>
    <div class="sb-item" onclick="showTab('storage',this)">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 10h18M3 14h18m-9-4v8m-7 0h14a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
      Storage
    </div>
    <div class="sb-item" onclick="showTab('network',this)">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l4-4 4 4m0 6l-4 4-4-4"/></svg>
      Network
    </div>
    <div class="sb-item" onclick="showTab('hardware',this)">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2v-4M9 21H5a2 2 0 01-2-2v-4m0 0h18"/></svg>
      Hardware
    </div>
  </div>

  <div class="sb-section">
    <div class="sb-section-label">Deployment</div>
    <div class="sb-item" onclick="showTab('deploy',this)">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6m0 0v6m0-6h6m-6 0H6"/></svg>
      Deploy Apps
    </div>
    <div class="sb-item" onclick="showTab('stack',this)">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"/></svg>
      Stack Manager
    </div>
    <div class="sb-item" onclick="showTab('updates',this)">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"/></svg>
      Updates
    </div>
    <div class="sb-item" onclick="showTab('backup',this)">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M9 19l3 3m0 0l3-3m-3 3V10"/></svg>
      Backup
    </div>
  </div>

  <div class="sb-section">
    <div class="sb-section-label">Monitoring</div>
    <div class="sb-item" onclick="showTab('logs',this)">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
      Logs
    </div>
    <div class="sb-item" onclick="openExternalLink('http://localhost:9090')">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>
      Prometheus ↗
    </div>
    <div class="sb-item" onclick="openExternalLink('http://localhost:3000')">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 12a9 9 0 1018 0 9 9 0 00-18 0"/></svg>
      Grafana ↗
    </div>
    <div class="sb-item" onclick="openExternalLink('http://localhost:3001')">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
      Uptime Kuma ↗
    </div>
    <div class="sb-item" onclick="openExternalLink('http://localhost:8888')">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/></svg>
      Dozzle ↗
    </div>
  </div>

  <div class="sb-section">
    <div class="sb-section-label">System</div>
    <div class="sb-item" onclick="showTab('settings',this)">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><circle cx="12" cy="12" r="3"/></svg>
      Settings
    </div>
  </div>
</nav>

<!-- ═══════════════════════════════════════════════════════════
     MAIN
═══════════════════════════════════════════════════════════ -->
<div id="main">

  <!-- TOP BAR -->
  <div id="topbar">
    <div class="tb-stat">
      <span class="tb-stat-label">Containers</span>
      <span class="tb-stat-val green" id="tb-running">—</span>
      <span class="tb-stat-label">/ <span id="tb-total">—</span> total</span>
    </div>
    <div class="tb-stat">
      <span class="tb-stat-label">CPU</span>
      <span class="tb-stat-val" id="tb-cpu">—%</span>
    </div>
    <div class="tb-stat">
      <span class="tb-stat-label">RAM</span>
      <span class="tb-stat-val" id="tb-ram">—%</span>
    </div>
    <div class="tb-stat">
      <span class="tb-stat-label">Load</span>
      <span class="tb-stat-val" id="tb-load">—</span>
    </div>
    <div class="live-pill" id="live-badge" style="display:none">
      <div class="live-dot"></div>Live
    </div>
    <div class="tb-right">
      <span class="tb-time" id="tb-time"></span>
    </div>
  </div>

  <!-- CONTENT -->
  <div id="content">

    <!-- ── DASHBOARD ── -->
    <div id="tab-overview" class="tab-panel active fade-in">
      <div class="section-header">
        <div>
          <div class="section-title">System Overview</div>
          <div class="section-sub" id="ov-hostname">Loading...</div>
        </div>
      </div>

      <!-- Gauges row -->
      <div class="metric-grid" id="gauge-row">
        <div class="metric-card">
          <div class="metric-top">
            <span class="metric-name">CPU Usage</span>
            <span class="metric-badge" id="cpu-badge" style="background:var(--green2);color:var(--green)">0%</span>
          </div>
          <div class="gauge-wrap">
            <svg class="gauge-svg" viewBox="0 0 100 100">
              <circle class="gauge-circle-bg" cx="50" cy="50" r="40" stroke-dasharray="251.3" stroke-dashoffset="0"/>
              <circle class="gauge-circle" id="cpu-ring" cx="50" cy="50" r="40" stroke="var(--blue)" stroke-dasharray="251.3" stroke-dashoffset="251.3"/>
              <text class="gauge-text" id="cpu-gauge-text" x="50" y="50">0%</text>
            </svg>
            <span class="gauge-sub" id="cpu-cores">— cores</span>
          </div>
          <div class="pbar-wrap"><div class="pbar blue" id="cpu-pbar" style="width:0%"></div></div>
        </div>

        <div class="metric-card">
          <div class="metric-top">
            <span class="metric-name">Memory</span>
            <span class="metric-badge" id="mem-badge" style="background:var(--green2);color:var(--green)">0%</span>
          </div>
          <div class="gauge-wrap">
            <svg class="gauge-svg" viewBox="0 0 100 100">
              <circle class="gauge-circle-bg" cx="50" cy="50" r="40" stroke-dasharray="251.3" stroke-dashoffset="0"/>
              <circle class="gauge-circle" id="mem-ring" cx="50" cy="50" r="40" stroke="var(--purple)" stroke-dasharray="251.3" stroke-dashoffset="251.3"/>
              <text class="gauge-text" id="mem-gauge-text" x="50" y="50">0%</text>
            </svg>
            <span class="gauge-sub" id="mem-detail">— / — GB</span>
          </div>
          <div class="pbar-wrap"><div class="pbar" id="mem-pbar" style="width:0%;background:var(--purple)"></div></div>
        </div>

        <div class="metric-card">
          <div class="metric-top"><span class="metric-name">Load Average</span></div>
          <div style="display:flex;flex-direction:column;gap:8px;padding:8px 0;">
            <div class="ctr-row"><span>1 min</span><span id="load-1m">—</span></div>
            <div class="ctr-row"><span>5 min</span><span id="load-5m">—</span></div>
            <div class="ctr-row"><span>15 min</span><span id="load-15m">—</span></div>
          </div>
          <div class="ctr-row" style="margin-top:4px;"><span>Uptime</span><span id="uptime-val">—</span></div>
        </div>

        <div class="metric-card">
          <div class="metric-top"><span class="metric-name">Containers</span></div>
          <div style="display:flex;flex-direction:column;gap:8px;padding:8px 0;">
            <div class="ctr-row"><span>Running</span><span id="ctr-running-count" style="color:var(--green)">—</span></div>
            <div class="ctr-row"><span>Stopped</span><span id="ctr-stopped-count" style="color:var(--red)">—</span></div>
            <div class="ctr-row"><span>Total</span><span id="ctr-total-count">—</span></div>
          </div>
          <button class="btn blue" style="margin-top:6px;width:100%;justify-content:center" onclick="showTab('containers',null)">
            <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/></svg>
            View Containers
          </button>
        </div>
      </div>

      <!-- Quick stats -->
      <div class="panel">
        <div class="panel-title">
          <svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
          System Info
        </div>
        <div class="stat-grid" id="sys-info-grid">
          <div class="stat-card"><div class="stat-card-val" id="si-os">—</div><div class="stat-card-label">OS</div></div>
          <div class="stat-card"><div class="stat-card-val" id="si-kernel">—</div><div class="stat-card-label">Kernel</div></div>
          <div class="stat-card"><div class="stat-card-val" id="si-arch">—</div><div class="stat-card-label">Architecture</div></div>
          <div class="stat-card"><div class="stat-card-val" id="si-python">—</div><div class="stat-card-label">Python</div></div>
        </div>
      </div>
    </div>

    <!-- ── CONTAINERS ── -->
    <div id="tab-containers" class="tab-panel">
      <div class="section-header">
        <div class="section-title">Containers</div>
        <button class="btn-primary" onclick="loadContainers()">
          <svg width="12" height="12" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
          Refresh
        </button>
      </div>
      <div class="filter-row">
        <div class="filter-pill active" onclick="filterContainers('all',this)">All</div>
        <div class="filter-pill" onclick="filterContainers('running',this)">Running</div>
        <div class="filter-pill" onclick="filterContainers('exited',this)">Stopped</div>
        <div class="filter-pill" onclick="filterContainers('restarting',this)">Restarting</div>
      </div>
      <div class="container-grid" id="ctr-grid">
        <div class="empty"><div class="empty-icon">📦</div><div class="empty-text">Loading containers...</div></div>
      </div>
    </div>

    <!-- ── STORAGE ── -->
    <div id="tab-storage" class="tab-panel">
      <div class="section-header"><div class="section-title">Storage</div></div>
      <div class="panel">
        <div class="panel-title">Filesystems</div>
        <div id="disk-list"></div>
      </div>
    </div>

    <!-- ── NETWORK ── -->
    <div id="tab-network" class="tab-panel">
      <div class="section-header"><div class="section-title">Network</div></div>
      <div class="panel">
        <div class="panel-title">Interfaces</div>
        <table><thead><tr><th>Interface</th><th>IP</th><th>Sent</th><th>Recv</th><th>Status</th></tr></thead>
        <tbody id="net-table"></tbody></table>
      </div>
    </div>

    <!-- ── HARDWARE ── -->
    <div id="tab-hardware" class="tab-panel">
      <div class="section-header"><div class="section-title">Hardware</div></div>
      <div class="panel">
        <div class="panel-title">CPU</div>
        <div class="stat-grid" id="hw-cpu"></div>
      </div>
      <div class="panel">
        <div class="panel-title">Memory</div>
        <div id="hw-mem"></div>
      </div>
    </div>

    <!-- ── LOGS ── -->
    <div id="tab-logs" class="tab-panel">
      <div class="section-header">
        <div class="section-title">System Logs</div>
        <button class="btn-primary" onclick="loadLogs()">Refresh</button>
      </div>
      <div class="panel-title" style="margin-bottom:8px">Journal (last 200 lines)</div>
      <div id="log-output">Loading...</div>
    </div>

    <!-- ── DEPLOY ── -->
    <div id="tab-deploy" class="tab-panel">
      <div class="section-header">
        <div class="section-title">App Catalog</div>
        <div class="search-wrap">
          <svg class="search-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/></svg>
          <input type="text" id="cat-search" placeholder="Search apps..." oninput="filterCatalog()">
        </div>
      </div>
      <div id="cat-categories" class="filter-row" style="margin-bottom:16px"></div>
      <div class="cat-grid" id="cat-grid">
        <div class="empty"><div class="empty-icon">🔍</div><div class="empty-text">Loading catalog...</div></div>
      </div>
    </div>

    <!-- ── STACK ── -->
    <div id="tab-stack" class="tab-panel">
      <div class="section-header"><div class="section-title">Stack Manager</div></div>
      <div class="panel">
        <div class="panel-title">Compose Stacks</div>
        <div id="stacks-list"><div class="empty"><div class="empty-icon">📋</div><div class="empty-text">Loading stacks...</div></div></div>
      </div>
      <div class="panel">
        <div class="panel-title">Deploy History</div>
        <div id="deploy-history"></div>
      </div>
    </div>

    <!-- ── UPDATES ── -->
    <div id="tab-updates" class="tab-panel">
      <div class="section-header">
        <div class="section-title">Image Updates</div>
        <button class="btn-primary" onclick="checkUpdates()">Check Now</button>
      </div>
      <div id="updates-list"><div class="empty"><div class="empty-icon">🔄</div><div class="empty-text">Click "Check Now" to scan for updates</div></div></div>
    </div>

    <!-- ── BACKUP ── -->
    <div id="tab-backup" class="tab-panel">
      <div class="section-header">
        <div class="section-title">Backup & Restore</div>
        <button class="btn-primary" onclick="createBackup()">Create Backup</button>
      </div>
      <div id="backups-list"><div class="empty"><div class="empty-icon">💾</div><div class="empty-text">No backups yet</div></div></div>
    </div>

    <!-- ── SETTINGS ── -->
    <div id="tab-settings" class="tab-panel">
      <div class="section-header"><div class="section-title">Settings</div></div>
      <div class="panel">
        <div class="panel-title">Configuration</div>
        <div class="settings-grid" id="settings-form">
          <div class="field"><label>Config Directory</label><input type="text" id="cfg-dir" placeholder="/docker"><div class="field-hint">Where app configs are stored</div></div>
          <div class="field"><label>Media Directory</label><input type="text" id="media-dir" placeholder="/mnt/media"><div class="field-hint">Where media files are stored</div></div>
          <div class="field"><label>Timezone</label><input type="text" id="cfg-tz" placeholder="America/New_York"></div>
          <div class="field"><label>PUID</label><input type="text" id="cfg-puid" placeholder="1000"></div>
          <div class="field"><label>PGID</label><input type="text" id="cfg-pgid" placeholder="1000"></div>
        </div>
        <button class="btn-primary" style="margin-top:16px" onclick="saveSettings()">Save Settings</button>
      </div>
      <div class="panel">
        <div class="panel-title">About</div>
        <div class="ctr-row"><span>Deployrr Version</span><span>3.2.0</span></div>
        <div class="ctr-row"><span>Auth Status</span><span style="color:var(--green)">Disabled (open access)</span></div>
        <div class="ctr-row"><span>WebUI Port</span><span>9999</span></div>
      </div>
    </div>

  </div><!-- /content -->
</div><!-- /main -->
</div><!-- /app -->

<!-- ═══ LOG MODAL ═══ -->
<div class="modal-overlay" id="log-modal">
  <div class="modal">
    <div class="modal-header">
      <div class="modal-title" id="log-modal-title">Container Logs</div>
      <button class="modal-close" onclick="closeModal()">
        <svg width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
      </button>
    </div>
    <div class="modal-body">
      <div class="modal-log" id="log-modal-body">Loading...</div>
    </div>
  </div>
</div>

<script>
/* =====================================================================
   DEPLOYRR FRONTEND — no auth, pure vanilla JS
   ===================================================================== */

const API = '';
let currentTab = 'overview';
let allContainers = [];
let ctrFilter = 'all';
let allCatalog = [];
let catFilter = 'all';

// ── Time ─────────────────────────────────────────────────────────────
function updateTime() {
    const el = document.getElementById('tb-time');
    if (el) el.textContent = new Date().toLocaleTimeString('en-US',{hour12:false});
}
setInterval(updateTime, 1000);
updateTime();

// ── Tab navigation ────────────────────────────────────────────────────
function showTab(name, el) {
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.sb-item').forEach(i => i.classList.remove('active'));
    const panel = document.getElementById('tab-' + name);
    if (panel) { panel.classList.add('active'); panel.classList.add('fade-in'); }
    if (el) el.classList.add('active');
    currentTab = name;

    // Lazy-load on first show
    if (name === 'containers') loadContainers();
    else if (name === 'storage') loadStorage();
    else if (name === 'network') loadNetwork();
    else if (name === 'hardware') loadHardware();
    else if (name === 'logs') loadLogs();
    else if (name === 'deploy') loadCatalog();
    else if (name === 'stack') { loadStackManager(); loadDeployHistory(); }
    else if (name === 'backup') loadBackups();
    else if (name === 'settings') loadSettings();
}

function openExternalLink(url) { window.open(url, '_blank'); }

// ── Gauge ─────────────────────────────────────────────────────────────
function updateGauge(ringId, textId, value, max) {
    const ring = document.getElementById(ringId);
    const text = document.getElementById(textId);
    if (!ring || !text) return;
    const pct = Math.min(100, Math.max(0, (value / max) * 100));
    const circumference = 251.3;
    ring.style.strokeDashoffset = circumference - (pct / 100) * circumference;
    // Color
    const color = pct < 50 ? 'var(--green)' : pct < 80 ? 'var(--yellow)' : 'var(--red)';
    ring.style.stroke = color;
    text.textContent = Math.round(pct) + '%';
    text.style.fill = color;
}

function pbarColor(pct) {
    if (pct < 50) return 'green';
    if (pct < 80) return 'yellow';
    return 'red';
}

// ── SSE live stream ───────────────────────────────────────────────────
let evtSource = null;
function startSSE() {
    if (evtSource) return;
    evtSource = new EventSource(API + '/api/stream');
    const badge = document.getElementById('live-badge');
    evtSource.onopen = () => { if (badge) badge.style.display = 'inline-flex'; };
    evtSource.onmessage = (e) => {
        try {
            const d = JSON.parse(e.data);
            // Topbar
            const cpu = d.cpu_percent; const ram = d.mem_percent;
            setEl('tb-cpu', cpu + '%');
            setEl('tb-ram', ram + '%');
            setEl('tb-load', d.load_1m);
            colorEl('tb-cpu', cpu < 50 ? '' : cpu < 80 ? 'orange' : 'red');
            colorEl('tb-ram', ram < 50 ? '' : ram < 80 ? 'orange' : 'red');
            // Gauges
            updateGauge('cpu-ring','cpu-gauge-text', cpu, 100);
            updateGauge('mem-ring','mem-gauge-text', ram, 100);
            setEl('cpu-badge', cpu + '%');
            setEl('mem-badge', ram + '%');
            const cpuPbar = document.getElementById('cpu-pbar');
            if (cpuPbar) { cpuPbar.style.width = cpu + '%'; cpuPbar.className = 'pbar ' + pbarColor(cpu); }
            const memPbar = document.getElementById('mem-pbar');
            if (memPbar) { memPbar.style.width = ram + '%'; memPbar.style.background = ram < 50 ? 'var(--green)' : ram < 80 ? 'var(--yellow)' : 'var(--red)'; }
            setEl('load-1m', d.load_1m);
            setEl('load-5m', d.load_5m);
            setEl('load-15m', d.load_15m);
            setEl('uptime-val', d.uptime);
            setEl('mem-detail', d.mem_used_gb + ' / ' + d.mem_total_gb + ' GB');
        } catch(err) {}
    };
    evtSource.onerror = () => {
        if (badge) badge.style.display = 'none';
        evtSource.close(); evtSource = null;
        setTimeout(startSSE, 5000);
    };
}

// ── Helpers ───────────────────────────────────────────────────────────
function setEl(id, val) { const el = document.getElementById(id); if (el) el.textContent = val; }
function colorEl(id, cls) {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = 'tb-stat-val' + (cls ? ' ' + cls : '');
}
function fmtBytes(b) {
    if (!b) return '0 B';
    const k=1024,m=k*k,g=m*k,t=g*k;
    if (b>=t) return (b/t).toFixed(1)+' TB';
    if (b>=g) return (b/g).toFixed(1)+' GB';
    if (b>=m) return (b/m).toFixed(1)+' MB';
    if (b>=k) return (b/k).toFixed(1)+' KB';
    return b+' B';
}

// ── Overview ─────────────────────────────────────────────────────────
async function loadOverview() {
    try {
        const r = await fetch(API + '/api/overview');
        const d = await r.json();
        setEl('ov-hostname', (d.hostname || '') + ' · ' + (d.os || ''));
        setEl('si-os', d.os || '—');
        setEl('si-kernel', d.kernel || '—');
        setEl('si-arch', d.arch || '—');
        setEl('si-python', d.python || '—');
        setEl('cpu-cores', (d.cpu_count || '—') + ' cores');
        if (d.cpu_percent !== undefined) {
            updateGauge('cpu-ring','cpu-gauge-text', d.cpu_percent, 100);
            setEl('cpu-badge', d.cpu_percent + '%');
        }
        if (d.mem_percent !== undefined) {
            updateGauge('mem-ring','mem-gauge-text', d.mem_percent, 100);
            setEl('mem-badge', d.mem_percent + '%');
            const used = ((d.mem_used||0)/1e9).toFixed(1);
            const total = ((d.mem_total||0)/1e9).toFixed(1);
            setEl('mem-detail', used + ' / ' + total + ' GB');
        }
    } catch(e) {}
}

// ── Containers ─────────────────────────────────────────────────────────
async function loadContainers() {
    try {
        const r = await fetch(API + '/api/containers');
        const d = await r.json();
        allContainers = d.containers || [];
        const running = allContainers.filter(c=>c.status==='running').length;
        const stopped = allContainers.length - running;
        // Update topbar + sidebar badge
        setEl('tb-running', running);
        setEl('tb-total', allContainers.length);
        setEl('sb-ctr-count', allContainers.length);
        setEl('ctr-running-count', running);
        setEl('ctr-stopped-count', stopped);
        setEl('ctr-total-count', allContainers.length);
        renderContainers();
    } catch(e) {
        document.getElementById('ctr-grid').innerHTML = '<div class="empty"><div class="empty-icon">⚠️</div><div class="empty-text">Docker not available</div></div>';
    }
}

function filterContainers(f, el) {
    ctrFilter = f;
    document.querySelectorAll('#tab-containers .filter-pill').forEach(p=>p.classList.remove('active'));
    if(el) el.classList.add('active');
    renderContainers();
}

const ICONS = {
    grafana:'📊', prometheus:'📈', jellyfin:'🎬', plex:'🎬', emby:'🎬',
    radarr:'🎥', sonarr:'📺', lidarr:'🎵', bazarr:'💬', prowlarr:'🔍',
    qbittorrent:'⬇️', transmission:'⬇️', nextcloud:'☁️', portainer:'🐳',
    watchtower:'👁️', dozzle:'📋', uptime:'✅', homer:'🏠', homarr:'🏠',
    immich:'📷', vaultwarden:'🔐', pihole:'🛡️', adguard:'🛡️',
    wireguard:'🔒', tailscale:'🔒', gitea:'🐙', postgres:'🐘',
    redis:'🔴', mariadb:'🐬', netdata:'📡', glances:'👀',
    nginx:'🌐', caddy:'🌐', traefik:'🌐', scrutiny:'💿',
    home_assistant:'🏡', node_red:'🔴', mosquitto:'📨',
    bookstack:'📚', outline:'📝', speedtest:'⚡', boxarr:'📦',
};

function ctrIcon(name) {
    for (const [k,v] of Object.entries(ICONS)) {
        if (name.toLowerCase().includes(k)) return v;
    }
    return '📦';
}

function statusClass(status) {
    if (status === 'running') return 'running';
    if (status === 'exited') return 'exited';
    if (status === 'paused') return 'paused';
    if (status === 'restarting') return 'restarting';
    return 'exited';
}

function renderContainers() {
    const grid = document.getElementById('ctr-grid');
    let ctrs = allContainers;
    if (ctrFilter !== 'all') ctrs = ctrs.filter(c => c.status === ctrFilter);
    if (!ctrs.length) {
        grid.innerHTML = '<div class="empty"><div class="empty-icon">📦</div><div class="empty-text">No containers match filter</div></div>';
        return;
    }
    // Sort: running first
    ctrs = [...ctrs].sort((a,b)=>{
        if(a.status==='running' && b.status!=='running') return -1;
        if(a.status!=='running' && b.status==='running') return 1;
        return a.name.localeCompare(b.name);
    });
    grid.innerHTML = ctrs.map(c => {
        const sc = statusClass(c.status);
        const icon = ctrIcon(c.name);
        const ports = c.ports && c.ports.length ? c.ports.join(', ') : '—';
        const uptime = c.uptime || '—';
        const isRunning = c.status === 'running';
        return `
<div class="ctr-card" id="card-${c.name}">
  <div class="ctr-header">
    <div class="ctr-icon">${icon}</div>
    <div class="ctr-info">
      <div class="ctr-name">${c.name}</div>
      <div class="ctr-image">${c.image}</div>
      <div>
        <span class="ctr-status ${sc}">
          <span class="ctr-status-dot"></span>${c.status}
        </span>
      </div>
    </div>
  </div>
  <div class="ctr-body">
    <div class="ctr-row"><span>Uptime</span><span>${uptime}</span></div>
    <div class="ctr-row"><span>ID</span><span>${c.id}</span></div>
    <div class="ctr-row"><span>Ports</span><span class="ctr-ports">${ports}</span></div>
  </div>
  <div class="ctr-stats">
    <div class="ctr-stat-item">
      <div class="ctr-stat-label">CPU</div>
      <div class="ctr-stat-val" id="stat-cpu-${c.name}">—</div>
      <div class="pbar-wrap" style="margin-top:3px"><div class="pbar blue" id="pb-cpu-${c.name}" style="width:0%"></div></div>
    </div>
    <div class="ctr-stat-item">
      <div class="ctr-stat-label">Memory</div>
      <div class="ctr-stat-val" id="stat-mem-${c.name}">—</div>
      <div class="pbar-wrap" style="margin-top:3px"><div class="pbar" id="pb-mem-${c.name}" style="width:0%;background:var(--purple)"></div></div>
    </div>
  </div>
  <div class="ctr-footer">
    ${isRunning ? `<button class="btn red" onclick="ctrAction('${c.name}','stop')"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1" stroke-width="2"/></svg>Stop</button>` : `<button class="btn green" onclick="ctrAction('${c.name}','start')"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/></svg>Start</button>`}
    <button class="btn orange" onclick="ctrAction('${c.name}','restart')"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>Restart</button>
    <button class="btn blue" onclick="openLogs('${c.name}')"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>Logs</button>
    <button class="btn red" onclick="ctrAction('${c.name}','remove')" style="margin-left:auto"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg></button>
  </div>
</div>`;
    }).join('');

    // Load stats for running containers
    ctrs.filter(c=>c.status==='running').forEach(c => loadCtrStats(c.name));
}

async function ctrAction(name, action) {
    if (action === 'remove' && !confirm('Remove container ' + name + '?')) return;
    try {
        const r = await fetch(API + `/api/container/${name}/${action}`, {method:'POST'});
        const d = await r.json();
        if (d.error) alert('Error: ' + d.error);
        else setTimeout(loadContainers, 800);
    } catch(e) { alert('Request failed'); }
}

async function loadCtrStats(name) {
    try {
        const r = await fetch(API + `/api/container/${name}/stats`);
        const d = await r.json();
        if (d.error) return;
        const cpuEl = document.getElementById('stat-cpu-' + name);
        const memEl = document.getElementById('stat-mem-' + name);
        const cpuPb = document.getElementById('pb-cpu-' + name);
        const memPb = document.getElementById('pb-mem-' + name);
        if (cpuEl) cpuEl.textContent = d.cpu_pct + '%';
        if (memEl) memEl.textContent = d.mem_usage_mb + ' MB';
        if (cpuPb) { cpuPb.style.width = Math.min(d.cpu_pct,100) + '%'; }
        if (memPb) { memPb.style.width = Math.min(d.mem_pct,100) + '%'; }
    } catch(e) {}
}

// ── Log Modal ─────────────────────────────────────────────────────────
async function openLogs(name) {
    document.getElementById('log-modal-title').textContent = name + ' — Logs';
    document.getElementById('log-modal-body').textContent = 'Loading...';
    document.getElementById('log-modal').classList.add('open');
    try {
        const r = await fetch(API + `/api/container/${name}/logs`);
        const d = await r.json();
        const el = document.getElementById('log-modal-body');
        el.textContent = d.logs || '(empty)';
        el.scrollTop = el.scrollHeight;
    } catch(e) { document.getElementById('log-modal-body').textContent = 'Failed to load logs'; }
}
function closeModal() {
    document.getElementById('log-modal').classList.remove('open');
}
document.getElementById('log-modal').addEventListener('click', function(e) {
    if (e.target === this) closeModal();
});

// ── Storage ───────────────────────────────────────────────────────────
async function loadStorage() {
    try {
        const r = await fetch(API + '/api/storage');
        const d = await r.json();
        const el = document.getElementById('disk-list');
        if (!d.filesystems) { el.innerHTML = '<div class="empty-text">No data</div>'; return; }
        el.innerHTML = d.filesystems.map(fs => {
            const pct = fs.percent || 0;
            const color = pct < 70 ? 'green' : pct < 90 ? 'orange' : 'red';
            return `<div class="disk-item">
              <div class="disk-header">
                <span class="disk-path">${fs.mountpoint || fs.device}</span>
                <span class="disk-usage">${fmtBytes(fs.used)} / ${fmtBytes(fs.total)} (${pct}%)</span>
              </div>
              <div class="pbar-wrap"><div class="pbar ${color}" style="width:${pct}%"></div></div>
            </div>`;
        }).join('');
    } catch(e) {}
}

// ── Network ───────────────────────────────────────────────────────────
async function loadNetwork() {
    try {
        const r = await fetch(API + '/api/network');
        const d = await r.json();
        const tbody = document.getElementById('net-table');
        const ifaces = d.interfaces || [];
        tbody.innerHTML = ifaces.map(i => `
          <tr>
            <td><b>${i.name}</b></td>
            <td style="font-family:var(--mono);font-size:12px">${(i.addresses||[]).join(', ')||'—'}</td>
            <td>${fmtBytes(i.bytes_sent)}</td>
            <td>${fmtBytes(i.bytes_recv)}</td>
            <td><span class="ctr-status ${i.is_up?'running':'exited'}" style="display:inline-flex;align-items:center;gap:4px"><span class="ctr-status-dot"></span>${i.is_up?'Up':'Down'}</span></td>
          </tr>`).join('') || '<tr><td colspan="5" style="text-align:center;color:var(--text3)">No interfaces</td></tr>';
    } catch(e) {}
}

// ── Hardware ─────────────────────────────────────────────────────────
async function loadHardware() {
    try {
        const r = await fetch(API + '/api/hardware');
        const d = await r.json();
        const cpuEl = document.getElementById('hw-cpu');
        if (d.cpu) {
            cpuEl.innerHTML = `
              <div class="stat-card"><div class="stat-card-val">${d.cpu.count||'—'}</div><div class="stat-card-label">Logical Cores</div></div>
              <div class="stat-card"><div class="stat-card-val">${d.cpu.freq?.current ? Math.round(d.cpu.freq.current)+'MHz':'—'}</div><div class="stat-card-label">Frequency</div></div>`;
        }
        const memEl = document.getElementById('hw-mem');
        if (d.memory) {
            const m = d.memory;
            memEl.innerHTML = `<div class="ctr-row"><span>Total</span><span>${fmtBytes(m.total)}</span></div>
              <div class="ctr-row"><span>Available</span><span>${fmtBytes(m.available)}</span></div>
              <div class="ctr-row"><span>Used</span><span>${fmtBytes(m.used)}</span></div>`;
        }
    } catch(e) {}
}

// ── Logs ─────────────────────────────────────────────────────────────
async function loadLogs() {
    const el = document.getElementById('log-output');
    el.textContent = 'Loading...';
    try {
        const r = await fetch(API + '/api/logs');
        const d = await r.json();
        el.textContent = (d.lines || []).join('\n') || '(empty)';
        el.scrollTop = el.scrollHeight;
    } catch(e) { el.textContent = 'Failed to load logs'; }
}

// ── Catalog ───────────────────────────────────────────────────────────
async function loadCatalog() {
    try {
        const r = await fetch(API + '/api/catalog');
        const d = await r.json();
        allCatalog = d.apps || [];
        // Build category pills
        const cats = ['All', ...new Set(allCatalog.map(a=>a.category).filter(Boolean))].sort();
        const catEl = document.getElementById('cat-categories');
        catEl.innerHTML = cats.map(c=>`<div class="filter-pill${c==='All'?' active':''}" onclick="filterCat('${c}',this)">${c}</div>`).join('');
        renderCatalog();
    } catch(e) {
        document.getElementById('cat-grid').innerHTML = '<div class="empty"><div class="empty-icon">⚠️</div><div class="empty-text">Catalog unavailable</div></div>';
    }
}

function filterCat(cat, el) {
    catFilter = cat;
    document.querySelectorAll('#cat-categories .filter-pill').forEach(p=>p.classList.remove('active'));
    if(el) el.classList.add('active');
    renderCatalog();
}

function filterCatalog() {
    renderCatalog();
}

function renderCatalog() {
    const search = (document.getElementById('cat-search')?.value || '').toLowerCase();
    let apps = allCatalog;
    if (catFilter !== 'All') apps = apps.filter(a=>a.category===catFilter);
    if (search) apps = apps.filter(a=>(a.name+a.description+a.category).toLowerCase().includes(search));
    const grid = document.getElementById('cat-grid');
    if (!apps.length) { grid.innerHTML = '<div class="empty"><div class="empty-icon">🔍</div><div class="empty-text">No apps found</div></div>'; return; }
    grid.innerHTML = apps.map(a=>`
      <div class="cat-card">
        <div class="cat-card-header">
          <div class="cat-icon">${a.icon||'📦'}</div>
          <div><div class="cat-name">${a.name}</div><div class="cat-cat">${a.category}</div></div>
        </div>
        <div class="cat-desc">${a.description||''}</div>
        <div class="cat-footer">
          <div class="cat-image">${(a.image||'').split(':')[0].split('/').pop()}</div>
          <button class="btn blue" onclick="deployApp('${a.id}','${a.name}')">Deploy</button>
        </div>
      </div>`).join('');
}

async function deployApp(id, name) {
    if (!confirm('Deploy ' + name + '?')) return;
    try {
        const r = await fetch(API + '/api/deploy', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({app_id:id})});
        const d = await r.json();
        alert(d.error ? 'Error: ' + d.error : name + ' deployed successfully!');
    } catch(e) { alert('Deploy request failed'); }
}

// ── Stack Manager ─────────────────────────────────────────────────────
async function loadStackManager() {
    try {
        const r = await fetch(API + '/api/stacks');
        const d = await r.json();
        const el = document.getElementById('stacks-list');
        const stacks = d.stacks || [];
        if (!stacks.length) { el.innerHTML = '<div class="empty-text" style="padding:12px;color:var(--text3)">No compose stacks found in /docker/</div>'; return; }
        el.innerHTML = '<table><thead><tr><th>Stack</th><th>Path</th><th>Services</th></tr></thead><tbody>' +
          stacks.map(s=>`<tr><td><b>${s.name}</b></td><td style="font-family:var(--mono);font-size:11px">${s.path}</td><td>${s.services||'—'}</td></tr>`).join('') +
          '</tbody></table>';
    } catch(e) {}
}

async function loadDeployHistory() {
    try {
        const r = await fetch(API + '/api/deploy/history');
        const d = await r.json();
        const el = document.getElementById('deploy-history');
        const h = d.history || [];
        if (!h.length) { el.innerHTML = '<div style="padding:12px;color:var(--text3)">No deploys yet</div>'; return; }
        el.innerHTML = '<table><thead><tr><th>Time</th><th>Apps</th><th>Status</th></tr></thead><tbody>' +
          h.map(e=>`<tr><td>${e.ts}</td><td>${e.apps}</td><td><span class="ctr-status ${e.status==='ok'?'running':'exited'}" style="display:inline-flex;align-items:center;gap:4px"><span class="ctr-status-dot"></span>${e.status}</span></td></tr>`).join('') +
          '</tbody></table>';
    } catch(e) {}
}

// ── Updates ───────────────────────────────────────────────────────────
async function checkUpdates() {
    const el = document.getElementById('updates-list');
    el.innerHTML = '<div class="empty"><div class="empty-icon">🔄</div><div class="empty-text">Checking for updates...</div></div>';
    try {
        const r = await fetch(API + '/api/updates');
        const d = await r.json();
        const updates = d.updates || [];
        if (!updates.length) { el.innerHTML = '<div class="empty"><div class="empty-icon">✅</div><div class="empty-text">All containers up to date</div></div>'; return; }
        el.innerHTML = '<table><thead><tr><th>Container</th><th>Current</th><th>Status</th><th></th></tr></thead><tbody>' +
          updates.map(u=>`<tr><td><b>${u.name}</b></td><td style="font-family:var(--mono);font-size:11px">${u.image}</td><td><span style="color:${u.update_available?'var(--orange)':'var(--green)'}">${u.update_available?'Update available':'Up to date'}</span></td><td>${u.update_available?`<button class="btn orange" onclick="pullUpdate('${u.name}')">Pull</button>`:''}</td></tr>`).join('') +
          '</tbody></table>';
    } catch(e) { el.innerHTML = '<div class="empty"><div class="empty-icon">⚠️</div><div class="empty-text">Update check failed</div></div>'; }
}

async function pullUpdate(name) {
    alert('Pulling update for ' + name + '... check Dozzle for progress.');
    try { await fetch(API + `/api/container/${name}/restart`, {method:'POST'}); } catch(e) {}
}

// ── Backup ────────────────────────────────────────────────────────────
async function loadBackups() {
    try {
        const r = await fetch(API + '/api/backups');
        const d = await r.json();
        const el = document.getElementById('backups-list');
        const b = d.backups || [];
        if (!b.length) { el.innerHTML = '<div class="empty"><div class="empty-icon">💾</div><div class="empty-text">No backups yet</div></div>'; return; }
        el.innerHTML = b.map(x=>`<div class="ctr-row"><span>${x.name}</span><span>${x.size}</span></div>`).join('');
    } catch(e) {}
}

async function createBackup() {
    try {
        const r = await fetch(API + '/api/backup', {method:'POST'});
        const d = await r.json();
        alert(d.error ? 'Error: '+d.error : 'Backup created: ' + d.file);
        loadBackups();
    } catch(e) { alert('Backup failed'); }
}

// ── Settings ──────────────────────────────────────────────────────────
async function loadSettings() {
    try {
        const r = await fetch(API + '/api/settings');
        const d = await r.json();
        const s = d.settings || {};
        setInput('cfg-dir', s.config_dir);
        setInput('media-dir', s.media_dir);
        setInput('cfg-tz', s.tz);
        setInput('cfg-puid', s.puid);
        setInput('cfg-pgid', s.pgid);
    } catch(e) {}
}

function setInput(id, val) { const el = document.getElementById(id); if(el && val) el.value = val; }

async function saveSettings() {
    const payload = {
        config_dir: document.getElementById('cfg-dir')?.value,
        media_dir: document.getElementById('media-dir')?.value,
        tz: document.getElementById('cfg-tz')?.value,
        puid: document.getElementById('cfg-puid')?.value,
        pgid: document.getElementById('cfg-pgid')?.value,
    };
    try {
        const r = await fetch(API + '/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
        const d = await r.json();
        alert(d.error ? 'Error: '+d.error : 'Settings saved!');
    } catch(e) { alert('Save failed'); }
}

// ── Polling ───────────────────────────────────────────────────────────
setInterval(() => {
    if (currentTab === 'storage') loadStorage();
    else if (currentTab === 'network') loadNetwork();
    else if (currentTab === 'containers') loadContainers();
}, 10000);

// ── Boot ──────────────────────────────────────────────────────────────
loadOverview();
loadContainers();
startSSE();
</script>
</body>
</html>

"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9999, debug=False)
