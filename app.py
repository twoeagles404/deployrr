#!/usr/bin/env python3
#
"""
ArrHub Monitor — Enhanced Server Administration Dashboard
Version: 3.10.0 · Full deployment, update management, and real-time monitoring
Port: 9999

Dependencies:
  pip install flask psutil requests docker

"""
import json, os, re, subprocess, shutil, time, glob, threading, xml.etree.ElementTree as ET, sqlite3, socket
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

# ── Docker CLI binary discovery ───────────────────────────────────────────────
# The WebUI runs inside a container; the docker CLI binary is copied from the
# official docker:27-cli image in the Dockerfile so compose commands work.
# shutil.which searches PATH; fallback list covers common install locations.
def _find_docker_bin():
    found = shutil.which("docker")
    if found:
        return found
    for p in ["/usr/local/bin/docker", "/usr/bin/docker", "/snap/bin/docker"]:
        if os.path.isfile(p):
            return p
    return "docker"   # last resort — will raise FileNotFoundError if absent

_DOCKER_BIN = _find_docker_bin()

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
    "/opt/arrhub/apps/catalog.json",                          # runtime volume mount
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
DB_PATH = os.environ.get("ARRHUB_DB", "/data/arrhub.db")
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
            "hostname": os.uname().nodename,
            "os": os.uname().sysname,
            "kernel": os.uname().release,
            "arch": os.uname().machine,
            "python": "3.12",
            "cpu_count": psutil.cpu_count(),
            "cpu_percent": cpu_pct,
            "mem_percent": mem.percent,
            "mem_used": mem.used,
            "mem_total": mem.total,
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
        filesystems = []
        for part in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(part.mountpoint)
                filesystems.append({
                    "mountpoint": part.mountpoint,
                    "device": part.device,
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                    "percent": usage.percent,
                    "fstype": part.fstype
                })
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
            "filesystems": filesystems,
            "io": io_data
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/network")
def api_network():
    """Network interfaces and statistics."""
    try:
        interfaces = []
        addrs_by_name = psutil.net_if_addrs()
        stats_by_name = psutil.net_if_stats()
        io_by_name = psutil.net_io_counters(pernic=True)

        for name, addrs in addrs_by_name.items():
            stats = stats_by_name.get(name, None)
            io = io_by_name.get(name, None)

            addresses = [addr.address for addr in addrs if addr.address and isinstance(addr.address, str)]

            interfaces.append({
                "name": name,
                "addresses": addresses,
                "is_up": stats.isup if stats else False,
                "bytes_sent": io.bytes_sent if io else 0,
                "bytes_recv": io.bytes_recv if io else 0,
                "packets_sent": io.packets_sent if io else 0,
                "packets_recv": io.packets_recv if io else 0
            })

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

@app.route("/api/container/<cname>/update", methods=["POST"])
@require_auth
def api_container_update(cname):
    """Pull latest image for a container and recreate it with the same config."""
    if not DOCKER_OK:
        return jsonify({"error": "Docker not available"}), 500
    try:
        container = _dc.containers.get(cname)
        image_name = container.image.tags[0] if container.image.tags else None
        if not image_name:
            return jsonify({"error": "No image tag found — cannot pull"}), 400

        # Capture current config before stopping
        cfg = container.attrs.get("HostConfig", {})
        net_cfg = container.attrs.get("NetworkSettings", {})
        inspect = container.attrs

        # Pull the latest image
        _dc.images.pull(image_name)

        # Gather restart policy
        restart_policy = cfg.get("RestartPolicy", {"Name": "unless-stopped"})

        # Gather port bindings  {container_port/proto: host_port}
        port_bindings = cfg.get("PortBindings") or {}

        # Gather volume bindings
        binds = cfg.get("Binds") or []

        # Gather env
        env = inspect.get("Config", {}).get("Env") or []

        # Stop and remove existing container
        try:
            container.stop(timeout=15)
        except Exception:
            pass
        try:
            container.remove()
        except Exception:
            pass

        # Recreate with same config
        new_container = _dc.containers.run(
            image_name,
            name=cname,
            detach=True,
            ports=port_bindings if port_bindings else None,
            volumes=binds if binds else None,
            environment=env if env else None,
            restart_policy=restart_policy,
        )
        return jsonify({
            "status": "updated",
            "name": cname,
            "image": image_name,
            "container_id": new_container.short_id,
        })
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
    """System logs from Docker containers, ArrHub logs, and host."""
    try:
        lines_count = request.args.get('lines', 100, type=int)
        unit = request.args.get('unit', '')

        log_lines = []

        if unit and DOCKER_OK:
            try:
                c = _dc.containers.get(unit)
                logs = c.logs(tail=lines_count, timestamps=True).decode('utf-8', errors='replace')
                log_lines = logs.strip().split('\n') if logs.strip() else []
            except Exception:
                pass
        elif DOCKER_OK:
            # Get recent logs from all running containers
            try:
                for c in _dc.containers.list():
                    try:
                        logs = c.logs(tail=10, timestamps=True).decode('utf-8', errors='replace')
                        for line in logs.strip().split('\n'):
                            if line.strip():
                                log_lines.append(f"[{c.name}] {line}")
                    except Exception:
                        pass
            except Exception:
                pass

        # Read ArrHub application logs (always attempt)
        arrhub_logs = ["/var/log/arrhub.log", "/var/log/arrhub-errors.log"]
        for logpath in arrhub_logs:
            if os.path.isfile(logpath):
                try:
                    with open(logpath, 'r') as f:
                        lines = f.readlines()
                        tail = lines[-min(len(lines), lines_count // 2):]
                        for line in tail:
                            stripped = line.strip()
                            if stripped:
                                tag = "[arrhub]" if "arrhub.log" in logpath else "[arrhub-errors]"
                                log_lines.append(f"{tag} {stripped}")
                except Exception:
                    pass

        # Try journalctl as fallback
        if not log_lines:
            try:
                result = subprocess.run(
                    ["journalctl", "--no-pager", "-n", str(lines_count), "--output=short-iso"],
                    capture_output=True, text=True, timeout=5
                )
                if result.stdout.strip():
                    log_lines = result.stdout.strip().split('\n')[-lines_count:]
            except Exception:
                pass

        # Dmesg as final fallback
        if not log_lines:
            try:
                result = subprocess.run(["dmesg", "-T"],
                    capture_output=True, text=True, timeout=5)
                if result.stdout.strip():
                    log_lines = result.stdout.strip().split('\n')[-lines_count:]
            except Exception:
                pass

        if not log_lines:
            log_lines = ["No logs available. Container logs will appear here when Docker is running."]

        # Sort and return last N
        log_lines.sort()
        log_lines = log_lines[-lines_count:]

        return jsonify({"lines": log_lines})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/catalog")
def api_catalog():
    """Get application catalog."""
    return jsonify({"apps": list(APP_REGISTRY.values())})

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

# ── Port conflict detection ──────────────────────────────────────────────────
def _port_in_use(port: int) -> bool:
    """Check if a TCP port is in use on the host."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(("0.0.0.0", port)) == 0
    except Exception:
        return False

def _find_free_port(start_port: int, max_tries: int = 50) -> int:
    """Find next available port starting from start_port."""
    for offset in range(max_tries):
        candidate = start_port + offset
        if not _port_in_use(candidate):
            return candidate
    # Fallback: random high port
    import random
    return random.randint(20000, 30000)

def _resolve_port_mapping(mapping: str) -> tuple:
    """Resolve a port mapping like '8080:80' to avoid conflicts.
    Returns (resolved_mapping, original_host_port, new_host_port, changed)."""
    parts = mapping.split(":")
    if len(parts) != 2:
        return mapping, None, None, False
    try:
        host_port = int(parts[0])
        container_port = parts[1]
    except ValueError:
        return mapping, None, None, False

    if _port_in_use(host_port):
        new_port = _find_free_port(host_port + 1)
        return f"{new_port}:{container_port}", host_port, new_port, True
    return mapping, host_port, host_port, False

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

    # Build compose snippet (for logging/history only)
    ports_yaml = "\n".join(f"      - \"{p}\"" for p in app_data.get("ports", []))
    vols_yaml = "\n".join(f"      - \"{replace_placeholders(v)}\"" for v in app_data.get("volumes", []))
    env_yaml = "\n".join(f"      - \"{replace_placeholders(e)}\"" for e in app_data.get("environment", []))

    snippet = f"""  {app_id}:
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

    compose_content = f"services:\n{snippet}"

    # Write compose file to app's config directory
    app_dir = os.path.join(config_dir, app_id)
    os.makedirs(app_dir, exist_ok=True)
    compose_path = os.path.join(app_dir, "docker-compose.yml")
    with open(compose_path, "w") as f:
        f.write(compose_content)

    if not DOCKER_OK:
        return jsonify({"error": "Docker is not available. Ensure the Docker socket is mounted."}), 500

    try:
        image_name = app_data["image"]

        # Pull the image first
        try:
            _dc.images.pull(image_name)
        except Exception as pull_err:
            return jsonify({"error": f"Failed to pull image {image_name}: {str(pull_err)}"}), 500

        # Remove existing container with the same name
        try:
            existing = _dc.containers.get(app_id)
            existing.stop(timeout=10)
            existing.remove()
        except Exception:
            pass

        # Build port bindings with conflict resolution
        port_bindings = {}
        port_changes = []
        for port_str in app_data.get("ports", []):
            parts = str(port_str).split(":")
            if len(parts) == 2:
                resolved, orig, new_port, changed = _resolve_port_mapping(str(port_str))
                if changed:
                    port_changes.append({"original": orig, "assigned": new_port})
                r_parts = resolved.split(":")
                h_port = int(r_parts[0])
                c_port_str = r_parts[1]
                proto = "tcp"
                if "/" in c_port_str:
                    c_port_str, proto = c_port_str.split("/")
                c_port = int(c_port_str)
                port_bindings[f"{c_port}/{proto}"] = h_port

        # Build volume bindings
        binds = []
        for vol_str in app_data.get("volumes", []):
            replaced = replace_placeholders(vol_str)
            parts = replaced.split(":")
            if len(parts) >= 2:
                host_path = parts[0]
                container_path = parts[1]
                mode = parts[2] if len(parts) > 2 else "rw"
                os.makedirs(host_path, exist_ok=True)
                try:
                    os.chmod(host_path, 0o777)
                except Exception:
                    pass
                binds.append(f"{host_path}:{container_path}:{mode}")

        # Build environment
        environment = {}
        for env_str in app_data.get("environment", []):
            replaced = replace_placeholders(env_str)
            if "=" in replaced:
                key, val = replaced.split("=", 1)
                environment[key] = val

        restart_val = app_data.get("restart", "unless-stopped")

        container = _dc.containers.run(
            image_name,
            name=app_id,
            detach=True,
            ports=port_bindings if port_bindings else None,
            volumes=binds if binds else None,
            environment=environment if environment else None,
            restart_policy={"Name": restart_val}
        )

        status = "success"
        error = None

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
            "container_id": container.short_id,
            "compose_path": compose_path,
            "port_changes": port_changes,
            "error": error
        })
    except Exception as e:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "INSERT INTO deploy_history (app_id, app_name, action, status, compose_snapshot, error) VALUES (?,?,?,?,?,?)",
                    (app_id, app_data["name"], "deploy", "failed", compose_content, str(e))
                )
                conn.commit()
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500

@app.route("/api/deploy/history")
def api_deploy_history_alias():
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

@app.route("/api/ports/map")
def api_ports_map():
    """Get a complete map of all port assignments across all containers."""
    if not DOCKER_OK:
        return jsonify({"error": "Docker not available"}), 500

    port_map = []
    used_ports = set()

    try:
        for c in _dc.containers.list(all=True):
            container_ports = c.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
            for container_port, bindings in container_ports.items():
                if bindings:
                    for binding in bindings:
                        host_port = binding.get("HostPort")
                        host_ip = binding.get("HostIp", "0.0.0.0")
                        if host_port:
                            port_map.append({
                                "container": c.name,
                                "status": c.status,
                                "host_port": int(host_port),
                                "host_ip": host_ip,
                                "container_port": container_port,
                                "image": c.image.tags[0] if c.image.tags else "unknown",
                            })
                            used_ports.add(int(host_port))
                else:
                    # Port exposed but not bound to host
                    port_map.append({
                        "container": c.name,
                        "status": c.status,
                        "host_port": None,
                        "host_ip": None,
                        "container_port": container_port,
                        "image": c.image.tags[0] if c.image.tags else "unknown",
                    })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Sort by host port (None last)
    port_map.sort(key=lambda x: (x["host_port"] is None, x["host_port"] or 99999))

    return jsonify({
        "ports": port_map,
        "used_ports": sorted(list(used_ports)),
        "total_bindings": len([p for p in port_map if p["host_port"]]),
    })

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
        "settings": {
            "config_dir": _db_get("config_dir", "/docker"),
            "media_dir": _db_get("media_dir", "/mnt/media"),
            "tz": _db_get("tz", "America/New_York"),
            "puid": _db_get("puid", "1000"),
            "pgid": _db_get("pgid", "1000"),
            "no_auth": _NO_AUTH,
            "version": "3.14.0",
            # Service integration keys — returned so the UI can re-populate fields on revisit
            "radarr_url":     _db_get("radarr_url", ""),
            "radarr_api_key": _db_get("radarr_api_key", ""),
            "sonarr_url":     _db_get("sonarr_url", ""),
            "sonarr_api_key": _db_get("sonarr_api_key", ""),
            "plex_url":       _db_get("plex_url", ""),
            "plex_token":     _db_get("plex_token", ""),
            "seerr_url":      _db_get("seerr_url", ""),
            "seerr_api_key":  _db_get("seerr_api_key", ""),
        }
    })

@app.route("/api/settings", methods=["POST"])
@require_auth
def api_settings_set():
    """Save settings."""
    data = request.json or {}
    allowed = [
        "config_dir", "media_dir", "tz", "puid", "pgid",
        # Service integration keys
        "radarr_url", "radarr_api_key",
        "sonarr_url", "sonarr_api_key",
        "plex_url", "plex_token",
        "seerr_url", "seerr_api_key",
    ]
    for key in allowed:
        if key in data:
            _db_set(key, data[key])
    return jsonify({"status": "saved"})

@app.route("/api/updates")
def api_updates():
    """Check which containers have image updates available."""
    if not DOCKER_OK:
        return jsonify({"error": "Docker not available"}), 500
    updates = []
    try:
        containers = _dc.containers.list()
        for c in containers:
            image_tag = c.image.tags[0] if c.image.tags else "unknown"
            updates.append({
                "name": c.name,
                "image": image_tag,
                "status": c.status,
                "update_available": False
            })
        return jsonify({"updates": updates, "checked_at": datetime.now().isoformat()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
    backup_name = f"arrhub_backup_{timestamp}.tar.gz"
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

@app.route("/api/backups")
def api_backups():
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
    config_dir = _db_get("config_dir", "/docker")
    search_dirs = [config_dir, "/docker", "/opt/arrhub/docker"]
    searched = set()

    # Search for docker-compose.yml files on disk
    for sdir in search_dirs:
        if sdir in searched or not os.path.exists(sdir):
            continue
        searched.add(sdir)
        try:
            for item in glob.glob(os.path.join(sdir, "*", "docker-compose.yml")):
                try:
                    stack_dir = os.path.dirname(item)
                    stack_name = os.path.basename(stack_dir)
                    with open(item) as f:
                        content = f.read()
                    # Count services by looking for "image:" lines
                    service_count = content.count("image:")
                    stacks.append({
                        "name": stack_name,
                        "path": item,
                        "services": max(1, service_count),
                        "source": "file"
                    })
                except Exception:
                    pass
        except Exception:
            pass

    # Discover running compose projects from Docker labels
    if DOCKER_OK:
        try:
            projects = {}
            for c in _dc.containers.list(all=True):
                project = c.labels.get("com.docker.compose.project", "")
                if project:
                    if project not in projects:
                        projects[project] = {
                            "name": project,
                            "path": c.labels.get("com.docker.compose.project.working_dir", ""),
                            "services": 0,
                            "source": "docker"
                        }
                    projects[project]["services"] += 1

            # Merge: if a project was found on disk, update its service count
            existing_names = {s["name"] for s in stacks}
            for name, proj in projects.items():
                if name not in existing_names:
                    stacks.append(proj)
                else:
                    for s in stacks:
                        if s["name"] == name:
                            s["services"] = max(s["services"], proj["services"])
                            break
        except Exception:
            pass

    return jsonify({"stacks": stacks})

@app.route("/api/stack/<name>/compose")
def api_stack_compose_named(name):
    """Get compose file content for a stack."""
    config_dir = _db_get("config_dir", "/docker")
    compose_path = os.path.join(config_dir, name, "docker-compose.yml")
    if not os.path.isfile(compose_path):
        return jsonify({"error": "Compose file not found"}), 404
    try:
        with open(compose_path) as f:
            content = f.read()
        return jsonify({"name": name, "path": compose_path, "content": content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stack/<name>/up", methods=["POST"])
def api_stack_up(name):
    """Start a stack — uses Docker SDK to start containers belonging to this stack.
    Falls back to `docker compose` CLI if the binary is available."""
    if not DOCKER_OK:
        return jsonify({"error": "Docker not available"}), 500

    config_dir = _db_get("config_dir", "/docker")
    compose_path = os.path.join(config_dir, name, "docker-compose.yml")

    # ── Try CLI first (available when running via rebuilt image) ──────────────
    if os.path.isfile(_DOCKER_BIN):
        try:
            result = subprocess.run(
                [_DOCKER_BIN, "compose", "-f", compose_path, "up", "-d"],
                capture_output=True, text=True, timeout=120
            )
            return jsonify({
                "status": "ok" if result.returncode == 0 else "error",
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-2000:]
            })
        except FileNotFoundError:
            pass  # fall through to SDK method

    # ── SDK fallback: start containers that belong to this project ────────────
    if not os.path.isfile(compose_path):
        return jsonify({"error": "Compose file not found and docker CLI unavailable"}), 404
    try:
        started, errors = [], []
        for c in _dc.containers.list(all=True):
            project = c.labels.get("com.docker.compose.project", "")
            if project == name and c.status != "running":
                try:
                    c.start()
                    started.append(c.name)
                except Exception as ex:
                    errors.append(f"{c.name}: {ex}")
        return jsonify({
            "status": "ok" if not errors else "partial",
            "started": started,
            "errors": errors
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stack/<name>/down", methods=["POST"])
def api_stack_down(name):
    """Stop a stack — uses Docker SDK to stop containers belonging to this stack.
    Falls back to `docker compose` CLI if the binary is available."""
    if not DOCKER_OK:
        return jsonify({"error": "Docker not available"}), 500

    config_dir = _db_get("config_dir", "/docker")
    compose_path = os.path.join(config_dir, name, "docker-compose.yml")

    # ── Try CLI first ─────────────────────────────────────────────────────────
    if os.path.isfile(_DOCKER_BIN):
        try:
            result = subprocess.run(
                [_DOCKER_BIN, "compose", "-f", compose_path, "down"],
                capture_output=True, text=True, timeout=120
            )
            return jsonify({
                "status": "ok" if result.returncode == 0 else "error",
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-2000:]
            })
        except FileNotFoundError:
            pass  # fall through to SDK method

    # ── SDK fallback: stop + remove containers that belong to this project ────
    try:
        stopped, errors = [], []
        for c in _dc.containers.list(all=True):
            project = c.labels.get("com.docker.compose.project", "")
            # Also match by container name prefix (single-app stacks)
            if project == name or c.name == name:
                try:
                    if c.status == "running":
                        c.stop(timeout=10)
                    stopped.append(c.name)
                except Exception as ex:
                    errors.append(f"{c.name}: {ex}")
        return jsonify({
            "status": "ok" if not errors else "partial",
            "stopped": stopped,
            "errors": errors
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stack/<name>/pull", methods=["POST"])
def api_stack_pull(name):
    """Pull latest images for a stack without restarting it."""
    if not DOCKER_OK:
        return jsonify({"error": "Docker not available"}), 500
    config_dir = _db_get("config_dir", "/docker")
    compose_path = os.path.join(config_dir, name, "docker-compose.yml")
    if not os.path.isfile(compose_path):
        return jsonify({"error": f"Compose file not found: {compose_path}"}), 404
    try:
        result = subprocess.run(
            [_DOCKER_BIN, "compose", "-f", compose_path, "pull"],
            capture_output=True, text=True, timeout=300
        )
        return jsonify({
            "status": "ok" if result.returncode == 0 else "error",
            "stdout": result.stdout[-3000:],
            "stderr": result.stderr[-2000:]
        })
    except FileNotFoundError:
        return jsonify({"error": "docker CLI not found"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
    """Check for ArrHub updates."""
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

        # Get weather from open-meteo.
        # current= gives real-time humidity/wind; daily= gives 5-day forecast.
        weather_resp = requests.get(
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code,apparent_temperature"
            f"&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum"
            f"&wind_speed_unit=mph"
            f"&timezone=auto",
            timeout=5
        )
        weather = weather_resp.json()

        # Extract current conditions (humidity, wind, feels-like)
        current = weather.get("current", {})
        result = {
            "location": f"{geo.get('city', 'Unknown')}, {geo.get('country_name', '')}",
            "humidity": current.get("relative_humidity_2m"),
            "wind_mph": round(current.get("wind_speed_10m", 0), 1),
            "feels_like": current.get("apparent_temperature"),
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

@app.route("/api/docker/info")
def api_docker_info():
    """Docker system information."""
    if not DOCKER_OK:
        return jsonify({"error": "Docker not available"}), 500
    try:
        info = _dc.info()
        images = len(_dc.images.list())
        volumes = len(_dc.volumes.list())
        networks = len(_dc.networks.list())

        # Calculate disk usage from Docker
        disk_usage = "—"
        try:
            df = _dc.df()
            total_size = sum(img.get("Size", 0) for img in df.get("Images", []))
            total_size += sum(vol.get("UsageData", {}).get("Size", 0) for vol in df.get("Volumes", []))
            if total_size > 0:
                if total_size >= 1e9:
                    disk_usage = f"{total_size/1e9:.1f} GB"
                elif total_size >= 1e6:
                    disk_usage = f"{total_size/1e6:.1f} MB"
                else:
                    disk_usage = f"{total_size/1e3:.1f} KB"
        except Exception:
            pass

        return jsonify({
            "images": images,
            "volumes": volumes,
            "networks": networks,
            "containers_running": info.get("ContainersRunning", 0),
            "containers_total": info.get("Containers", 0),
            "docker_version": info.get("ServerVersion", ""),
            "disk_usage": disk_usage
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/rss/feeds")
def api_rss_feeds():
    """Return RSS feed categories and sources (no fetching — client fetches via /api/rss/fetch)."""
    feeds = {
        "World News": [
            {"name": "BBC World", "url": "https://feeds.bbci.co.uk/news/world/rss.xml", "icon": "🇬🇧"},
            {"name": "AP News", "url": "https://feeds.apnews.com/rss/apf-topnews", "icon": "🇺🇸"},
            {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml", "icon": "🌍"},
            {"name": "The Guardian", "url": "https://www.theguardian.com/world/rss", "icon": "🗞️"},
            {"name": "NPR News", "url": "https://feeds.npr.org/1001/rss.xml", "icon": "📻"},
            {"name": "DW World", "url": "https://rss.dw.com/xml/rss-en-all", "icon": "🇩🇪"},
        ],
        "Technology": [
            {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index", "icon": "💻"},
            {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml", "icon": "⚡"},
            {"name": "Hacker News", "url": "https://hnrss.org/frontpage", "icon": "🟠"},
            {"name": "TechCrunch", "url": "https://techcrunch.com/feed/", "icon": "🚀"},
            {"name": "9to5Mac", "url": "https://9to5mac.com/feed/", "icon": "🍎"},
        ],
        "Sports": [
            {"name": "BBC Sport", "url": "https://feeds.bbci.co.uk/sport/rss.xml", "icon": "⚽"},
            {"name": "BBC Football", "url": "https://feeds.bbci.co.uk/sport/football/rss.xml", "icon": "⚽"},
            {"name": "ESPN", "url": "https://www.espn.com/espn/rss/news", "icon": "🏈"},
            {"name": "Formula 1", "url": "https://www.formula1.com/content/fom-website/en/latest/all.xml", "icon": "🏎️"},
            {"name": "UFC / MMA", "url": "https://www.bloodyelbow.com/rss/current", "icon": "🥊"},
        ],
        "Science": [
            {"name": "NASA Breaking", "url": "https://www.nasa.gov/rss/dyn/breaking_news.rss", "icon": "🚀"},
            {"name": "Science Daily", "url": "https://www.sciencedaily.com/rss/all.xml", "icon": "🧪"},
            {"name": "Phys.org", "url": "https://phys.org/rss-feed/", "icon": "⚛️"},
            {"name": "Space.com", "url": "https://www.space.com/feeds/all", "icon": "🌌"},
        ],
        "Entertainment": [
            {"name": "Variety", "url": "https://variety.com/feed/", "icon": "🎬"},
            {"name": "IGN", "url": "https://feeds.ign.com/ign/all", "icon": "🎮"},
            {"name": "Eurogamer", "url": "https://www.eurogamer.net/?format=rss", "icon": "🕹️"},
            {"name": "Pitchfork", "url": "https://pitchfork.com/rss/news/", "icon": "🎸"},
        ],
        "Business": [
            {"name": "MarketWatch", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories", "icon": "📊"},
            {"name": "CNBC", "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "icon": "📈"},
            {"name": "Forbes", "url": "https://www.forbes.com/real-time/feed2/", "icon": "💰"},
            {"name": "Economist", "url": "https://www.economist.com/the-world-this-week/rss.xml", "icon": "💹"},
        ],
        "YouTube": [
            {"name": "Linus Tech Tips", "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCXuqSBlHAE6Xw-yeJA0Tunw", "icon": "▶️"},
            {"name": "Fireship", "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCsBjURrPoezykLs9EqgamOA", "icon": "🔥"},
            {"name": "NetworkChuck", "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UC9x0AN7BWHpCDHSm9NiJFJQ", "icon": "🌐"},
            {"name": "TechLinked", "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCeeFfhMcJa1kjtfZAGskOCA", "icon": "🔗"},
        ],
    }
    return jsonify({"categories": feeds})

@app.route("/api/rss/fetch")
def api_rss_fetch():
    """Proxy-fetch and parse an RSS/Atom feed URL to avoid CORS.
    Returns enriched items: title, link, date, excerpt, and thumbnail URL.
    Supports RSS 2.0, Atom, and YouTube Atom feeds.
    """
    import html as _html_mod
    import re as _re
    url = request.args.get("url", "")
    if not url:
        return jsonify({"error": "Missing url"}), 400

    # Per-feed response cache to avoid hammering external servers
    cache_key = "rss_fetch_" + url
    if cache_key in _rss_cache and (time.time() - _rss_cache[cache_key].get("ts", 0)) < 300:
        return jsonify(_rss_cache[cache_key]["data"])

    try:
        import urllib.request
        headers = {"User-Agent": "Mozilla/5.0 (compatible; ArrHub/3.5; +https://github.com/twoeagles404/arrhub)"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()

        root = ET.fromstring(raw)
        ns = {
            "atom":    "http://www.w3.org/2005/Atom",
            "media":   "http://search.yahoo.com/mrss/",
            "content": "http://purl.org/rss/1.0/modules/content/",
            "dc":      "http://purl.org/dc/elements/1.1/"
        }

        def _strip_html(text):
            """Strip HTML tags and decode entities for excerpt use."""
            if not text:
                return ""
            text = _re.sub(r"<[^>]+>", " ", text)
            text = _html_mod.unescape(text)
            text = _re.sub(r"\s+", " ", text).strip()
            return text[:200]

        def _first_img(text):
            """Find first <img src=...> in HTML string."""
            if not text:
                return None
            m = _re.search(r'<img[^>]+src=["\']([^"\']+)["\']', text, _re.I)
            return m.group(1) if m else None

        def _parse_item(item, is_atom=False):
            """Extract fields from a single RSS item or Atom entry."""
            # Title
            title_el = item.find("title") if not is_atom else item.find("atom:title", ns)
            title = (title_el.text or "").strip() if title_el is not None else "Untitled"
            title = _strip_html(title) or title   # some feeds wrap title in CDATA with HTML

            # Link
            link = "#"
            if not is_atom:
                link_el = item.find("link")
                link = (link_el.text or "").strip() if link_el is not None else "#"
            if not link or link == "#":
                atom_link = item.find("atom:link", ns)
                if atom_link is not None:
                    link = atom_link.get("href", "#")
            # YouTube Atom: yt:videoId → build link
            if not link or link == "#":
                vid_el = item.find("{http://www.youtube.com/xml/schemas/2015}videoId")
                if vid_el is not None and vid_el.text:
                    link = "https://www.youtube.com/watch?v=" + vid_el.text.strip()

            # Date
            date_el = item.find("pubDate") or item.find("dc:date", ns) or item.find("atom:updated", ns) or item.find("atom:published", ns)
            date = (date_el.text or "")[:16] if date_el is not None else ""

            # Thumbnail — priority order:
            # 1. <media:thumbnail url="...">
            # 2. <media:content url="..." medium="image">
            # 3. <enclosure url="..." type="image/...">
            # 4. First <img> in <description> or <content:encoded>
            # 5. YouTube video thumbnail via videoId
            thumb = None
            mt = item.find("media:thumbnail", ns)
            if mt is not None:
                thumb = mt.get("url")
            if not thumb:
                mc = item.find("media:content", ns)
                if mc is not None and ("image" in (mc.get("medium","") + mc.get("type",""))):
                    thumb = mc.get("url")
            if not thumb:
                enc = item.find("enclosure")
                if enc is not None and "image" in (enc.get("type","") or ""):
                    thumb = enc.get("url")
            if not thumb:
                for tag in ["content:encoded", "description"]:
                    raw_el = item.find(tag, ns) or item.find(tag)
                    if raw_el is not None and raw_el.text:
                        thumb = _first_img(raw_el.text)
                        if thumb:
                            break
            if not thumb:
                # YouTube fallback: extract videoId from link
                yt_match = _re.search(r"[?&]v=([A-Za-z0-9_-]{11})", link)
                if yt_match:
                    thumb = f"https://i.ytimg.com/vi/{yt_match.group(1)}/mqdefault.jpg"

            # Excerpt — from <description> or <content:encoded>
            excerpt = ""
            for tag in ["description", "content:encoded", "atom:summary", "atom:content"]:
                raw_el = item.find(tag, ns) or item.find(tag)
                if raw_el is not None and raw_el.text:
                    excerpt = _strip_html(raw_el.text)
                    if excerpt:
                        break

            return {"title": title, "link": link, "date": date,
                    "thumb": thumb, "excerpt": excerpt}

        items = []
        # Try RSS 2.0 items first, then Atom entries
        raw_items = root.findall(".//item")
        is_atom = False
        if not raw_items:
            raw_items = root.findall("atom:entry", ns)
            is_atom = True

        for item in raw_items[:20]:
            items.append(_parse_item(item, is_atom=is_atom))

        result = {"items": items}
        _rss_cache[cache_key] = {"data": result, "ts": time.time()}
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "items": []}), 200

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

@app.route("/api/ports/check")
def api_ports_check():
    """Check for port conflicts in running containers."""
    if not DOCKER_OK:
        return jsonify({"error": "Docker not available"}), 500

    try:
        port_usage = {}
        container_ports = []
        conflicts = []

        containers = _dc.containers.list()
        for c in containers:
            ports = c.ports or {}
            for key, val in ports.items():
                if val:
                    host_port = val[0].get("HostPort")
                    container_port = key.split('/')[0] if key else ""
                    if host_port:
                        port_num = int(host_port)
                        container_ports.append({
                            "host_port": port_num,
                            "container_name": c.name,
                            "container_port": container_port
                        })

                        if port_num not in port_usage:
                            port_usage[port_num] = []
                        port_usage[port_num].append(c.name)

        # Identify conflicts
        for port, containers_list in port_usage.items():
            if len(containers_list) > 1:
                conflicts.append({
                    "port": port,
                    "containers": containers_list
                })

        return jsonify({
            "ports": container_ports,
            "conflicts": conflicts,
            "total_ports": len(container_ports)
        })
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

# =============================================================================
# SERVICE INTEGRATION — Radarr / Sonarr / Plex / Seerr
# These proxy endpoints pull data from locally running ARR/Plex/Seerr services.
# Each reads its URL + API key from the settings table (set in the UI Settings
# tab).  If not configured they return {"configured": false} so the frontend
# can prompt the user rather than silently failing.
# =============================================================================

def _get_setting(key, default=None):
    """Read a single setting from SQLite; return default if missing."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default
    except Exception:
        return default

def _svc_get(base_url, path, api_key, api_key_header="X-Api-Key", timeout=5):
    """GET a JSON endpoint on a local service; raises on HTTP/network error."""
    url = base_url.rstrip('/') + path
    headers = {api_key_header: api_key} if api_key else {}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()

@app.route("/api/services/radarr/calendar")
def api_radarr_calendar():
    """Upcoming movies from Radarr (next 14 days)."""
    url = _get_setting("radarr_url")
    key = _get_setting("radarr_api_key")
    if not url:
        return jsonify({"configured": False})
    try:
        from datetime import date, timedelta
        start = date.today().isoformat()
        end   = (date.today() + timedelta(days=14)).isoformat()
        data = _svc_get(url, f"/api/v3/calendar?start={start}&end={end}&unmonitored=false", key)
        movies = [{"title": m.get("title"), "year": m.get("year"),
                   "date": m.get("physicalRelease") or m.get("digitalRelease") or m.get("inCinemas", "")[:10],
                   "poster": (next((i["remoteUrl"] for i in m.get("images",[]) if i.get("coverType")=="poster"), None)),
                   "hasFile": m.get("hasFile", False)} for m in data]
        return jsonify({"configured": True, "movies": movies[:10]})
    except Exception as e:
        return jsonify({"configured": True, "error": str(e), "movies": []})

@app.route("/api/services/radarr/queue")
def api_radarr_queue():
    """Active download queue from Radarr."""
    url = _get_setting("radarr_url")
    key = _get_setting("radarr_api_key")
    if not url:
        return jsonify({"configured": False})
    try:
        data = _svc_get(url, "/api/v3/queue?pageSize=20&includeUnknownMovieItems=false&includeMovie=true", key)
        records = data.get("records", []) if isinstance(data, dict) else []
        items = []
        for rec in records[:15]:
            movie = rec.get("movie", {})
            poster = next((i["remoteUrl"] for i in movie.get("images", []) if i.get("coverType") == "poster"), None)
            items.append({
                "title": rec.get("title") or movie.get("title", "Unknown"),
                "status": rec.get("status", ""),
                "progress": round(100 - (rec.get("sizeleft", 0) / max(rec.get("size", 1), 1)) * 100, 1),
                "size": rec.get("size", 0),
                "sizeleft": rec.get("sizeleft", 0),
                "timeleft": rec.get("timeleft", ""),
                "quality": rec.get("quality", {}).get("quality", {}).get("name", ""),
                "poster": poster,
                "indexer": rec.get("indexer", ""),
                "downloadClient": rec.get("downloadClient", ""),
            })
        return jsonify({"configured": True, "queue": items, "totalRecords": data.get("totalRecords", 0)})
    except Exception as e:
        return jsonify({"configured": True, "error": str(e), "queue": []})

@app.route("/api/services/radarr/library")
def api_radarr_library():
    """Radarr library stats."""
    url = _get_setting("radarr_url")
    key = _get_setting("radarr_api_key")
    if not url:
        return jsonify({"configured": False})
    try:
        movies = _svc_get(url, "/api/v3/movie", key)
        total = len(movies)
        monitored = sum(1 for m in movies if m.get("monitored"))
        downloaded = sum(1 for m in movies if m.get("hasFile"))
        missing = sum(1 for m in movies if m.get("monitored") and not m.get("hasFile"))
        return jsonify({
            "configured": True,
            "total": total, "monitored": monitored,
            "downloaded": downloaded, "missing": missing,
        })
    except Exception as e:
        return jsonify({"configured": True, "error": str(e)})

@app.route("/api/services/sonarr/calendar")
def api_sonarr_calendar():
    """Upcoming episodes from Sonarr (next 7 days)."""
    url = _get_setting("sonarr_url")
    key = _get_setting("sonarr_api_key")
    if not url:
        return jsonify({"configured": False})
    try:
        from datetime import date, timedelta
        start = date.today().isoformat()
        end   = (date.today() + timedelta(days=7)).isoformat()
        data = _svc_get(url, f"/api/v3/calendar?start={start}&end={end}&unmonitored=false&includeSeries=true", key)
        episodes = [{"series": e.get("series",{}).get("title",""),
                     "title": e.get("title",""),
                     "season": e.get("seasonNumber"),
                     "episode": e.get("episodeNumber"),
                     "airDate": e.get("airDateUtc","")[:10],
                     "hasFile": e.get("hasFile", False),
                     "poster": (next((i["remoteUrl"] for i in e.get("series",{}).get("images",[]) if i.get("coverType")=="poster"), None))} for e in data]
        return jsonify({"configured": True, "episodes": episodes[:10]})
    except Exception as e:
        return jsonify({"configured": True, "error": str(e), "episodes": []})

@app.route("/api/services/sonarr/queue")
def api_sonarr_queue():
    """Active download queue from Sonarr."""
    url = _get_setting("sonarr_url")
    key = _get_setting("sonarr_api_key")
    if not url:
        return jsonify({"configured": False})
    try:
        data = _svc_get(url, "/api/v3/queue?pageSize=20&includeUnknownSeriesItems=false&includeSeries=true&includeEpisode=true", key)
        records = data.get("records", []) if isinstance(data, dict) else []
        items = []
        for rec in records[:15]:
            series = rec.get("series", {})
            episode = rec.get("episode", {})
            poster = next((i["remoteUrl"] for i in series.get("images", []) if i.get("coverType") == "poster"), None)
            ep_label = f"S{episode.get('seasonNumber',0):02d}E{episode.get('episodeNumber',0):02d}" if episode else ""
            items.append({
                "title": series.get("title", "Unknown"),
                "episode": ep_label,
                "episodeTitle": episode.get("title", ""),
                "status": rec.get("status", ""),
                "progress": round(100 - (rec.get("sizeleft", 0) / max(rec.get("size", 1), 1)) * 100, 1),
                "size": rec.get("size", 0),
                "sizeleft": rec.get("sizeleft", 0),
                "timeleft": rec.get("timeleft", ""),
                "quality": rec.get("quality", {}).get("quality", {}).get("name", ""),
                "poster": poster,
                "indexer": rec.get("indexer", ""),
                "downloadClient": rec.get("downloadClient", ""),
            })
        return jsonify({"configured": True, "queue": items, "totalRecords": data.get("totalRecords", 0)})
    except Exception as e:
        return jsonify({"configured": True, "error": str(e), "queue": []})

@app.route("/api/services/sonarr/library")
def api_sonarr_library():
    """Sonarr library stats."""
    url = _get_setting("sonarr_url")
    key = _get_setting("sonarr_api_key")
    if not url:
        return jsonify({"configured": False})
    try:
        series_list = _svc_get(url, "/api/v3/series", key)
        total = len(series_list)
        monitored = sum(1 for s in series_list if s.get("monitored"))
        episodes_total = sum(s.get("statistics", {}).get("episodeCount", 0) for s in series_list)
        episodes_have = sum(s.get("statistics", {}).get("episodeFileCount", 0) for s in series_list)
        return jsonify({
            "configured": True,
            "totalSeries": total, "monitored": monitored,
            "episodes": episodes_total, "episodesOnDisk": episodes_have,
        })
    except Exception as e:
        return jsonify({"configured": True, "error": str(e)})

@app.route("/api/services/plex/sessions")
def api_plex_sessions():
    """Active Plex streams (Now Playing)."""
    url   = _get_setting("plex_url")
    token = _get_setting("plex_token")
    if not url:
        return jsonify({"configured": False})
    if not token:
        return jsonify({"configured": False})
    try:
        # Plex requires token as header — never call _svc_get with None for auth
        full_url = url.rstrip('/') + "/status/sessions"
        r = requests.get(full_url, headers={"X-Plex-Token": token or "", "Accept": "application/json"}, timeout=5)
        r.raise_for_status()
        payload = r.json()
        sessions = []
        for item in payload.get("MediaContainer", {}).get("Metadata", []):
            pct = 0
            if item.get("duration") and item.get("viewOffset"):
                pct = round(item["viewOffset"] / item["duration"] * 100, 1)
            sessions.append({
                "title": item.get("grandparentTitle") or item.get("title",""),
                "subtitle": item.get("title","") if item.get("grandparentTitle") else "",
                "user": item.get("User",{}).get("title",""),
                "player": item.get("Player",{}).get("product",""),
                "progress": pct,
                "state": item.get("Player",{}).get("state",""),
                "thumb": item.get("thumb","")
            })
        return jsonify({"configured": True, "sessions": sessions})
    except Exception as e:
        return jsonify({"configured": True, "error": str(e), "sessions": []})

@app.route("/api/services/seerr/requests")
def api_seerr_requests():
    """Recent Seerr/Overseerr requests (latest 8)."""
    url = _get_setting("seerr_url")
    key = _get_setting("seerr_api_key")
    if not url:
        return jsonify({"configured": False})
    try:
        data = _svc_get(url, "/api/v1/request?take=8&skip=0&sort=added", key)
        reqs = []
        for r in data.get("results", []):
            media = r.get("media", {})
            reqs.append({
                "id": r.get("id"),
                "type": media.get("mediaType",""),
                "status": r.get("status"),          # 1=pending 2=approved 3=declined 4=available
                "requestedBy": r.get("requestedBy",{}).get("displayName",""),
                "title": media.get("tmdbId",""),     # resolved to title in frontend via cache
                "poster": media.get("posterPath",""),
                "createdAt": r.get("createdAt","")[:10]
            })
        return jsonify({"configured": True, "requests": reqs})
    except Exception as e:
        return jsonify({"configured": True, "error": str(e), "requests": []})

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
    """Extract ports from container as simple strings."""
    ports = []
    try:
        port_info = container.ports or {}
        for key, val in port_info.items():
            if val:
                host_port = val[0].get("HostPort")
                container_port = key.split('/')[0] if key else ""
                ports.append(f"{host_port}:{container_port}")
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
<title>ArrHub</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:ital,opsz,wght@0,14..32,300;0,14..32,400;0,14..32,500;0,14..32,600;0,14..32,700;1,14..32,400&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/gridstack@10.3.1/dist/gridstack-all.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/gridstack@10.3.1/dist/gridstack.min.css">
<style>
/* =====================================================================
   ARRHUB — PegaProx-inspired dark dashboard
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
  --ctr-card-min: 280px;
  --r:        8px;
  --mono:     'JetBrains Mono',monospace;
  --ui:       'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
  --transition: .15s ease;
}
/* ── Themes ─────────────────────────────────────────────────────────── */
[data-theme="light"]{
  --bg:#f6f8fa;--bg2:#ffffff;--bg3:#f0f2f5;
  --surface:#e9ecef;--surface2:#dee2e6;
  --border:#d0d7de;--border2:#b8bfc7;
  --text:#1f2328;--text2:#656d76;--text3:#9198a1;
}
[data-theme="nord"]{
  --bg:#2e3440;--bg2:#3b4252;--bg3:#434c5e;
  --surface:#434c5e;--surface2:#4c566a;
  --border:#4c566a;--border2:#616e88;
  --text:#eceff4;--text2:#d8dee9;--text3:#81a1c1;
  --blue:#88c0d0;--blue2:rgba(136,192,208,.15);
  --green:#a3be8c;--green2:rgba(163,190,140,.15);
  --purple:#b48ead;--purple2:rgba(180,142,173,.15);
}
[data-theme="catppuccin"]{
  --bg:#1e1e2e;--bg2:#181825;--bg3:#313244;
  --surface:#313244;--surface2:#45475a;
  --border:#45475a;--border2:#585b70;
  --text:#cdd6f4;--text2:#a6adc8;--text3:#7f849c;
  --blue:#89b4fa;--blue2:rgba(137,180,250,.15);
  --green:#a6e3a1;--green2:rgba(166,227,161,.15);
  --purple:#cba6f7;--purple2:rgba(203,166,247,.15);
}
[data-theme="dracula"]{
  --bg:#282a36;--bg2:#1e1f29;--bg3:#343746;
  --surface:#343746;--surface2:#44475a;
  --border:#44475a;--border2:#6272a4;
  --text:#f8f8f2;--text2:#d0d0e0;--text3:#6272a4;
  --blue:#8be9fd;--blue2:rgba(139,233,253,.15);
  --green:#50fa7b;--green2:rgba(80,250,123,.15);
  --purple:#bd93f9;--purple2:rgba(189,147,249,.15);
}
/* ── Accent color overrides ─── */
[data-accent="purple"]{--blue:#bc8cff;--blue2:rgba(188,140,255,.15);}
[data-accent="green"]{--blue:#3fb950;--blue2:rgba(63,185,80,.15);}
[data-accent="orange"]{--blue:#f78166;--blue2:rgba(247,129,102,.15);}
[data-accent="pink"]{--blue:#f778ba;--blue2:rgba(247,120,186,.15);}
[data-accent="cyan"]{--blue:#56d9e0;--blue2:rgba(86,217,224,.15);}

/* ── Background image layer ─────────────────────────────────────────── */
#bg-layer{display:none;position:fixed;inset:0;z-index:0;background-size:cover;background-position:center;}
#app{position:relative;z-index:1;}

/* ── GridStack Dashboard Widgets ────────────────────────────────────── */
/* Hide widgets before GridStack positions them — prevents 600ms stacking flash */
.grid-stack:not(.gs-ready) > .grid-stack-item { visibility:hidden; }
.grid-stack{width:100%;}
.grid-stack-item-content{
  overflow:hidden;
  height:100%;
  border-radius:var(--r);
  position:relative;
}
.grid-stack-item-content .panel{
  margin:0;
  height:100%;
  border-radius:var(--r);
  overflow:hidden;
}
/* Drag handle shown when in edit mode */
.gs-editing .grid-stack-item>.grid-stack-item-content::before{
  content:'⠿  drag to rearrange · resize from corner';
  display:block;
  padding:4px 10px;
  font-size:10px;
  color:var(--text3);
  background:var(--surface);
  border-bottom:1px solid var(--border);
  border-radius:var(--r) var(--r) 0 0;
  cursor:grab;
  user-select:none;
  letter-spacing:.03em;
}
.gs-editing .grid-stack-item>.grid-stack-item-content .panel{
  border-radius:0 0 var(--r) var(--r);
}
.gs-editing .grid-stack-item{outline:1px dashed var(--border2);}
/* Widget remove button (shown in edit mode) */
.gs-editing .widget-remove-btn{display:flex!important;}
.widget-remove-btn{
  display:none;position:absolute;top:4px;right:4px;z-index:10;
  width:20px;height:20px;border-radius:50%;border:none;
  background:var(--red2);color:var(--red);cursor:pointer;
  font-size:12px;line-height:1;align-items:center;justify-content:center;
  transition:background .15s;
}
.widget-remove-btn:hover{background:var(--red)!important;color:#fff!important;}
/* ── Compact widget content — auto-shrink text, hide overflow, tighten padding ── */
.grid-stack-item-content .panel{container-type:inline-size;}
.grid-stack-item-content .panel-title{font-size:clamp(11px,1.6vw,13px);padding:8px 12px;}
.grid-stack-item .stat-grid{grid-template-columns:repeat(auto-fill,minmax(100px,1fr));gap:8px;margin-bottom:8px;}
.grid-stack-item .stat-card{padding:8px 10px;gap:4px;}
.grid-stack-item .stat-card-val{font-size:clamp(12px,2vw,15px);}
.grid-stack-item .stat-card-label{font-size:10px;}
/* Ultra-compact mode — applied via JS when widget is resized small */
.widget-compact .panel{padding:6px 8px;}
.widget-compact .panel-title{font-size:11px;padding:4px 8px;gap:4px;}
.widget-compact .stat-grid{grid-template-columns:repeat(2,1fr);gap:6px;margin-bottom:4px;}
.widget-compact .gauge-wrap canvas{width:60px!important;height:60px!important;}
.widget-compact .metric-card{padding:6px 8px;gap:2px;}
.widget-compact .metric-name{font-size:10px;}
.widget-compact .ctr-row{font-size:11px;}
.widget-compact #service-cards-row{grid-template-columns:1fr!important;gap:6px!important;}
/* ── Service card tabs ── */
.svc-card{display:flex;flex-direction:column;overflow:hidden;}
.svc-card-hdr{display:flex;align-items:center;justify-content:space-between;padding:8px 12px;border-bottom:1px solid var(--border);gap:6px;flex-shrink:0;}
.svc-card-title{font-size:13px;font-weight:600;color:var(--text);white-space:nowrap;}
.svc-tabs{display:flex;gap:2px;}
.svc-tab{background:none;border:none;color:var(--text3);font-size:10px;font-weight:600;padding:3px 8px;border-radius:6px;cursor:pointer;transition:background .15s,color .15s;}
.svc-tab:hover{background:var(--surface);color:var(--text2);}
.svc-tab.active{background:var(--blue2);color:var(--blue);}
.svc-card-body{display:flex;flex-direction:column;gap:4px;padding:6px 8px;overflow-y:auto;flex:1;min-height:0;}
/* Clickable service items */
.svc-item{display:flex;align-items:center;gap:8px;padding:4px 4px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .12s;border-radius:4px;}
.svc-item:hover{background:var(--surface);}
.svc-item:last-child{border-bottom:none;}
.svc-detail{display:none;padding:6px 10px;background:var(--bg3);border-radius:6px;margin:2px 0 4px;font-size:11px;color:var(--text2);line-height:1.5;border:1px solid var(--border);}
.svc-detail.open{display:block;}
/* Queue progress bar */
.svc-q-bar{height:3px;background:var(--surface2);border-radius:2px;overflow:hidden;margin-top:3px;}
.svc-q-fill{height:100%;border-radius:2px;transition:width .5s ease;}
/* Service library stats grid */
.svc-lib-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:6px 0;}
.svc-lib-stat{text-align:center;padding:10px 6px;background:var(--bg3);border-radius:6px;border:1px solid var(--border);}
.svc-lib-val{font-size:20px;font-weight:700;font-family:var(--mono);color:var(--text);}
.svc-lib-label{font-size:10px;color:var(--text3);margin-top:2px;text-transform:uppercase;letter-spacing:.04em;}
/* Service launcher tiles */
.launcher-tile{
  display:flex;flex-direction:column;align-items:center;gap:4px;
  padding:10px 14px;background:var(--surface);border:1px solid var(--border);
  border-radius:8px;text-decoration:none;color:var(--text);
  min-width:80px;transition:background .15s,border-color .15s;cursor:pointer;
}
.launcher-tile:hover{background:var(--surface2);border-color:var(--blue);}
.launcher-tile-icon{font-size:22px;line-height:1;}
.launcher-tile-name{font-size:11px;font-weight:600;color:var(--text);text-align:center;max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.launcher-tile-port{font-size:10px;color:var(--text3);font-family:var(--mono);}
/* Widget palette cards */
.widget-palette-card{
  display:flex;flex-direction:column;align-items:center;gap:6px;padding:14px 8px;
  background:var(--surface);border:2px solid var(--border);border-radius:8px;
  cursor:pointer;transition:border-color .15s,background .15s;text-align:center;
}
.widget-palette-card:hover{border-color:var(--blue);background:var(--surface2);}
.widget-palette-card.active{border-color:var(--green);background:var(--green2);}
.widget-palette-card.active:hover{border-color:var(--red);background:var(--red2);}
.widget-palette-card .wpc-icon{font-size:26px;line-height:1;}
.widget-palette-card .wpc-name{font-size:11px;font-weight:600;color:var(--text);}
.widget-palette-card .wpc-status{font-size:10px;color:var(--text3);margin-top:2px;}
/* Theme button active state */
.theme-btn.active{background:var(--blue2)!important;color:var(--blue)!important;border-color:var(--blue)!important;}
/* Accent swatch selected ring */
.accent-swatch.active{outline:3px solid var(--text);outline-offset:2px;}

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
  container-type:inline-size;overflow:hidden;min-height:0;
}
.stat-card:hover{border-color:var(--border2);}
.stat-card-icon{font-size:18px;margin-bottom:2px;}
.stat-card-val{font-size:22px;font-weight:700;color:var(--text);font-family:var(--mono);}
.stat-card-label{font-size:11px;color:var(--text2);}
/* Scale stat-card content as widget shrinks */
@container (max-width: 130px){
  .stat-card-val{font-size:16px;}
  .stat-card-label{font-size:9px;}
  .stat-card-icon{font-size:14px;}
  .stat-card{padding:8px 10px;gap:3px;}
}
@container (max-width: 90px){
  .stat-card-val{font-size:12px;}
  .stat-card-label{font-size:8px;}
  .stat-card{padding:5px 7px;gap:2px;}
}

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
.metric-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:20px;}
.metric-card{
  background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);
  padding:12px;display:flex;flex-direction:column;gap:6px;overflow:hidden;
}
/* When gauge widget is narrow, stack metric cards 2×2 */
.grid-stack-item[gs-id="gauges"] .metric-grid{margin-bottom:0;}
@container (max-width:600px){.metric-grid{grid-template-columns:repeat(2,1fr);}}
.metric-top{display:flex;align-items:center;justify-content:space-between;}
.metric-name{font-size:12px;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:.06em;}
.metric-badge{font-size:11px;padding:2px 7px;border-radius:10px;font-weight:600;}

/* ── Container grid ── */
.container-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--ctr-card-min,280px),1fr));gap:14px;}
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

/* ── Responsive — original ── */
@media(max-width:700px){
  .container-grid{grid-template-columns:1fr;}
  .settings-grid{grid-template-columns:1fr;}
  .stat-grid{grid-template-columns:repeat(2,1fr);}
}

/* ══════════════════════════════════════════════════════════════
   1. TOAST NOTIFICATION SYSTEM
   ══════════════════════════════════════════════════════════════ */
#toast-container{
  position:fixed;bottom:20px;right:20px;
  display:flex;flex-direction:column;gap:8px;
  z-index:9999;pointer-events:none;
}
.toast{
  display:flex;align-items:flex-start;gap:10px;
  background:var(--bg2);border:1px solid var(--border2);
  border-radius:var(--r);padding:12px 14px;
  min-width:260px;max-width:380px;
  box-shadow:0 8px 24px rgba(0,0,0,.5);
  pointer-events:all;
  animation:toastIn .25s ease forwards;
  border-left:4px solid var(--blue);
}
.toast.success{border-left-color:var(--green);}
.toast.error{border-left-color:var(--red);}
.toast.warn{border-left-color:var(--yellow);}
.toast.info{border-left-color:var(--blue);}
.toast.toast-exit{animation:toastOut .25s ease forwards;}
.toast-icon{font-size:16px;flex-shrink:0;margin-top:1px;}
.toast-body{flex:1;min-width:0;}
.toast-msg{font-size:13px;color:var(--text);line-height:1.4;word-break:break-word;}
.toast-close{
  background:none;border:none;color:var(--text3);cursor:pointer;
  padding:0 2px;font-size:16px;line-height:1;flex-shrink:0;
}
.toast-close:hover{color:var(--text);}
@keyframes toastIn{
  from{opacity:0;transform:translateX(40px);}
  to{opacity:1;transform:translateX(0);}
}
@keyframes toastOut{
  from{opacity:1;transform:translateX(0);}
  to{opacity:0;transform:translateX(40px);}
}

/* ══════════════════════════════════════════════════════════════
   2. SSE DISCONNECTED BANNER
   ══════════════════════════════════════════════════════════════ */
#sse-banner{
  position:fixed;top:0;left:0;right:0;
  background:#7a5c00;border-bottom:2px solid var(--yellow);
  color:#fff;font-size:13px;font-weight:600;
  padding:8px 20px;
  display:none;align-items:center;justify-content:center;gap:12px;
  z-index:8000;
}
#sse-banner.visible{display:flex;}
#sse-banner button{
  background:rgba(255,255,255,.2);border:1px solid rgba(255,255,255,.5);
  color:#fff;border-radius:6px;padding:3px 12px;font-size:12px;cursor:pointer;
}
#sse-banner button:hover{background:rgba(255,255,255,.35);}

/* Push content down when banner visible */
body.sse-disconnected #app{padding-top:38px;}

/* ══════════════════════════════════════════════════════════════
   3. MOBILE RESPONSIVE LAYOUT
   ══════════════════════════════════════════════════════════════ */

/* Hamburger button — hidden on desktop */
#hamburger{
  display:none;
  background:none;border:none;color:var(--text2);cursor:pointer;
  padding:4px 6px;border-radius:6px;
  align-items:center;justify-content:center;
}
#hamburger:hover{background:var(--surface2);color:var(--text);}
#hamburger svg{width:20px;height:20px;}

/* ── Desktop sidebar collapse toggle ── */
#sb-collapse-btn{
  display:flex;background:none;border:none;color:var(--text2);cursor:pointer;
  padding:4px 6px;border-radius:6px;align-items:center;justify-content:center;flex-shrink:0;
}
#sb-collapse-btn:hover{background:var(--surface2);color:var(--text);}
#sb-collapse-btn svg{width:18px;height:18px;}
@media(max-width:900px){#sb-collapse-btn{display:none;}}

/* Collapsed sidebar state */
#app.sb-collapsed #sidebar{width:60px;min-width:60px;}
#app.sb-collapsed .sb-title,#app.sb-collapsed .sb-version,
#app.sb-collapsed .sb-section-label,#app.sb-collapsed .sb-badge{display:none!important;}
#app.sb-collapsed .sb-item{justify-content:center;padding:8px 0;}
#app.sb-collapsed .sb-item>span:not(.sb-icon){display:none;}
#app.sb-collapsed .sb-brand{justify-content:center;padding:12px 0;}
#app.sb-collapsed .sb-logo{margin:0;}
#app.sb-collapsed #sidebar .sb-section{padding:6px 4px;}

/* Card size slider */
.ctr-size-slider{accent-color:var(--blue);width:80px;cursor:pointer;vertical-align:middle;}

/* Port map accordion */
.pm-group{border:1px solid var(--border);border-radius:var(--r);overflow:hidden;margin-bottom:8px;}
.pm-group-hdr{
  display:flex;align-items:center;gap:10px;padding:10px 14px;cursor:pointer;
  background:var(--bg2);transition:background var(--transition);user-select:none;
}
.pm-group-hdr:hover{background:var(--surface);}
.pm-group-body{border-top:1px solid var(--border);}
.pm-group-body table{width:100%;border-collapse:collapse;}
.pm-group-chevron{margin-left:auto;transition:transform .2s;font-size:10px;color:var(--text3);}
.pm-group.collapsed .pm-group-chevron{transform:rotate(-90deg);}
.pm-group.collapsed .pm-group-body{display:none;}

/* Stack manager cards */
.stack-card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);padding:14px;margin-bottom:10px;}
.stack-card-hdr{display:flex;align-items:center;gap:10px;margin-bottom:10px;}
.stack-card-name{font-weight:600;font-size:14px;flex:1;}
.stack-card-actions{display:flex;gap:6px;flex-wrap:wrap;}

/* Mobile bottom nav — hidden on desktop */
#bottom-nav{
  display:none;
  position:fixed;bottom:0;left:0;right:0;
  height:60px;
  background:var(--bg2);border-top:1px solid var(--border);
  z-index:700;
  justify-content:space-around;align-items:stretch;
}
.bn-item{
  flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:3px;cursor:pointer;color:var(--text3);font-size:10px;font-weight:500;
  border:none;background:none;padding:4px 2px;
  transition:color var(--transition);
}
.bn-item:hover,.bn-item.active{color:var(--blue);}
.bn-item svg{width:20px;height:20px;}

/* ── 900px breakpoint: sidebar → bottom nav ── */
@media(max-width:900px){
  #sidebar{
    position:fixed;top:0;left:0;bottom:60px;
    z-index:800;
    transform:translateX(-100%);
    transition:transform .25s ease;
    width:var(--sb-w);
    box-shadow:4px 0 20px rgba(0,0,0,.5);
  }
  #sidebar.sidebar-open{transform:translateX(0);}
  #hamburger{display:flex;}
  #bottom-nav{display:flex;}
  #main{margin-bottom:60px;}
  /* Overlay when sidebar open */
  #sidebar-overlay{
    display:none;
    position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:799;
  }
  #sidebar-overlay.visible{display:block;}
}

/* ── 600px breakpoint ── */
@media(max-width:600px){
  .stat-grid{grid-template-columns:repeat(2,1fr);}
  .container-grid{grid-template-columns:1fr;}
  .cat-grid{grid-template-columns:1fr;}
  .settings-grid{grid-template-columns:1fr;}
  #topbar .tb-stat:nth-child(4){display:none;} /* hide Load stat */
  .topbar-hostname{display:none;}
  /* Port map: full width on small screens */
  .pm-group{margin-bottom:6px;}
  /* Stack manager: single column */
  #stack-list{grid-template-columns:1fr!important;}
  /* RSS view tabs: wrap on very small screens */
  #tab-rss .section-header{flex-wrap:wrap;gap:8px;}
  /* Containers section-header: wrap */
  #tab-containers .section-header{flex-direction:column;align-items:flex-start;gap:8px;}
  /* Disk list: 1 column on tiny screens */
  #disk-list{grid-template-columns:1fr!important;}
  /* Network chart canvases: respect container width */
  #net-tx-canvas,#net-rx-canvas{max-width:100%;}
}

/* Touch-friendly minimum heights */
@media(max-width:900px){
  .btn,.filter-pill,.tab-btn{min-height:40px;}
  .bn-item{min-height:40px;}
  /* Live feeds / IPTV: don't use full-page iframes on mobile */
  #rss-live-view iframe{height:220px;}
  /* GridStack: disable drag on mobile (too difficult on touch) */
  .grid-stack.gs-editing .grid-stack-item > .ui-resizable-handle{touch-action:none;}
  /* HLS video players: full width */
  #rss-iptv-view video{height:180px!important;}
}

/* ══════════════════════════════════════════════════════════════
   4. LOADING SKELETON SHIMMER
   ══════════════════════════════════════════════════════════════ */
@keyframes shimmer{
  0%{background-position:-400px 0;}
  100%{background-position:400px 0;}
}
/* ── RSS collapsible columns ── */
.rss-col.collapsed .rss-items{display:none;}
.rss-col.collapsed .rss-feed-tabs{margin-bottom:0;}
.rss-col-hdr{cursor:pointer;user-select:none;display:flex;align-items:center;justify-content:space-between;}
.rss-col-hdr:hover .rss-col-chevron{color:var(--blue);}
.rss-col-chevron{font-size:11px;color:var(--text3);display:flex;align-items:center;gap:4px;transition:color var(--transition);}
.rss-col-count{background:var(--surface2);border-radius:10px;padding:1px 7px;font-size:10px;font-weight:600;}
/* Expand/collapse all controls */
#rss-all-controls{display:none;}
#rss-all-controls.visible{display:flex;}

.skeleton{
  background:linear-gradient(90deg,var(--surface) 25%,var(--surface2) 50%,var(--surface) 75%);
  background-size:800px 100%;
  animation:shimmer 1.4s infinite linear;
  border-radius:var(--r);
}
.skeleton-card{
  background:var(--bg2);border:1px solid var(--border);
  border-radius:var(--r);padding:14px;
  display:flex;flex-direction:column;gap:10px;
}
.sk-line{height:12px;border-radius:4px;}
.sk-line.short{width:50%;}
.sk-line.med{width:70%;}
.sk-line.full{width:100%;}
.sk-circle{width:48px;height:48px;border-radius:50%;flex-shrink:0;}
.sk-rect{height:80px;border-radius:var(--r);}

/* ══════════════════════════════════════════════════════════════
   5. DEPLOY TAB IMPROVEMENTS
   ══════════════════════════════════════════════════════════════ */
#deploy-controls{
  display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px;
}
#cat-sort{
  background:var(--surface);border:1px solid var(--border);
  color:var(--text);border-radius:var(--r);padding:6px 10px;
  font-family:var(--ui);font-size:13px;outline:none;cursor:pointer;
}
#cat-sort:focus{border-color:var(--blue);}
#cat-count{font-size:12px;color:var(--text3);margin-left:auto;}
.fav-section{
  margin-bottom:16px;padding:12px;
  background:var(--bg2);border:1px solid var(--yellow);
  border-radius:var(--r);
}
.fav-section-title{
  font-size:12px;font-weight:600;color:var(--yellow);
  margin-bottom:10px;display:flex;align-items:center;gap:6px;
}

/* ══════════════════════════════════════════════════════════════
   6. CONTAINER MEMORY PROGRESS BARS
   ══════════════════════════════════════════════════════════════ */
.mem-bar-wrap{
  margin:4px 0 0;
  height:4px;background:var(--surface2);
  border-radius:2px;overflow:hidden;width:100%;
}
.mem-bar{
  height:100%;border-radius:2px;
  transition:width .6s ease,background .4s;
}

/* ══════════════════════════════════════════════════════════════
   7. UPDATE BUTTON
   ══════════════════════════════════════════════════════════════ */
.btn.purple{border-color:rgba(188,140,255,.4);color:var(--purple);}
.btn.purple:hover{background:var(--purple2);}

/* ══════════════════════════════════════════════════════════════
   8. FAVORITES — star button on catalog cards
   ══════════════════════════════════════════════════════════════ */
.fav-btn{
  background:none;border:none;cursor:pointer;
  font-size:16px;color:var(--text3);padding:2px 4px;
  transition:color var(--transition),transform .15s;
  line-height:1;
}
.fav-btn:hover{transform:scale(1.2);}
.fav-btn.starred{color:var(--yellow);}

/* ══════════════════════════════════════════════════════════════
   9. SORTABLE TABLE VIEW FOR CONTAINERS
   ══════════════════════════════════════════════════════════════ */
#ctr-table-wrap{display:none;overflow-x:auto;}
#ctr-table-wrap table{min-width:900px;}
#ctr-table-wrap thead th{cursor:pointer;user-select:none;white-space:nowrap;}
#ctr-table-wrap thead th:hover{color:var(--blue);}
#ctr-table-wrap thead th .sort-arrow{margin-left:4px;opacity:.5;}
#ctr-table-wrap thead th.sorted .sort-arrow{opacity:1;color:var(--blue);}
.view-toggle{display:flex;gap:6px;align-items:center;}
.view-btn{
  display:inline-flex;align-items:center;gap:5px;
  padding:4px 10px;border-radius:6px;border:1px solid var(--border);
  background:var(--surface);color:var(--text2);font-size:12px;cursor:pointer;
  transition:all var(--transition);
}
.view-btn.active{background:var(--blue2);border-color:rgba(56,139,253,.5);color:var(--blue);}

/* ══════════════════════════════════════════════════════════════
   10. ALERTS BAR
   ══════════════════════════════════════════════════════════════ */
#alerts-bar{
  border-radius:var(--r);margin-bottom:16px;
  border:1px solid var(--border);
  overflow:hidden;
}
#alerts-bar-header{
  display:flex;align-items:center;justify-content:space-between;
  padding:10px 14px;background:var(--bg2);cursor:pointer;
  font-size:13px;font-weight:600;
}
#alerts-bar-header:hover{background:var(--surface);}
#alerts-body{padding:0 14px 10px;background:var(--bg2);}
#alerts-body.collapsed{display:none;}
.alert-row{
  display:flex;align-items:center;gap:8px;
  padding:5px 0;font-size:12px;color:var(--text);
  border-bottom:1px solid var(--border);
}
.alert-row:last-child{border-bottom:none;}
</style>
</head>
<body>
<!-- ══ TOAST CONTAINER ══ -->
<div id="toast-container"></div>

<!-- ══ SSE DISCONNECTED BANNER ══ -->
<div id="sse-banner">
  ⚠ <span id="sse-banner-msg">Live metrics disconnected — Reconnecting…</span>
  <button onclick="retrySSE()">Retry Now</button>
</div>

<!-- ══ SIDEBAR OVERLAY (mobile) ══ -->
<div id="sidebar-overlay" onclick="closeSidebar()"></div>

<!-- Background image layer (below everything, z-index:0) -->
<div id="bg-layer"></div>

<div id="app">

<!-- ═══════════════════════════════════════════════════════════
     SIDEBAR
═══════════════════════════════════════════════════════════ -->
<nav id="sidebar">
  <div class="sb-brand">
    <div class="sb-logo">A</div>
    <div>
      <div class="sb-title">ArrHub</div>
      <div class="sb-version">v3.10.0</div>
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
    <div class="sb-item" onclick="showTab('stornet',this)">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 10h18M3 14h18m-9-4v8m-7 0h14a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
      Storage &amp; Network
    </div>
    <div class="sb-item" onclick="showTab('ports',this)">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
      Port Map
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
    <div class="sb-section-label">Feeds</div>
    <div class="sb-item" onclick="showTab('rss',this)">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 5c7.18 0 13 5.82 13 13M6 11a7 7 0 017 7m-6 0a1 1 0 11-2 0 1 1 0 012 0z"/></svg>
      RSS Feeds
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
    <!-- Hamburger for mobile -->
    <button id="hamburger" onclick="toggleSidebar()" aria-label="Menu">
      <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16"/></svg>
    </button>
    <!-- Sidebar collapse for desktop -->
    <button id="sb-collapse-btn" onclick="toggleSidebarDesktop()" aria-label="Collapse sidebar" title="Collapse sidebar">
      <svg id="sb-collapse-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 19l-7-7 7-7m8 14l-7-7 7-7"/></svg>
    </button>
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
      <!-- ALERTS BAR -->
      <div id="alerts-bar">
        <div id="alerts-bar-header" onclick="toggleAlerts()">
          <span id="alerts-title">🔔 Alerts</span>
          <span id="alerts-chevron" style="color:var(--text3);font-size:11px">▼</span>
        </div>
        <div id="alerts-body">
          <div class="alert-row"><span>🟢</span><span>Loading alerts...</span></div>
        </div>
      </div>
      <div class="section-header">
        <div>
          <div class="section-title">System Overview</div>
          <div class="section-sub" id="ov-hostname">Loading...</div>
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <button id="ov-add-btn" class="btn" onclick="showWidgetPalette()" style="display:none;font-size:11px;padding:4px 12px;gap:4px">
            <svg width="11" height="11" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/></svg>
            Add Widget
          </button>
          <button id="ov-reset-btn" class="btn" onclick="resetGridLayout()" style="font-size:11px;padding:4px 12px;gap:4px;display:none">
            <svg width="11" height="11" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
            Reset
          </button>
          <button id="ov-edit-btn" class="btn" onclick="toggleGridEdit()" style="font-size:11px;padding:4px 12px;gap:4px">
            <svg width="11" height="11" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg>
            Edit Layout
          </button>
        </div>
      </div>

      <!-- ── Overview GridStack Dashboard ─────────────────────────────────
           Click "Edit Layout" in the header to enter drag-and-drop mode.
           Positions are saved to localStorage (key: arrhub_grid).        -->
      <div class="grid-stack" id="ov-grid">

        <!-- ⓪ System Gauges widget  (default: full width, row 0) -->
        <div class="grid-stack-item" gs-id="gauges" gs-x="0" gs-y="0" gs-w="12" gs-h="3" gs-min-w="4" gs-min-h="2">
          <div class="grid-stack-item-content">
            <button class="widget-remove-btn" onclick="removeWidget('gauges')" title="Remove widget">✕</button>
            <div class="metric-grid" id="gauge-row" style="margin:0;padding:8px;height:100%;box-sizing:border-box;align-content:start">
              <div class="metric-card">
                <div class="metric-top">
                  <span class="metric-name">CPU Usage</span>
                  <span class="metric-badge" id="cpu-badge" style="background:var(--green2);color:var(--green)">0%</span>
                </div>
                <div class="gauge-wrap" style="position:relative;display:flex;flex-direction:column;align-items:center;gap:4px">
                  <div style="position:relative;width:100px;height:100px">
                    <canvas id="cpu-gauge-canvas" width="100" height="100"></canvas>
                    <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;flex-direction:column">
                      <span id="cpu-gauge-text" style="font-size:18px;font-weight:700;font-family:var(--mono);color:var(--text)">0%</span>
                    </div>
                  </div>
                  <span class="gauge-sub" id="cpu-cores">— cores</span>
                </div>
                <div class="pbar-wrap"><div class="pbar blue" id="cpu-pbar" style="width:0%"></div></div>
              </div>

              <div class="metric-card">
                <div class="metric-top">
                  <span class="metric-name">Memory</span>
                  <span class="metric-badge" id="mem-badge" style="background:var(--green2);color:var(--green)">0%</span>
                </div>
                <div class="gauge-wrap" style="position:relative;display:flex;flex-direction:column;align-items:center;gap:4px">
                  <div style="position:relative;width:100px;height:100px">
                    <canvas id="mem-gauge-canvas" width="100" height="100"></canvas>
                    <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;flex-direction:column">
                      <span id="mem-gauge-text" style="font-size:18px;font-weight:700;font-family:var(--mono);color:var(--text)">0%</span>
                    </div>
                  </div>
                  <span class="gauge-sub" id="mem-detail">— / — GB</span>
                </div>
                <div class="pbar-wrap"><div class="pbar" id="mem-pbar" style="width:0%;background:var(--purple)"></div></div>
              </div>

              <div class="metric-card">
                <div class="metric-top"><span class="metric-name">Load Average</span></div>
                <div style="display:flex;flex-direction:column;gap:6px;padding:4px 0;">
                  <div class="ctr-row"><span>1 min</span><span id="load-1m">—</span></div>
                  <div class="ctr-row"><span>5 min</span><span id="load-5m">—</span></div>
                  <div class="ctr-row"><span>15 min</span><span id="load-15m">—</span></div>
                </div>
                <div class="ctr-row" style="margin-top:2px;"><span>Uptime</span><span id="uptime-val">—</span></div>
              </div>

              <div class="metric-card">
                <div class="metric-top"><span class="metric-name">Containers</span></div>
                <div style="display:flex;flex-direction:column;gap:6px;padding:4px 0;">
                  <div class="ctr-row"><span>Running</span><span id="ctr-running-count" style="color:var(--green)">—</span></div>
                  <div class="ctr-row"><span>Stopped</span><span id="ctr-stopped-count" style="color:var(--red)">—</span></div>
                  <div class="ctr-row"><span>Total</span><span id="ctr-total-count">—</span></div>
                </div>
                <button class="btn blue" style="margin-top:4px;width:100%;justify-content:center;font-size:11px;padding:3px 8px" onclick="showTab('containers',null)">
                  View Containers
                </button>
              </div>
            </div>
          </div>
        </div>

        <!-- ① System Info widget  (default: left half, row 3) -->
        <div class="grid-stack-item" gs-id="sysinfo" gs-x="0" gs-y="3" gs-w="6" gs-h="4" gs-min-w="3" gs-min-h="2">
          <div class="grid-stack-item-content">
            <button class="widget-remove-btn" onclick="removeWidget('sysinfo')" title="Remove widget">✕</button>
            <div class="panel" style="margin:0;height:100%;overflow:hidden">
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
        </div>

        <!-- ② Weather widget  (default: right half, row 3) -->
        <div class="grid-stack-item" gs-id="weather" gs-x="6" gs-y="3" gs-w="6" gs-h="4" gs-min-w="3" gs-min-h="2">
          <div class="grid-stack-item-content">
            <button class="widget-remove-btn" onclick="removeWidget('weather')" title="Remove widget">✕</button>
            <div class="panel" id="weather-panel" style="margin:0;height:100%;overflow:hidden">
              <div class="panel-title">
                <svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 15a4 4 0 004 4h9a5 5 0 10-.1-9.999 5.002 5.002 0 10-9.78 2.051A4.002 4.002 0 003 15z"/></svg>
                Weather
                <span id="weather-location" style="margin-left:8px;font-weight:400;font-size:11px;color:var(--text3)"></span>
              </div>
              <div id="weather-widget" style="display:grid;grid-template-columns:auto 1fr 1fr 1fr;gap:10px;align-items:center">
                <div style="display:flex;align-items:center;gap:10px;padding:4px 12px 4px 0;border-right:1px solid var(--border)">
                  <div id="weather-icon" style="font-size:36px;line-height:1">🌤️</div>
                  <div>
                    <div id="weather-temp" style="font-size:26px;font-weight:700;font-family:var(--mono);color:var(--text)">—</div>
                    <div id="weather-desc" style="font-size:11px;color:var(--text3);margin-top:2px">Loading…</div>
                  </div>
                </div>
                <div class="stat-card"><div class="stat-card-val" id="weather-humidity">—</div><div class="stat-card-label">Humidity</div></div>
                <div class="stat-card"><div class="stat-card-val" id="weather-wind">—</div><div class="stat-card-label">Wind</div></div>
                <div class="stat-card"><div class="stat-card-val" id="weather-feels">—</div><div class="stat-card-label">Feels Like</div></div>
              </div>
              <div id="weather-forecast" style="display:flex;gap:6px;margin-top:10px;overflow-x:auto;padding-bottom:4px"></div>
            </div>
          </div>
        </div>

        <!-- ③ Service Cards row  (default: full width, row 7) -->
        <div class="grid-stack-item" gs-id="services" gs-x="0" gs-y="7" gs-w="12" gs-h="5" gs-min-w="4" gs-min-h="3">
          <div class="grid-stack-item-content" style="overflow:hidden">
            <button class="widget-remove-btn" onclick="removeWidget('services')" title="Remove widget">✕</button>
            <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;padding:8px;height:100%;box-sizing:border-box;overflow:auto" id="service-cards-row">
              <div class="panel svc-card" style="margin:0" id="radarr-card">
                <div class="svc-card-hdr">
                  <span class="svc-card-title">🎥 Radarr</span>
                  <div class="svc-tabs">
                    <button class="svc-tab active" onclick="svcTabSwitch('radarr','upcoming',this)">Upcoming</button>
                    <button class="svc-tab" onclick="svcTabSwitch('radarr','queue',this)">Queue</button>
                    <button class="svc-tab" onclick="svcTabSwitch('radarr','library',this)">Library</button>
                  </div>
                </div>
                <div id="radarr-card-body" class="svc-card-body"><div style="color:var(--text3);font-size:12px;text-align:center;padding:12px">Loading…</div></div>
              </div>
              <div class="panel svc-card" style="margin:0" id="sonarr-card">
                <div class="svc-card-hdr">
                  <span class="svc-card-title">📺 Sonarr</span>
                  <div class="svc-tabs">
                    <button class="svc-tab active" onclick="svcTabSwitch('sonarr','upcoming',this)">Upcoming</button>
                    <button class="svc-tab" onclick="svcTabSwitch('sonarr','queue',this)">Queue</button>
                    <button class="svc-tab" onclick="svcTabSwitch('sonarr','library',this)">Library</button>
                  </div>
                </div>
                <div id="sonarr-card-body" class="svc-card-body"><div style="color:var(--text3);font-size:12px;text-align:center;padding:12px">Loading…</div></div>
              </div>
              <div class="panel svc-card" style="margin:0" id="plex-card">
                <div class="svc-card-hdr">
                  <span class="svc-card-title">▶ Plex — Now Playing</span>
                  <span id="plex-stream-count" style="background:var(--blue2);color:var(--blue);border-radius:10px;padding:1px 8px;font-size:11px;font-weight:600"></span>
                </div>
                <div id="plex-card-body" class="svc-card-body"><div style="color:var(--text3);font-size:12px;text-align:center;padding:12px">Loading…</div></div>
              </div>
              <div class="panel svc-card" style="margin:0" id="seerr-card">
                <div class="svc-card-hdr">
                  <span class="svc-card-title">🎬 Seerr — Requests</span>
                  <span style="font-size:11px;font-weight:400;color:var(--text3)">Recent</span>
                </div>
                <div id="seerr-card-body" class="svc-card-body"><div style="color:var(--text3);font-size:12px;text-align:center;padding:12px">Loading…</div></div>
              </div>
            </div>
          </div>
        </div>

        <!-- ④+⑤ Docker & Network I/O — merged into one full-width row (default: row 12) -->
        <div class="grid-stack-item" gs-id="infra" gs-x="0" gs-y="12" gs-w="12" gs-h="3" gs-min-w="4" gs-min-h="2">
          <div class="grid-stack-item-content">
            <button class="widget-remove-btn" onclick="removeWidget('infra')" title="Remove widget">✕</button>
            <div class="panel" style="margin:0;height:100%;overflow:hidden">
              <div class="panel-title">
                <svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/></svg>
                Docker &amp; Network
              </div>
              <div style="display:grid;grid-template-columns:repeat(6,1fr);gap:10px">
                <!-- Docker stats -->
                <div class="stat-card"><div class="stat-card-val" id="docker-images">—</div><div class="stat-card-label">Images</div></div>
                <div class="stat-card"><div class="stat-card-val" id="docker-volumes">—</div><div class="stat-card-label">Volumes</div></div>
                <div class="stat-card"><div class="stat-card-val" id="docker-networks">—</div><div class="stat-card-label">Networks</div></div>
                <div class="stat-card"><div class="stat-card-val" id="docker-disk">—</div><div class="stat-card-label">Docker Disk</div></div>
                <!-- Network I/O stats -->
                <div class="stat-card"><div class="stat-card-val" id="ov-net-sent">—</div><div class="stat-card-label">Net ↑ Sent</div></div>
                <div class="stat-card"><div class="stat-card-val" id="ov-net-recv">—</div><div class="stat-card-label">Net ↓ Recv</div></div>
              </div>
            </div>
          </div>
        </div>

        <!-- ⑥ Recent Logs  (default: left 4 cols, row 15) -->
        <div class="grid-stack-item" gs-id="logs" gs-x="0" gs-y="15" gs-w="4" gs-h="4" gs-min-w="2" gs-min-h="2">
          <div class="grid-stack-item-content">
            <button class="widget-remove-btn" onclick="removeWidget('logs')" title="Remove widget">✕</button>
            <div class="panel" style="margin:0;height:100%;overflow:hidden">
              <div class="panel-title" style="display:flex;align-items:center;justify-content:space-between">
                <span>
                  <svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
                  Recent Logs
                </span>
                <button class="btn blue" style="padding:3px 10px;font-size:11px" onclick="showTab('logs',null)">View All</button>
              </div>
              <pre id="ov-log-excerpt" style="font-family:var(--mono);font-size:11px;color:var(--text2);background:var(--bg3);border-radius:6px;padding:10px;max-height:200px;overflow:auto;white-space:pre-wrap;word-break:break-all">(loading...)</pre>
            </div>
          </div>
        </div>

        <!-- ⑦ Containers Live  (default: right 8 cols, row 15) -->
        <div class="grid-stack-item" gs-id="ctrs" gs-x="4" gs-y="15" gs-w="8" gs-h="4" gs-min-w="3" gs-min-h="2">
          <div class="grid-stack-item-content">
            <button class="widget-remove-btn" onclick="removeWidget('ctrs')" title="Remove widget">✕</button>
            <div class="panel" style="margin:0;height:100%;overflow:hidden">
              <div class="panel-title" style="display:flex;align-items:center;justify-content:space-between">
                <span style="display:flex;align-items:center;gap:6px">
                  <svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/></svg>
                  Containers
                  <span id="ov-ctr-badge" style="background:var(--blue2);color:var(--blue);border-radius:10px;padding:1px 8px;font-size:11px;font-weight:600">—</span>
                </span>
                <button class="btn blue" style="padding:3px 10px;font-size:11px" onclick="showTab('containers',null)">View All</button>
              </div>
              <div id="ov-ctr-list" style="display:flex;flex-direction:column;gap:6px;margin-top:4px">
                <div style="color:var(--text3);font-size:12px;text-align:center;padding:8px">Loading...</div>
              </div>
            </div>
          </div>
        </div>

        <!-- ⑧ Service Launcher  (default: full width, row 19) -->
        <div class="grid-stack-item" gs-id="launcher" gs-x="0" gs-y="19" gs-w="12" gs-h="3" gs-min-w="3" gs-min-h="2">
          <div class="grid-stack-item-content">
            <button class="widget-remove-btn" onclick="removeWidget('launcher')" title="Remove widget">✕</button>
            <div class="panel" style="margin:0;height:100%;overflow:auto">
              <div class="panel-title">
                🚀 Service Launcher
                <span style="margin-left:auto;font-size:11px;font-weight:400;color:var(--text3)">Click any tile to open</span>
              </div>
              <div id="launcher-tiles" style="display:flex;flex-wrap:wrap;gap:8px;padding:6px 0"></div>
            </div>
          </div>
        </div>

      </div><!-- /ov-grid -->

      <!-- Widget Palette Modal -->
      <div id="widget-palette-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;align-items:center;justify-content:center">
        <div style="background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:20px;width:520px;max-width:95vw;max-height:80vh;overflow:auto">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
            <div style="font-size:14px;font-weight:600;color:var(--text)">Add / Remove Widgets</div>
            <button class="btn" onclick="document.getElementById('widget-palette-modal').style.display='none'" style="padding:4px 12px">✕ Close</button>
          </div>
          <div id="widget-palette-body" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px"></div>
        </div>
      </div>
    </div>

    <!-- ── CONTAINERS ── -->
    <div id="tab-containers" class="tab-panel">
      <div class="section-header">
        <div class="section-title">Containers</div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <div class="view-toggle">
            <button class="view-btn active" id="btn-grid-view" onclick="setCtrView('grid')">
              <svg width="12" height="12" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 5a1 1 0 011-1h4a1 1 0 011 1v5a1 1 0 01-1 1H5a1 1 0 01-1-1V5zm10 0a1 1 0 011-1h4a1 1 0 011 1v2a1 1 0 01-1 1h-4a1 1 0 01-1-1V5zM4 15a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1H5a1 1 0 01-1-1v-4zm10-3a1 1 0 011-1h4a1 1 0 011 1v7a1 1 0 01-1 1h-4a1 1 0 01-1-1v-7z"/></svg>
              Grid
            </button>
            <button class="view-btn" id="btn-table-view" onclick="setCtrView('table')">
              <svg width="12" height="12" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 10h18M3 6h18M3 14h18M3 18h18"/></svg>
              Table
            </button>
          </div>
          <label style="display:flex;align-items:center;gap:5px;font-size:11px;color:var(--text3)">
            <svg width="12" height="12" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5v-4m0 4h-4m4 0l-5-5"/></svg>
            Size
            <input type="range" class="ctr-size-slider" id="ctr-size-range" min="200" max="500" value="280" oninput="setCtrCardSize(this.value)">
          </label>
          <button class="btn-primary" onclick="loadContainers()">
            <svg width="12" height="12" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
            Refresh
          </button>
        </div>
      </div>
      <div class="filter-row">
        <div class="filter-pill active" onclick="filterContainers('all',this)">All</div>
        <div class="filter-pill" onclick="filterContainers('running',this)">Running</div>
        <div class="filter-pill" onclick="filterContainers('exited',this)">Stopped</div>
        <div class="filter-pill" onclick="filterContainers('restarting',this)">Restarting</div>
      </div>
      <!-- Grid view -->
      <div class="container-grid" id="ctr-grid">
        <div class="empty"><div class="empty-icon">📦</div><div class="empty-text">Loading containers...</div></div>
      </div>
      <!-- Table view -->
      <div id="ctr-table-wrap">
        <table>
          <thead>
            <tr>
              <th onclick="sortCtrTable('name')">Name <span class="sort-arrow">↕</span></th>
              <th onclick="sortCtrTable('image')">Image <span class="sort-arrow">↕</span></th>
              <th onclick="sortCtrTable('status')">Status <span class="sort-arrow">↕</span></th>
              <th onclick="sortCtrTable('cpu')">CPU% <span class="sort-arrow">↕</span></th>
              <th onclick="sortCtrTable('mem')">MEM <span class="sort-arrow">↕</span></th>
              <th>Ports</th>
              <th onclick="sortCtrTable('uptime')">Uptime <span class="sort-arrow">↕</span></th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="ctr-table-body">
            <tr><td colspan="8" style="text-align:center;color:var(--text3);padding:20px">Switch to Table view to load</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- ── STORAGE ── -->
    <div id="tab-stornet" class="tab-panel">
      <div class="section-header">
        <div>
          <div class="section-title">💾 Storage &amp; 📡 Network</div>
          <div class="section-sub">Disk usage and live bandwidth in one view</div>
        </div>
        <button class="btn-primary" onclick="loadStorage();loadNetwork()">↺ Refresh</button>
      </div>
      <!-- ── Storage ── -->
      <div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Disk Usage</div>
      <div id="disk-list" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;margin-bottom:18px"></div>
      <!-- ── Network ── -->
      <div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Network</div>
      <div class="panel" style="margin-bottom:14px;padding:10px 14px">
        <div class="panel-title" style="margin-bottom:8px">📈 Live Bandwidth</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
          <div>
            <div style="font-size:10px;color:var(--text3);margin-bottom:3px">↑ TX (Upload)</div>
            <canvas id="net-tx-chart" height="55"></canvas>
          </div>
          <div>
            <div style="font-size:10px;color:var(--text3);margin-bottom:3px">↓ RX (Download)</div>
            <canvas id="net-rx-chart" height="55"></canvas>
          </div>
        </div>
      </div>
      <div class="panel">
        <div class="panel-title">Interfaces</div>
        <table><thead><tr><th>Interface</th><th>IP</th><th>Sent</th><th>Recv</th><th>Rate ↑/↓</th><th>Status</th></tr></thead>
        <tbody id="net-table"></tbody></table>
      </div>
    </div>

    <!-- ── PORT MAP ─────────────────────────────── -->
    <div id="tab-ports" class="tab-panel">
      <div class="section-header">
        <div class="section-title">Port Assignments</div>
        <div style="display:flex;gap:8px;align-items:center">
          <button class="btn" onclick="pmExpandAll()">⊞ All</button>
          <button class="btn" onclick="pmCollapseAll()">⊟ All</button>
          <button class="btn-primary" onclick="loadPortMap()">↺ Refresh</button>
        </div>
      </div>
      <div id="port-summary" style="display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap"></div>
      <div id="port-accordion"><div class="empty"><div class="empty-icon">🔌</div><div class="empty-text">Click Refresh to load port assignments</div></div></div>
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
        <div style="display:flex;gap:8px">
          <button class="btn" onclick="toggleLogsAutoRefresh(this)">Auto-refresh: Off</button>
          <button class="btn-primary" onclick="loadLogs()">Refresh</button>
        </div>
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
      <!-- Favorites row (hidden when empty) -->
      <div id="fav-section" class="fav-section" style="display:none">
        <div class="fav-section-title">⭐ Favorites</div>
        <div class="cat-grid" id="fav-grid" style="gap:10px"></div>
      </div>
      <!-- Controls: category pills + sort + count -->
      <div id="cat-categories" class="filter-row" style="margin-bottom:8px"></div>
      <div id="deploy-controls">
        <select id="cat-sort" onchange="renderCatalog()">
          <option value="az">Name A–Z</option>
          <option value="za">Name Z–A</option>
          <option value="cat">Category</option>
        </select>
        <span id="cat-count" style="font-size:12px;color:var(--text3)"></span>
      </div>
      <div class="cat-grid" id="cat-grid">
        <div class="empty"><div class="empty-icon">🔍</div><div class="empty-text">Loading catalog...</div></div>
      </div>
    </div>

    <!-- ── STACK ── -->
    <div id="tab-stack" class="tab-panel">
      <div class="section-header">
        <div class="section-title">Stack Manager</div>
        <button class="btn-primary" onclick="loadStackManager()">↺ Refresh</button>
      </div>
      <div id="stacks-list"><div class="empty"><div class="empty-icon">📋</div><div class="empty-text">Loading stacks...</div></div></div>
      <div class="panel" style="margin-top:16px">
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
      <!-- Service Integrations — API keys for Overview cards -->
      <div class="panel">
        <div class="panel-title">
          <svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
          Service Integrations
        </div>
        <div style="font-size:12px;color:var(--text3);margin-bottom:10px">Fill in API details to enable Overview cards for Radarr, Sonarr, Plex, and Seerr.</div>
        <div class="settings-grid">
          <div class="field"><label>Radarr URL</label><input type="text" id="svc-radarr-url" placeholder="http://localhost:7878"><div class="field-hint">e.g. http://192.168.1.x:7878</div></div>
          <div class="field"><label>Radarr API Key</label><input type="password" id="svc-radarr-key" placeholder="•••••••••••"></div>
          <div class="field"><label>Sonarr URL</label><input type="text" id="svc-sonarr-url" placeholder="http://localhost:8989"></div>
          <div class="field"><label>Sonarr API Key</label><input type="password" id="svc-sonarr-key" placeholder="•••••••••••"></div>
          <div class="field"><label>Plex URL</label><input type="text" id="svc-plex-url" placeholder="http://localhost:32400"></div>
          <div class="field"><label>Plex Token</label><input type="password" id="svc-plex-token" placeholder="•••••••••••"><div class="field-hint">Settings → Troubleshooting → X-Plex-Token</div></div>
          <div class="field"><label>Seerr/Overseerr URL</label><input type="text" id="svc-seerr-url" placeholder="http://localhost:5055"></div>
          <div class="field"><label>Seerr API Key</label><input type="password" id="svc-seerr-key" placeholder="•••••••••••"></div>
        </div>
        <button class="btn-primary" style="margin-top:16px" onclick="saveSvcSettings()">Save Integrations</button>
      </div>

      <!-- ── Appearance ── -->
      <div class="panel">
        <div class="panel-title">🎨 Appearance</div>
        <div style="font-size:12px;color:var(--text3);margin-bottom:14px">Theme, accent color, and background. Saved to localStorage — persists across sessions.</div>

        <!-- Theme buttons -->
        <div class="field">
          <label style="margin-bottom:6px;display:block">Theme</label>
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            <button class="theme-btn" data-t="dark"       onclick="applyTheme('dark')"       style="padding:6px 14px;border-radius:var(--r);border:1px solid var(--border);background:var(--surface);color:var(--text);cursor:pointer;font-size:13px">🌑 Dark</button>
            <button class="theme-btn" data-t="light"      onclick="applyTheme('light')"      style="padding:6px 14px;border-radius:var(--r);border:1px solid var(--border);background:var(--surface);color:var(--text);cursor:pointer;font-size:13px">☀️ Light</button>
            <button class="theme-btn" data-t="nord"       onclick="applyTheme('nord')"       style="padding:6px 14px;border-radius:var(--r);border:1px solid var(--border);background:var(--surface);color:var(--text);cursor:pointer;font-size:13px">🧊 Nord</button>
            <button class="theme-btn" data-t="catppuccin" onclick="applyTheme('catppuccin')" style="padding:6px 14px;border-radius:var(--r);border:1px solid var(--border);background:var(--surface);color:var(--text);cursor:pointer;font-size:13px">🐱 Catppuccin</button>
            <button class="theme-btn" data-t="dracula"    onclick="applyTheme('dracula')"    style="padding:6px 14px;border-radius:var(--r);border:1px solid var(--border);background:var(--surface);color:var(--text);cursor:pointer;font-size:13px">🧛 Dracula</button>
          </div>
        </div>

        <!-- Accent color swatches -->
        <div class="field" style="margin-top:14px">
          <label style="margin-bottom:6px;display:block">Accent Color</label>
          <div style="display:flex;gap:10px;align-items:center">
            <button class="accent-swatch" data-a="blue"   onclick="applyAccent('blue')"   title="Blue"   style="width:30px;height:30px;border-radius:50%;background:#388bfd;border:2px solid transparent;cursor:pointer"></button>
            <button class="accent-swatch" data-a="purple" onclick="applyAccent('purple')" title="Purple" style="width:30px;height:30px;border-radius:50%;background:#bc8cff;border:2px solid transparent;cursor:pointer"></button>
            <button class="accent-swatch" data-a="green"  onclick="applyAccent('green')"  title="Green"  style="width:30px;height:30px;border-radius:50%;background:#3fb950;border:2px solid transparent;cursor:pointer"></button>
            <button class="accent-swatch" data-a="orange" onclick="applyAccent('orange')" title="Orange" style="width:30px;height:30px;border-radius:50%;background:#f78166;border:2px solid transparent;cursor:pointer"></button>
            <button class="accent-swatch" data-a="pink"   onclick="applyAccent('pink')"   title="Pink"   style="width:30px;height:30px;border-radius:50%;background:#f778ba;border:2px solid transparent;cursor:pointer"></button>
            <button class="accent-swatch" data-a="cyan"   onclick="applyAccent('cyan')"   title="Cyan"   style="width:30px;height:30px;border-radius:50%;background:#56d9e0;border:2px solid transparent;cursor:pointer"></button>
          </div>
        </div>

        <!-- Background image -->
        <div class="field" style="margin-top:14px">
          <label>Background Image URL</label>
          <input type="text" id="bg-url-input" placeholder="https://example.com/wallpaper.jpg or https://unsplash.com/photos/...">
          <div class="field-hint">Paste any image URL or an Unsplash photo page URL. Leave blank for solid color.</div>
        </div>
        <div class="field">
          <label>Blur: <span id="bg-blur-val">4</span>px</label>
          <input type="range" id="bg-blur-input" min="0" max="20" value="4" oninput="document.getElementById('bg-blur-val').textContent=this.value" style="width:100%;accent-color:var(--blue)">
        </div>
        <div class="field">
          <label>Overlay Darkness: <span id="bg-overlay-val">70</span>%</label>
          <input type="range" id="bg-overlay-input" min="0" max="95" value="70" oninput="document.getElementById('bg-overlay-val').textContent=this.value" style="width:100%;accent-color:var(--blue)">
        </div>
        <div style="display:flex;gap:8px;margin-top:14px">
          <button class="btn-primary" onclick="saveAppearance()">Apply Background</button>
          <button class="btn" onclick="resetAppearance()">Reset All</button>
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">About</div>
        <div class="ctr-row"><span>ArrHub Version</span><span>3.10.0</span></div>
        <div class="ctr-row"><span>Auth Status</span><span style="color:var(--green)">Disabled (open access)</span></div>
        <div class="ctr-row"><span>WebUI Port</span><span>9999</span></div>
      </div>
    </div>

    <!-- ── RSS FEEDS ── -->
    <div id="tab-rss" class="tab-panel">
      <!-- RSS header with category tabs + view toggle -->
      <div class="section-header">
        <div class="section-title">Live Feeds</div>
        <div style="display:flex;gap:8px;align-items:center">
          <button class="view-btn active" id="rss-view-feeds" onclick="setRSSView('feeds')">📰 Feeds</button>
          <button class="view-btn" id="rss-view-live" onclick="setRSSView('live')">📺 Live News</button>
          <button class="view-btn" id="rss-view-iptv" onclick="setRSSView('iptv')">🎬 Free TV</button>
          <div id="rss-all-controls" style="gap:6px;align-items:center">
            <button class="btn" style="font-size:11px;padding:3px 10px" onclick="rssExpandAll()">⊞ Expand All</button>
            <button class="btn" style="font-size:11px;padding:3px 10px" onclick="rssCollapseAll()">⊟ Collapse All</button>
          </div>
          <button class="btn-primary" onclick="loadRSSFeeds()">↺ Refresh</button>
        </div>
      </div>

      <!-- Category filter pills -->
      <div class="filter-row" id="rss-cat-pills" style="flex-wrap:wrap;gap:6px;margin-bottom:12px"></div>

      <!-- Feeds view -->
      <div id="rss-feeds-view">
        <div id="rss-content" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px;align-items:start"></div>
      </div>

      <!-- Live News view — confirmed-working embeds + quick-launch cards for the rest -->
      <!-- YouTube's live_stream?channel= embed only works when the channel explicitly
           allows embedding. Al Jazeera and France 24 allow it; others often block it.
           For blocked channels, quick-launch cards open the stream in a new tab. -->
      <div id="rss-live-view" style="display:none">
        <div style="font-size:11px;color:var(--text3);margin-bottom:12px">
          📺 Live news streams — confirmed embeds play inline; others open YouTube in a new tab.
        </div>

        <!-- ── Confirmed embeds ──────────────────────────────────────────── -->
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px;margin-bottom:20px">
          <div class="panel">
            <div class="panel-title">🌍 Al Jazeera English</div>
            <iframe src="https://www.youtube.com/embed/live_stream?channel=UCNye-wNBqNL5ZzHSJj3l8Bg&autoplay=0&rel=0&modestbranding=1" style="width:100%;height:240px;border:none;border-radius:6px;background:#000" loading="lazy" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>
            <a href="https://www.youtube.com/@aljazeeraenglish/live" target="_blank" rel="noopener noreferrer" style="display:block;text-align:center;font-size:11px;color:var(--text3);margin-top:6px;text-decoration:none">↗ Open in YouTube</a>
          </div>
          <div class="panel">
            <div class="panel-title">🇫🇷 France 24 English</div>
            <iframe src="https://www.youtube.com/embed/live_stream?channel=UCQfwfsi5VrQ8yKZ-UGuIzgA&autoplay=0&rel=0&modestbranding=1" style="width:100%;height:240px;border:none;border-radius:6px;background:#000" loading="lazy" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>
            <a href="https://www.youtube.com/@FRANCE24English/live" target="_blank" rel="noopener noreferrer" style="display:block;text-align:center;font-size:11px;color:var(--text3);margin-top:6px;text-decoration:none">↗ Open in YouTube</a>
          </div>
          <div class="panel">
            <div class="panel-title">🇩🇪 DW News</div>
            <iframe src="https://www.youtube.com/embed/live_stream?channel=UCknLrEdhRCp1aegoMqRaCZg&autoplay=0&rel=0&modestbranding=1" style="width:100%;height:240px;border:none;border-radius:6px;background:#000" loading="lazy" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>
            <a href="https://www.youtube.com/@dwnews/live" target="_blank" rel="noopener noreferrer" style="display:block;text-align:center;font-size:11px;color:var(--text3);margin-top:6px;text-decoration:none">↗ Open in YouTube</a>
          </div>
        </div>

        <!-- ── Quick-launch cards (open YouTube live in new tab) ─────────── -->
        <div style="font-size:12px;font-weight:600;color:var(--text);margin-bottom:8px;display:flex;align-items:center;gap:6px">
          <span>⚡ More Live Streams</span>
          <span style="font-size:10px;font-weight:400;color:var(--text3)">(opens YouTube — some may block embedded playback)</span>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px;margin-bottom:20px">
          <a href="https://www.youtube.com/@BBCNews/live" target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:6px;padding:14px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''">
            <div style="font-size:28px">🇬🇧</div>
            <div style="font-weight:600;color:var(--text);font-size:12px;text-align:center">BBC World News</div>
            <div style="font-size:10px;color:var(--blue)">▶ Watch Live</div>
          </a>
          <a href="https://www.youtube.com/@SkyNews/live" target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:6px;padding:14px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''">
            <div style="font-size:28px">🌐</div>
            <div style="font-weight:600;color:var(--text);font-size:12px;text-align:center">Sky News</div>
            <div style="font-size:10px;color:var(--blue)">▶ Watch Live</div>
          </a>
          <a href="https://www.youtube.com/@Bloomberg/live" target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:6px;padding:14px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''">
            <div style="font-size:28px">📊</div>
            <div style="font-weight:600;color:var(--text);font-size:12px;text-align:center">Bloomberg</div>
            <div style="font-size:10px;color:var(--blue)">▶ Watch Live</div>
          </a>
          <a href="https://www.youtube.com/@ABCNews/live" target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:6px;padding:14px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''">
            <div style="font-size:28px">🇺🇸</div>
            <div style="font-weight:600;color:var(--text);font-size:12px;text-align:center">ABC News Live</div>
            <div style="font-size:10px;color:var(--blue)">▶ Watch Live</div>
          </a>
          <a href="https://www.youtube.com/@euronews/live" target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:6px;padding:14px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''">
            <div style="font-size:28px">🇪🇺</div>
            <div style="font-weight:600;color:var(--text);font-size:12px;text-align:center">Euronews</div>
            <div style="font-size:10px;color:var(--blue)">▶ Watch Live</div>
          </a>
          <a href="https://www.youtube.com/@NHKWorldNews/live" target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:6px;padding:14px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''">
            <div style="font-size:28px">🌏</div>
            <div style="font-weight:600;color:var(--text);font-size:12px;text-align:center">NHK World</div>
            <div style="font-size:10px;color:var(--blue)">▶ Watch Live</div>
          </a>
          <a href="https://www.youtube.com/@wionews/live" target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:6px;padding:14px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''">
            <div style="font-size:28px">🌍</div>
            <div style="font-weight:600;color:var(--text);font-size:12px;text-align:center">WION</div>
            <div style="font-size:10px;color:var(--blue)">▶ Watch Live</div>
          </a>
          <a href="https://www.youtube.com/@CBSnews/live" target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:6px;padding:14px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''">
            <div style="font-size:28px">🎙️</div>
            <div style="font-weight:600;color:var(--text);font-size:12px;text-align:center">CBS News</div>
            <div style="font-size:10px;color:var(--blue)">▶ Watch Live</div>
          </a>
          <a href="https://www.youtube.com/@lofimusic/live" target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:6px;padding:14px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''">
            <div style="font-size:28px">🎵</div>
            <div style="font-weight:600;color:var(--text);font-size:12px;text-align:center">Lofi Girl</div>
            <div style="font-size:10px;color:var(--blue)">▶ Watch Live</div>
          </a>
        </div>
      </div>

      <!-- ─────────────────────────────────────────────────────────────────────
           Free TV view — dlstreams.top channel browser + popular quick-launch
           cards + custom M3U8 player + free streaming links
           ───────────────────────────────────────────────────────────────────── -->
      <div id="rss-iptv-view" style="display:none">

        <!-- ① dlstreams.top embedded channel browser -->
        <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:10px">
          <div>
            <div style="font-size:12px;font-weight:600;color:var(--text)">📡 DaddyLive — 1000+ Free Channels</div>
            <div style="font-size:11px;color:var(--text3);margin-top:2px">Browse and click any channel to open its stream in the player below</div>
          </div>
          <a href="https://dlstreams.top/24-7-channels.php" target="_blank" rel="noopener noreferrer" class="btn" style="font-size:11px;padding:4px 12px;text-decoration:none">
            ↗ Open in New Tab
          </a>
        </div>

        <!-- Channel browser iframe — embed the 24/7 listing page -->
        <div style="border:1px solid var(--border);border-radius:var(--r);overflow:hidden;margin-bottom:16px">
          <iframe id="dlstreams-browser"
            src="https://dlstreams.top/24-7-channels.php"
            style="width:100%;height:480px;border:none;background:var(--bg2)"
            sandbox="allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-forms allow-top-navigation-by-user-activation"
            referrerpolicy="no-referrer"
            loading="lazy"
            title="DaddyLive Channel Browser">
          </iframe>
          <!-- Shown only if iframe is blocked by X-Frame-Options -->
          <noscript><div style="padding:20px;text-align:center;color:var(--text3)">Enable JavaScript or <a href="https://dlstreams.top/24-7-channels.php" target="_blank" rel="noopener noreferrer" style="color:var(--blue)">open site directly</a></div></noscript>
        </div>

        <!-- ② Popular channel quick-launch cards (open watch page in new tab) -->
        <div style="font-size:12px;font-weight:600;color:var(--text);margin-bottom:8px;display:flex;align-items:center;gap:6px">
          <span>⚡ Quick Launch</span>
          <span style="font-size:10px;font-weight:400;color:var(--text3)">(opens channel in new tab)</span>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;margin-bottom:20px">
          <a href="https://dlstreams.top/watch.php?id=51"  target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:4px;padding:12px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''"><div style="font-size:22px">🇺🇸</div><div style="font-weight:600;color:var(--text);font-size:12px">ABC USA</div></a>
          <a href="https://dlstreams.top/watch.php?id=44"  target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:4px;padding:12px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''"><div style="font-size:22px">🏈</div><div style="font-weight:600;color:var(--text);font-size:12px">ESPN USA</div></a>
          <a href="https://dlstreams.top/watch.php?id=35"  target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:4px;padding:12px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''"><div style="font-size:22px">⚽</div><div style="font-weight:600;color:var(--text);font-size:12px">Sky Sports</div></a>
          <a href="https://dlstreams.top/watch.php?id=61"  target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:4px;padding:12px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''"><div style="font-size:22px">🌍</div><div style="font-weight:600;color:var(--text);font-size:12px">beIN Sports</div></a>
          <a href="https://dlstreams.top/watch.php?id=72"  target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:4px;padding:12px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''"><div style="font-size:22px">📺</div><div style="font-weight:600;color:var(--text);font-size:12px">CNN USA</div></a>
          <a href="https://dlstreams.top/watch.php?id=57"  target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:4px;padding:12px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''"><div style="font-size:22px">📰</div><div style="font-weight:600;color:var(--text);font-size:12px">Fox News</div></a>
          <a href="https://dlstreams.top/watch.php?id=78"  target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:4px;padding:12px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''"><div style="font-size:22px">🇬🇧</div><div style="font-weight:600;color:var(--text);font-size:12px">BBC News</div></a>
          <a href="https://dlstreams.top/watch.php?id=97"  target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:4px;padding:12px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''"><div style="font-size:22px">🏀</div><div style="font-weight:600;color:var(--text);font-size:12px">NBA TV</div></a>
          <a href="https://dlstreams.top/watch.php?id=68"  target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:4px;padding:12px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''"><div style="font-size:22px">🎬</div><div style="font-weight:600;color:var(--text);font-size:12px">TNT USA</div></a>
          <a href="https://dlstreams.top/watch.php?id=86"  target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:4px;padding:12px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''"><div style="font-size:22px">🚀</div><div style="font-weight:600;color:var(--text);font-size:12px">NASA TV</div></a>
          <a href="https://dlstreams.top/watch.php?id=42"  target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:4px;padding:12px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''"><div style="font-size:22px">🇩🇪</div><div style="font-weight:600;color:var(--text);font-size:12px">DW News</div></a>
          <a href="https://dlstreams.top/24-7-channels.php" target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:4px;padding:12px;cursor:pointer;margin:0;border-style:dashed" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''"><div style="font-size:22px">+</div><div style="font-weight:600;color:var(--text3);font-size:12px">More…</div></a>
        </div>

        <!-- ③ Custom M3U8 URL player -->
        <details style="margin-bottom:16px">
          <summary style="cursor:pointer;font-size:12px;font-weight:600;color:var(--text);padding:8px 0;user-select:none">🔗 Custom HLS / M3U8 Stream Player</summary>
          <div style="padding:10px 0">
            <div style="font-size:11px;color:var(--text3);margin-bottom:8px">Paste any direct M3U8 stream URL — plays in-browser via HLS.js</div>
            <div style="display:flex;gap:8px;align-items:flex-start;flex-wrap:wrap">
              <input type="text" id="hls-custom-url" placeholder="https://example.com/stream.m3u8"
                style="flex:1;min-width:260px;padding:8px 12px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);font-size:12px">
              <button class="btn-primary" style="white-space:nowrap" onclick="hlsPlay('hls-custom',document.getElementById('hls-custom-url').value.trim())">▶ Play</button>
            </div>
            <video id="hls-custom" controls muted playsinline
              style="width:100%;height:280px;background:#000;border-radius:var(--r);margin-top:8px;object-fit:contain;display:none"></video>
          </div>
        </details>

        <!-- ④ Free streaming services — external links -->
        <div style="font-size:12px;font-weight:600;color:var(--text);margin-bottom:8px;display:flex;align-items:center;gap:6px">
          <span>📺 Free Streaming Services</span>
          <span style="font-size:10px;font-weight:400;color:var(--text3)">(opens in new tab)</span>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px">
          <a href="https://pluto.tv" target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:5px;padding:14px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''">
            <div style="font-size:24px">📡</div><div style="font-weight:600;color:var(--text);font-size:12px">Pluto TV</div>
            <div style="font-size:10px;color:var(--text3);text-align:center">250+ channels</div>
            <div style="font-size:10px;background:var(--green2);color:var(--green);padding:1px 6px;border-radius:8px;font-weight:600">FREE</div>
          </a>
          <a href="https://tubitv.com" target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:5px;padding:14px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''">
            <div style="font-size:24px">🎬</div><div style="font-weight:600;color:var(--text);font-size:12px">Tubi</div>
            <div style="font-size:10px;color:var(--text3);text-align:center">Movies &amp; shows</div>
            <div style="font-size:10px;background:var(--green2);color:var(--green);padding:1px 6px;border-radius:8px;font-weight:600">FREE</div>
          </a>
          <a href="https://watch.plex.tv/live-tv" target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:5px;padding:14px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''">
            <div style="font-size:24px">▶️</div><div style="font-weight:600;color:var(--text);font-size:12px">Plex Free TV</div>
            <div style="font-size:10px;color:var(--text3);text-align:center">Live TV &amp; VOD</div>
            <div style="font-size:10px;background:var(--green2);color:var(--green);padding:1px 6px;border-radius:8px;font-weight:600">FREE</div>
          </a>
          <a href="https://www.peacocktv.com" target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:5px;padding:14px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''">
            <div style="font-size:24px">🦚</div><div style="font-weight:600;color:var(--text);font-size:12px">Peacock</div>
            <div style="font-size:10px;color:var(--text3);text-align:center">NBC free tier</div>
            <div style="font-size:10px;background:var(--green2);color:var(--green);padding:1px 6px;border-radius:8px;font-weight:600">FREE TIER</div>
          </a>
          <a href="https://therokuchannel.roku.com" target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:5px;padding:14px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''">
            <div style="font-size:24px">📺</div><div style="font-weight:600;color:var(--text);font-size:12px">Roku Channel</div>
            <div style="font-size:10px;color:var(--text3);text-align:center">Movies &amp; live TV</div>
            <div style="font-size:10px;background:var(--green2);color:var(--green);padding:1px 6px;border-radius:8px;font-weight:600">FREE</div>
          </a>
          <a href="https://www.crackle.com" target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:5px;padding:14px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''">
            <div style="font-size:24px">🎥</div><div style="font-weight:600;color:var(--text);font-size:12px">Crackle</div>
            <div style="font-size:10px;color:var(--text3);text-align:center">Movies &amp; originals</div>
            <div style="font-size:10px;background:var(--green2);color:var(--green);padding:1px 6px;border-radius:8px;font-weight:600">FREE</div>
          </a>
        </div>
      </div>
    </div>

  </div><!-- /content -->
</div><!-- /main -->
</div><!-- /app -->

<!-- ══ MOBILE BOTTOM NAV ══ -->
<nav id="bottom-nav">
  <button class="bn-item active" onclick="showTab('overview',this);closeSidebar()">
    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 5a1 1 0 011-1h4a1 1 0 011 1v5a1 1 0 01-1 1H5a1 1 0 01-1-1V5zm10 0a1 1 0 011-1h4a1 1 0 011 1v2a1 1 0 01-1 1h-4a1 1 0 01-1-1V5zM4 15a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1H5a1 1 0 01-1-1v-4zm10-3a1 1 0 011-1h4a1 1 0 011 1v7a1 1 0 01-1 1h-4a1 1 0 01-1-1v-7z"/></svg>
    <span>Overview</span>
  </button>
  <button class="bn-item" onclick="showTab('containers',this);closeSidebar()">
    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/></svg>
    <span>Containers</span>
  </button>
  <button class="bn-item" onclick="showTab('deploy',this);closeSidebar()">
    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6m0 0v6m0-6h6m-6 0H6"/></svg>
    <span>Deploy</span>
  </button>
  <button class="bn-item" onclick="showTab('logs',this);closeSidebar()">
    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
    <span>Logs</span>
  </button>
  <button class="bn-item" onclick="toggleSidebar()">
    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16"/></svg>
    <span>More</span>
  </button>
</nav>

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
   ARRHUB FRONTEND — no auth, pure vanilla JS
   All new features: toasts, SSE banner, mobile, skeletons, favorites,
   alerts bar, update-recreate, sort, table view.
   ===================================================================== */

const API = '';
let currentTab = 'overview';
let allContainers = [];
let ctrFilter = 'all';
let allCatalog = [];
let catFilter = 'all';

// ── Container table view state ─────────────────────────────────────────
let ctrViewMode = 'grid';         // 'grid' | 'table'
let ctrTableSort = {col: 'name', dir: 'asc'};
let ctrStatsCache = {};           // name -> {cpu_pct, mem_pct, mem_usage_mb}

// ══════════════════════════════════════════════════════════════════════
// 1. TOAST SYSTEM
// ══════════════════════════════════════════════════════════════════════
const TOAST_ICONS = {success:'✓', error:'✕', info:'ℹ', warn:'⚠'};

/**
 * showToast(msg, type, duration)
 * type: 'success' | 'error' | 'info' | 'warn'
 */
function showToast(msg, type='info', duration=4000) {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const el = document.createElement('div');
    el.className = 'toast ' + type;
    el.innerHTML = `
        <span class="toast-icon">${TOAST_ICONS[type] || 'ℹ'}</span>
        <div class="toast-body"><div class="toast-msg">${msg}</div></div>
        <button class="toast-close" onclick="dismissToast(this.parentElement)">×</button>`;
    container.appendChild(el);
    // Auto-dismiss
    const timer = setTimeout(() => dismissToast(el), duration);
    el._timer = timer;
}

function dismissToast(el) {
    if (!el || el._dismissed) return;
    el._dismissed = true;
    clearTimeout(el._timer);
    el.classList.add('toast-exit');
    setTimeout(() => el.remove(), 280);
}

// ══════════════════════════════════════════════════════════════════════
// 2. SSE DISCONNECTED BANNER
// ══════════════════════════════════════════════════════════════════════
function showSSEBanner() {
    const b = document.getElementById('sse-banner');
    if (b) { b.classList.add('visible'); document.body.classList.add('sse-disconnected'); }
}
function hideSSEBanner() {
    const b = document.getElementById('sse-banner');
    if (b) { b.classList.remove('visible'); document.body.classList.remove('sse-disconnected'); }
}
function retrySSE() {
    if (evtSource) { evtSource.close(); evtSource = null; }
    startSSE();
}

// ══════════════════════════════════════════════════════════════════════
// 3. MOBILE SIDEBAR
// ══════════════════════════════════════════════════════════════════════
function toggleSidebar() {
    const sb = document.getElementById('sidebar');
    const ov = document.getElementById('sidebar-overlay');
    if (sb) sb.classList.toggle('sidebar-open');
    if (ov) ov.classList.toggle('visible');
}
function closeSidebar() {
    const sb = document.getElementById('sidebar');
    const ov = document.getElementById('sidebar-overlay');
    if (sb) sb.classList.remove('sidebar-open');
    if (ov) ov.classList.remove('visible');
}

// ── Desktop sidebar collapse ─────────────────────────────────────────
function toggleSidebarDesktop() {
    const app = document.getElementById('app');
    const collapsed = app.classList.toggle('sb-collapsed');
    localStorage.setItem('arrhub_sb_collapsed', collapsed ? '1' : '0');
    const icon = document.getElementById('sb-collapse-icon');
    if (icon) {
        const p = icon.querySelector('path');
        if (p) p.setAttribute('d', collapsed
            ? 'M13 5l7 7-7 7M5 5l7 7-7 7'   // expand arrows →→
            : 'M11 19l-7-7 7-7m8 14l-7-7 7-7'); // collapse arrows ←←
    }
    const btn = document.getElementById('sb-collapse-btn');
    if (btn) btn.title = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
}
// Restore sidebar collapse state on load
(function _restoreSbCollapse() {
    if (localStorage.getItem('arrhub_sb_collapsed') === '1') {
        const app = document.getElementById('app');
        if (app) app.classList.add('sb-collapsed');
        const icon = document.getElementById('sb-collapse-icon');
        if (icon) {
            const p = icon.querySelector('path');
            if (p) p.setAttribute('d', 'M13 5l7 7-7 7M5 5l7 7-7 7');
        }
        const btn = document.getElementById('sb-collapse-btn');
        if (btn) btn.title = 'Expand sidebar';
    }
})();

// ── Container card size slider ───────────────────────────────────────
function setCtrCardSize(val) {
    document.documentElement.style.setProperty('--ctr-card-min', val + 'px');
    localStorage.setItem('arrhub_ctr_card_min', val);
}
// Restore card size on load
(function _restoreCtrCardSize() {
    const saved = localStorage.getItem('arrhub_ctr_card_min');
    if (saved) {
        document.documentElement.style.setProperty('--ctr-card-min', saved + 'px');
        const r = document.getElementById('ctr-size-range');
        if (r) r.value = saved;
    }
})();

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
    document.querySelectorAll('.bn-item').forEach(i => i.classList.remove('active'));
    const panel = document.getElementById('tab-' + name);
    if (panel) { panel.classList.add('active'); panel.classList.add('fade-in'); }
    if (el) el.classList.add('active');
    currentTab = name;

    // Lazy-load on first show
    if (name === 'overview') loadServiceLauncher();
    else if (name === 'containers') loadContainers();
    else if (name === 'stornet') { loadStorage(); loadNetwork(); }
    else if (name === 'ports') loadPortMap();
    else if (name === 'hardware') loadHardware();
    else if (name === 'logs') loadLogs();
    else if (name === 'deploy') loadCatalog();
    else if (name === 'stack') { loadStackManager(); loadDeployHistory(); }
    else if (name === 'backup') loadBackups();
    else if (name === 'updates') checkUpdates();
    else if (name === 'settings') loadSettings();
    else if (name === 'rss') loadRSSFeeds();
}

function openExternalLink(url) {
    // Replace localhost with actual server hostname so links work remotely
    const host = window.location.hostname;
    url = url.replace('localhost', host).replace('127.0.0.1', host);
    window.open(url, '_blank');
}

// ── Chart.js Gauges ───────────────────────────────────────────────────
let _cpuChart = null, _memChart = null;

function _makeGaugeChart(canvasId, color) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;
    return new Chart(ctx.getContext('2d'), {
        type: 'doughnut',
        data: {
            datasets: [{
                data: [0, 100],
                backgroundColor: [color, 'rgba(48,54,61,0.8)'],
                borderWidth: 0,
                hoverOffset: 0
            }]
        },
        options: {
            responsive: false,
            cutout: '72%',
            circumference: 270,
            rotation: 225,
            animation: { duration: 400 },
            plugins: { legend: { display: false }, tooltip: { enabled: false } }
        }
    });
}

function initGauges() {
    _cpuChart = _makeGaugeChart('cpu-gauge-canvas', '#388bfd');
    _memChart = _makeGaugeChart('mem-gauge-canvas', '#bc8cff');
}

function updateGauge(chart, textId, value, max) {
    if (!chart) return;
    const pct = Math.min(100, Math.max(0, (value / max) * 100));
    const color = pct < 50 ? '#3fb950' : pct < 80 ? '#e3b341' : '#f85149';
    chart.data.datasets[0].data = [pct, 100 - pct];
    chart.data.datasets[0].backgroundColor[0] = color;
    chart.update('none');
    const text = document.getElementById(textId);
    if (text) { text.textContent = Math.round(pct) + '%'; text.style.color = color; }
}

function pbarColor(pct) {
    if (pct < 50) return 'green';
    if (pct < 80) return 'yellow';
    return 'red';
}

// ── SSE live stream ───────────────────────────────────────────────────
// Uses exponential backoff on reconnect so a post-wizard gunicorn restart
// (which makes the first few retries fail quickly) doesn't flood the server.
// Backoff: 3s → 6s → 12s → 24s → 30s (capped), resets to 3s on success.
let evtSource = null;
let _sseWasConnected = false;
let _sseRetryCount = 0;
const _SSE_BASE_RETRY_MS = 3000;
const _SSE_MAX_RETRY_MS  = 30000;

function _sseRetryDelay() {
    // Exponential: base * 2^(n-1), capped at max
    return Math.min(_SSE_BASE_RETRY_MS * Math.pow(2, Math.max(_sseRetryCount - 1, 0)), _SSE_MAX_RETRY_MS);
}

function startSSE() {
    if (evtSource) return;   // already connecting/connected
    evtSource = new EventSource(API + '/api/stream');
    const badge = document.getElementById('live-badge');

    evtSource.onopen = () => {
        _sseRetryCount = 0;   // success — reset backoff counter
        if (badge) badge.style.display = 'inline-flex';
        hideSSEBanner();
        // Show "restored" toast only after a previous connection loss
        if (_sseWasConnected) showToast('Live metrics restored', 'success', 3000);
        _sseWasConnected = true;
    };

    evtSource.onmessage = (e) => {
        try {
            const d = JSON.parse(e.data);
            const cpu = d.cpu_percent; const ram = d.mem_percent;
            setEl('tb-cpu', cpu + '%');
            setEl('tb-ram', ram + '%');
            setEl('tb-load', d.load_1m);
            colorEl('tb-cpu', cpu < 50 ? '' : cpu < 80 ? 'orange' : 'red');
            colorEl('tb-ram', ram < 50 ? '' : ram < 80 ? 'orange' : 'red');
            updateGauge(_cpuChart,'cpu-gauge-text', cpu, 100);
            updateGauge(_memChart,'mem-gauge-text', ram, 100);
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
        // Null before close so the guard at the top doesn't block the retry
        const dying = evtSource;
        evtSource = null;
        try { dying.close(); } catch(e) {}

        // Show banner only after a first successful connection (not on initial load failure)
        if (_sseWasConnected) showSSEBanner();

        _sseRetryCount++;
        const delay = _sseRetryDelay();
        // Update banner text so user can see reconnect is in progress
        const bannerMsg = document.getElementById('sse-banner-msg');
        if (bannerMsg) bannerMsg.textContent =
            `Lost connection — reconnecting in ${Math.round(delay/1000)}s (attempt ${_sseRetryCount})…`;
        setTimeout(startSSE, delay);
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
            updateGauge(_cpuChart,'cpu-gauge-text', d.cpu_percent, 100);
            setEl('cpu-badge', d.cpu_percent + '%');
        }
        if (d.mem_percent !== undefined) {
            updateGauge(_memChart,'mem-gauge-text', d.mem_percent, 100);
            setEl('mem-badge', d.mem_percent + '%');
            const used = ((d.mem_used||0)/1e9).toFixed(1);
            const total = ((d.mem_total||0)/1e9).toFixed(1);
            setEl('mem-detail', used + ' / ' + total + ' GB');
        }
    } catch(e) {}
}

// ══════════════════════════════════════════════════════════════════════
// 4. SKELETON HELPERS
// ══════════════════════════════════════════════════════════════════════
/**
 * showSkeleton(containerId, count) — inject skeleton placeholder cards
 */
function showSkeleton(containerId, count=3) {
    const el = document.getElementById(containerId);
    if (!el) return;
    el.innerHTML = Array.from({length: count}, () => `
        <div class="skeleton-card">
          <div style="display:flex;gap:10px;align-items:center">
            <div class="sk-circle skeleton"></div>
            <div style="flex:1;display:flex;flex-direction:column;gap:6px">
              <div class="sk-line med skeleton"></div>
              <div class="sk-line short skeleton"></div>
            </div>
          </div>
          <div class="sk-rect skeleton"></div>
          <div class="sk-line full skeleton"></div>
        </div>`).join('');
}

/**
 * clearSkeleton(containerId) — remove skeleton cards (actual content replaces)
 */
function clearSkeleton(containerId) {
    // Content will be directly replaced by renderContainers() etc; this is a no-op
    // but kept as hook if needed for explicit clearing.
}

// ── Containers ─────────────────────────────────────────────────────────
async function loadContainers() {
    // Show skeleton while loading
    if (!allContainers.length) showSkeleton('ctr-grid', 3);
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
        // Refresh alerts since container state changed
        refreshAlerts();
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
    watchtower:'👁️', dozzle:'📋', uptime:'✅', launcharr:'🚀', dasherr:'🏠',
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

// ══════════════════════════════════════════════════════════════════════
// 9. CONTAINER VIEW MODE (grid / table)
// ══════════════════════════════════════════════════════════════════════
function setCtrView(mode) {
    ctrViewMode = mode;
    // Use explicit display values — '' would inherit the CSS display:none default
    document.getElementById('ctr-grid').style.display = mode === 'grid' ? 'grid' : 'none';
    document.getElementById('ctr-table-wrap').style.display = mode === 'table' ? 'block' : 'none';
    document.getElementById('btn-grid-view').classList.toggle('active', mode === 'grid');
    document.getElementById('btn-table-view').classList.toggle('active', mode === 'table');
    if (mode === 'table') renderContainerTable();
}

// Build the static HTML shell for one container card (charts injected separately).
// Splitting static HTML from dynamic state lets us update cards in-place without
// destroying Chart.js canvas instances.
function _ctrCardHTML(c) {
    const sc = statusClass(c.status);
    const icon = ctrIcon(c.name);
    const ports = c.ports && c.ports.length ? c.ports.join(', ') : '—';
    const uptime = c.uptime || '—';
    const isRunning = c.status === 'running';
    return `
<div class="ctr-card" id="card-${c.name}" data-status="${c.status}">
  <div class="ctr-header">
    <div class="ctr-icon">${icon}</div>
    <div class="ctr-info">
      <div class="ctr-name">${c.name}</div>
      <div class="ctr-image">${c.image}</div>
      <div>
        <span class="ctr-status ${sc}" id="ctr-status-badge-${c.name}">
          <span class="ctr-status-dot"></span>${c.status}
        </span>
      </div>
    </div>
  </div>
  <div class="ctr-body">
    <div class="ctr-row"><span>Uptime</span><span id="ctr-uptime-${c.name}">${uptime}</span></div>
    <div class="ctr-row"><span>ID</span><span>${c.id}</span></div>
    <div class="ctr-row"><span>Ports</span><span class="ctr-ports">${ports}</span></div>
  </div>
  <div class="ctr-stats" style="display:flex;gap:16px;justify-content:center;padding:8px 14px;align-items:flex-start">
    <div style="text-align:center">
      <div style="position:relative;width:80px;height:80px;filter:drop-shadow(0 0 6px rgba(63,185,80,0.3))">
        <canvas id="donut-cpu-${c.name}" width="80" height="80"></canvas>
        <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center">
          <span id="stat-cpu-${c.name}" style="font-size:13px;font-weight:700;color:#fff;font-family:var(--mono)">—</span>
        </div>
      </div>
      <div style="font-size:10px;color:var(--text3);margin-top:4px">CPU</div>
    </div>
    <div style="text-align:center">
      <div style="position:relative;width:80px;height:80px">
        <canvas id="donut-mem-${c.name}" width="80" height="80"></canvas>
        <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center">
          <span id="stat-mem-${c.name}" style="font-size:13px;font-weight:700;color:#fff;font-family:var(--mono)">—</span>
        </div>
      </div>
      <div style="font-size:10px;color:var(--text3);margin-top:4px">MEM</div>
    </div>
  </div>
  <div class="ctr-footer" id="ctr-footer-${c.name}">
    ${isRunning
      ? `<button class="btn red" onclick="ctrAction('${c.name}','stop')"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1" stroke-width="2"/></svg>Stop</button>`
      : `<button class="btn green" onclick="ctrAction('${c.name}','start')"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/></svg>Start</button>`}
    <button class="btn orange" onclick="ctrAction('${c.name}','restart')"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>Restart</button>
    <button class="btn blue" onclick="openLogs('${c.name}')"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>Logs</button>
    ${isRunning ? `<button class="btn purple" onclick="updateContainer('${c.name}')">⬆ Update</button>` : ''}
    <button class="btn red" onclick="ctrAction('${c.name}','remove')" style="margin-left:auto"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg></button>
  </div>
</div>`;
}

// Update only the mutable parts of an existing card (status badge, uptime, footer
// buttons) without touching the chart canvases.  This prevents the
// destroy→rebuild cycle that caused flickering donuts.
function _ctrCardUpdate(c) {
    const badge = document.getElementById('ctr-status-badge-' + c.name);
    if (badge) {
        badge.className = 'ctr-status ' + statusClass(c.status);
        badge.innerHTML = `<span class="ctr-status-dot"></span>${c.status}`;
    }
    const uptimeEl = document.getElementById('ctr-uptime-' + c.name);
    if (uptimeEl) uptimeEl.textContent = c.uptime || '—';

    // Swap footer buttons when running state changes
    const card = document.getElementById('card-' + c.name);
    if (card) {
        const prevStatus = card.dataset.status;
        if (prevStatus !== c.status) {
            card.dataset.status = c.status;
            const footer = document.getElementById('ctr-footer-' + c.name);
            if (footer) {
                // Replace only the start/stop button (first child)
                const isRunning = c.status === 'running';
                const newBtn = isRunning
                    ? `<button class="btn red" onclick="ctrAction('${c.name}','stop')"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1" stroke-width="2"/></svg>Stop</button>`
                    : `<button class="btn green" onclick="ctrAction('${c.name}','start')"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/></svg>Start</button>`;
                footer.innerHTML = newBtn +
                    `<button class="btn orange" onclick="ctrAction('${c.name}','restart')"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>Restart</button>
                    <button class="btn blue" onclick="openLogs('${c.name}')"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>Logs</button>
                    ${isRunning ? `<button class="btn purple" onclick="updateContainer('${c.name}')">⬆ Update</button>` : ''}
                    <button class="btn red" onclick="ctrAction('${c.name}','remove')" style="margin-left:auto"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg></button>`;
            }
            // Destroy chart only when container goes from running→stopped (stats no longer valid)
            if (!isRunning && _ctrCharts[c.name]) {
                try { _ctrCharts[c.name].cpu.destroy(); _ctrCharts[c.name].mem.destroy(); } catch(e) {}
                delete _ctrCharts[c.name];
                const cpuEl = document.getElementById('stat-cpu-' + c.name);
                const memEl = document.getElementById('stat-mem-' + c.name);
                if (cpuEl) cpuEl.textContent = '—';
                if (memEl) memEl.textContent = '—';
            }
        }
    }
}

function renderContainers() {
    const grid = document.getElementById('ctr-grid');
    // Always clear skeleton/placeholder elements first (prevents ghost cards)
    [...grid.children].forEach(el => { if (!el.classList.contains('ctr-card')) el.remove(); });

    let ctrs = allContainers;
    if (ctrFilter !== 'all') ctrs = ctrs.filter(c => c.status === ctrFilter);

    if (!ctrs.length) {
        // Destroy all charts before clearing
        Object.keys(_ctrCharts).forEach(n => {
            try { _ctrCharts[n].cpu.destroy(); _ctrCharts[n].mem.destroy(); } catch(e) {}
            delete _ctrCharts[n];
        });
        grid.innerHTML = '<div class="empty"><div class="empty-icon">📦</div><div class="empty-text">No containers match filter</div></div>';
        if (ctrViewMode === 'table') renderContainerTable();
        return;
    }

    // Sort: running first, then alphabetical
    ctrs = [...ctrs].sort((a,b)=>{
        if(a.status==='running' && b.status!=='running') return -1;
        if(a.status!=='running' && b.status==='running') return 1;
        return a.name.localeCompare(b.name);
    });

    const newNames = new Set(ctrs.map(c => c.name));
    const existingCards = new Set(
        [...grid.querySelectorAll('.ctr-card')].map(el => el.id.replace('card-', ''))
    );

    // Remove cards for containers that no longer exist / are filtered out
    existingCards.forEach(name => {
        if (!newNames.has(name)) {
            const el = document.getElementById('card-' + name);
            if (el) el.remove();
            if (_ctrCharts[name]) {
                try { _ctrCharts[name].cpu.destroy(); _ctrCharts[name].mem.destroy(); } catch(e) {}
                delete _ctrCharts[name];
            }
        }
    });

    // Remove any placeholder/skeleton/empty-state elements (but keep real .ctr-card nodes
    // so existing cards can be updated in-place without destroying their Chart.js canvases)
    [...grid.children].forEach(el => { if (!el.classList.contains('ctr-card')) el.remove(); });

    // Add new cards; update existing ones in-place (no chart destruction)
    ctrs.forEach((c, idx) => {
        if (!existingCards.has(c.name)) {
            // New container — inject card HTML then initialise charts in next tick
            const wrapper = document.createElement('div');
            wrapper.innerHTML = _ctrCardHTML(c).trim();
            const card = wrapper.firstChild;
            // Insert at correct sorted position
            const allCards = [...grid.querySelectorAll('.ctr-card')];
            const nextCard = allCards[idx];
            if (nextCard) grid.insertBefore(card, nextCard);
            else grid.appendChild(card);
            // Charts must be created after the canvas is in the DOM
            if (c.status === 'running') {
                requestAnimationFrame(() => loadCtrStats(c.name, true));
            }
        } else {
            // Existing container — update mutable fields only, leave charts alone
            _ctrCardUpdate(c);
            if (c.status === 'running') loadCtrStats(c.name, false);
        }
    });

    if (ctrViewMode === 'table') renderContainerTable();
}

async function ctrAction(name, action) {
    if (action === 'remove' && !confirm('Remove container ' + name + '?')) return;
    const actionLabels = {start:'Starting', stop:'Stopping', restart:'Restarting', remove:'Removing'};
    showToast((actionLabels[action] || action) + ' ' + name + '…', 'info', 3000);
    try {
        const r = await fetch(API + `/api/container/${name}/${action}`, {method:'POST'});
        const d = await r.json();
        if (d.error) {
            showToast('Error: ' + d.error, 'error');
        } else {
            const doneLabels = {start:'started', stop:'stopped', restart:'restarted', remove:'removed'};
            showToast(name + ' ' + (doneLabels[action] || action), 'success');
            setTimeout(loadContainers, 800);
        }
    } catch(e) { showToast('Request failed', 'error'); }
}

// ── 7. Update & Recreate ────────────────────────────────────────────────
async function updateContainer(name) {
    showToast('Pulling latest image for ' + name + '…', 'info', 8000);
    try {
        const r = await fetch(API + `/api/container/${name}/update`, {method:'POST'});
        const d = await r.json();
        if (d.error) {
            showToast('Update failed: ' + d.error, 'error');
        } else {
            showToast('✓ ' + name + ' updated and recreated', 'success', 5000);
            setTimeout(loadContainers, 1200);
        }
    } catch(e) { showToast('Update request failed', 'error'); }
}

// ── Container Chart.js donut registry ────────────────────────────────
const _ctrCharts = {};   // name -> {cpu: Chart, mem: Chart}

function _getCtrChartColor(pct, isCPU) {
    if (pct >= 80) return '#f85149';
    if (pct >= 50) return '#d29922';
    return isCPU ? '#3fb950' : '#a371f7';
}

function _ensureCtrCharts(name) {
    if (_ctrCharts[name]) return _ctrCharts[name];
    const cpuCanvas = document.getElementById('donut-cpu-' + name);
    const memCanvas = document.getElementById('donut-mem-' + name);
    if (!cpuCanvas || !memCanvas) return null;

    const makeChart = (canvas, initColor) => new Chart(canvas.getContext('2d'), {
        type: 'doughnut',
        data: {
            datasets: [{
                data: [0, 100],
                backgroundColor: [initColor, '#21262d'],
                borderWidth: 0,
                hoverOffset: 0,
                borderRadius: 4
            }]
        },
        options: {
            responsive: false,
            cutout: '72%',
            animation: { duration: 400 },
            plugins: { legend: { display: false }, tooltip: { enabled: false } }
        }
    });

    const charts = {
        cpu: makeChart(cpuCanvas, '#3fb950'),
        mem: makeChart(memCanvas, '#a371f7')
    };
    _ctrCharts[name] = charts;
    return charts;
}

function _updateCtrChart(name, cpuPct, memPct) {
    const charts = _ensureCtrCharts(name);
    if (!charts) return;
    const cpu = Math.min(Math.max(cpuPct, 0), 100);
    const mem = Math.min(Math.max(memPct, 0), 100);
    const cpuColor = _getCtrChartColor(cpu, true);
    const memColor = _getCtrChartColor(mem, false);

    charts.cpu.data.datasets[0].data = [cpu, 100 - cpu];
    charts.cpu.data.datasets[0].backgroundColor[0] = cpuColor;
    charts.cpu.update('none');

    charts.mem.data.datasets[0].data = [mem, 100 - mem];
    charts.mem.data.datasets[0].backgroundColor[0] = memColor;
    charts.mem.update('none');

    const cpuEl = document.getElementById('stat-cpu-' + name);
    const memEl = document.getElementById('stat-mem-' + name);
    if (cpuEl) { cpuEl.textContent = cpu + '%'; cpuEl.style.color = cpuColor; }
    if (memEl) { memEl.textContent = mem + '%'; memEl.style.color = memColor; }
}

// ── Smarter stats refresh ─────────────────────────────────────────────
const _lastStatsFetch = {};   // name -> performance.now() timestamp

async function loadCtrStats(name, force) {
    const now = performance.now();
    const last = _lastStatsFetch[name] || 0;
    const isContainersTab = currentTab === 'containers';
    const minInterval = isContainersTab ? 8000 : 30000;
    if (!force && (now - last) < minInterval) return;
    _lastStatsFetch[name] = now;
    try {
        const r = await fetch(API + `/api/container/${name}/stats`);
        const d = await r.json();
        if (d.error) return;

        // Cache for table view
        ctrStatsCache[name] = d;

        // Update Chart.js donuts + center text with color
        _updateCtrChart(name, d.cpu_pct, d.mem_pct);
    } catch(e) {}
}

// ── Container Table View ──────────────────────────────────────────────
function renderContainerTable() {
    const tbody = document.getElementById('ctr-table-body');
    if (!tbody) return;
    let ctrs = [...allContainers];
    if (ctrFilter !== 'all') ctrs = ctrs.filter(c => c.status === ctrFilter);
    if (!ctrs.length) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text3);padding:20px">No containers match filter</td></tr>';
        return;
    }
    // Sort by ctrTableSort
    const {col, dir} = ctrTableSort;
    ctrs.sort((a, b) => {
        let va, vb;
        if (col === 'cpu')    { va = (ctrStatsCache[a.name]||{}).cpu_pct||0;  vb = (ctrStatsCache[b.name]||{}).cpu_pct||0; }
        else if (col === 'mem') { va = (ctrStatsCache[a.name]||{}).mem_pct||0; vb = (ctrStatsCache[b.name]||{}).mem_pct||0; }
        else if (col === 'uptime') { va = a.uptime||''; vb = b.uptime||''; }
        else { va = (a[col]||'').toString(); vb = (b[col]||'').toString(); }
        if (va < vb) return dir === 'asc' ? -1 : 1;
        if (va > vb) return dir === 'asc' ? 1 : -1;
        return 0;
    });
    // Update sort arrow classes
    document.querySelectorAll('#ctr-table-wrap thead th').forEach(th => th.classList.remove('sorted'));
    tbody.innerHTML = ctrs.map(c => {
        const sc = statusClass(c.status);
        const isRunning = c.status === 'running';
        const stats = ctrStatsCache[c.name] || {};
        const cpuPct = stats.cpu_pct !== undefined ? stats.cpu_pct + '%' : '—';
        const memStr = stats.mem_usage_mb !== undefined ? stats.mem_usage_mb + ' MB' : '—';
        const ports = (c.ports||[]).slice(0,3).join(' ') || '—';
        const icon = ctrIcon(c.name);
        return `<tr>
          <td><span style="margin-right:6px">${icon}</span><b>${c.name}</b></td>
          <td style="font-family:var(--mono);font-size:11px;color:var(--text2)">${c.image}</td>
          <td><span class="ctr-status ${sc}" style="display:inline-flex;align-items:center;gap:4px"><span class="ctr-status-dot"></span>${c.status}</span></td>
          <td style="font-family:var(--mono)">${cpuPct}</td>
          <td style="font-family:var(--mono)">${memStr}</td>
          <td style="font-family:var(--mono);font-size:11px;color:var(--blue)">${ports}</td>
          <td style="font-family:var(--mono)">${c.uptime||'—'}</td>
          <td>
            <div style="display:flex;gap:4px;flex-wrap:wrap">
              ${isRunning
                ? `<button class="btn red" style="padding:2px 7px;font-size:11px" onclick="ctrAction('${c.name}','stop')">Stop</button>
                   <button class="btn orange" style="padding:2px 7px;font-size:11px" onclick="ctrAction('${c.name}','restart')">Restart</button>
                   <button class="btn purple" style="padding:2px 7px;font-size:11px" onclick="updateContainer('${c.name}')">⬆</button>`
                : `<button class="btn green" style="padding:2px 7px;font-size:11px" onclick="ctrAction('${c.name}','start')">Start</button>`}
              <button class="btn blue" style="padding:2px 7px;font-size:11px" onclick="openLogs('${c.name}')">Logs</button>
            </div>
          </td>
        </tr>`;
    }).join('');
    // Load stats for running ones (if not yet cached)
    ctrs.filter(c => c.status === 'running').forEach(c => {
        if (!ctrStatsCache[c.name]) loadCtrStats(c.name);
    });
}

function sortCtrTable(col) {
    if (ctrTableSort.col === col) {
        ctrTableSort.dir = ctrTableSort.dir === 'asc' ? 'desc' : 'asc';
    } else {
        ctrTableSort = {col, dir: 'asc'};
    }
    renderContainerTable();
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
// Storage pie chart registry
const _diskCharts = {};

async function loadStorage() {
    try {
        const r = await fetch(API + '/api/storage');
        const d = await r.json();
        const el = document.getElementById('disk-list');
        if (!d.filesystems || !d.filesystems.length) {
            el.innerHTML = '<div class="empty"><div class="empty-icon">💾</div><div class="empty-text">No filesystem data</div></div>';
            return;
        }
        d.filesystems.forEach(fs => {
            const pct = Math.round(fs.percent || 0);
            const mount = fs.mountpoint || fs.device;
            const safeId = 'disk-' + mount.replace(/[^a-zA-Z0-9]/g, '_');
            const clr = pct < 70 ? '#3fb950' : pct < 90 ? '#e3b341' : '#f85149';
            const clrFree = 'rgba(255,255,255,0.06)';

            // Create card if it doesn't exist
            if (!document.getElementById(safeId)) {
                const card = document.createElement('div');
                card.id = safeId;
                card.className = 'panel';
                card.style.cssText = 'display:flex;flex-direction:column;align-items:center;gap:8px;padding:16px;';
                card.innerHTML = `
                  <canvas id="c-${safeId}" width="130" height="130"></canvas>
                  <div style="font-size:12px;font-weight:600;color:var(--text);text-align:center;word-break:break-all">${mount}</div>
                  <div style="font-size:11px;color:var(--text3)">${fmtBytes(fs.used)} / ${fmtBytes(fs.total)}</div>
                  <div style="font-size:20px;font-weight:700;color:${clr}" id="pct-${safeId}">${pct}%</div>`;
                el.appendChild(card);
            } else {
                // Update percentage label color
                const pctEl = document.getElementById('pct-' + safeId);
                if (pctEl) { pctEl.textContent = pct + '%'; pctEl.style.color = clr; }
            }

            // Create or update chart
            const canvas = document.getElementById('c-' + safeId);
            if (canvas) {
                if (_diskCharts[safeId]) {
                    _diskCharts[safeId].data.datasets[0].data = [pct, 100 - pct];
                    _diskCharts[safeId].data.datasets[0].backgroundColor = [clr, clrFree];
                    _diskCharts[safeId].update('none');
                } else {
                    _diskCharts[safeId] = new Chart(canvas.getContext('2d'), {
                        type: 'doughnut',
                        data: { datasets: [{ data: [pct, 100 - pct], backgroundColor: [clr, clrFree], borderWidth: 0, hoverOffset: 0 }] },
                        options: {
                            cutout: '72%', responsive: false, animation: { duration: 600 },
                            plugins: { legend: { display: false }, tooltip: { enabled: false } }
                        }
                    });
                }
            }
        });
    } catch(e) {}
}

// ── Network (with bandwidth line charts) ─────────────────────────────
const _netHistory = {tx: [], rx: [], labels: [], maxPoints: 60};
const _netPrev = {tx: 0, rx: 0, time: 0};
let _netTxChart = null, _netRxChart = null;

function _netChartInit(canvasId, label, color) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return null;
    return new Chart(canvas.getContext('2d'), {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label, data: [], borderColor: color,
                backgroundColor: color.replace(')', ', 0.1)').replace('rgb', 'rgba'),
                borderWidth: 1.5, fill: true, tension: 0.4,
                pointRadius: 0, pointHoverRadius: 3
            }]
        },
        options: {
            responsive: true, maintainAspectRatio: false, animation: { duration: 0 },
            scales: {
                x: { display: false },
                y: {
                    display: true, beginAtZero: true, min: 0,
                    ticks: { color: 'rgba(139,148,158,.7)', font: { size: 10 }, maxTicksLimit: 4,
                             callback: v => fmtBytes(v) + '/s' },
                    grid: { color: 'rgba(48,54,61,.6)' }
                }
            },
            plugins: { legend: { display: false }, tooltip: {
                callbacks: { label: ctx => fmtBytes(ctx.raw) + '/s' }
            }}
        }
    });
}

async function loadNetwork() {
    try {
        const r = await fetch(API + '/api/network');
        const d = await r.json();
        const tbody = document.getElementById('net-table');
        const ifaces = d.interfaces || [];

        // Aggregate total TX/RX across all physical interfaces
        let totalTx = 0, totalRx = 0;
        ifaces.forEach(i => { totalTx += i.bytes_sent || 0; totalRx += i.bytes_recv || 0; });

        const now = Date.now();
        let txRate = 0, rxRate = 0;
        if (_netPrev.time && (now - _netPrev.time) < 30000) {
            const dt = (now - _netPrev.time) / 1000;
            txRate = Math.max(0, (totalTx - _netPrev.tx) / dt);
            rxRate = Math.max(0, (totalRx - _netPrev.rx) / dt);
        }
        _netPrev.tx = totalTx; _netPrev.rx = totalRx; _netPrev.time = now;

        // Rolling history
        const label = new Date().toLocaleTimeString('en-US', {hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'});
        _netHistory.tx.push(txRate);
        _netHistory.rx.push(rxRate);
        _netHistory.labels.push(label);
        if (_netHistory.tx.length > _netHistory.maxPoints) {
            _netHistory.tx.shift(); _netHistory.rx.shift(); _netHistory.labels.shift();
        }

        // Init charts on first call
        if (!_netTxChart) _netTxChart = _netChartInit('net-tx-chart', 'TX', 'rgb(56,139,253)');
        if (!_netRxChart) _netRxChart = _netChartInit('net-rx-chart', 'RX', 'rgb(63,185,80)');

        if (_netTxChart) {
            _netTxChart.data.labels = [..._netHistory.labels];
            _netTxChart.data.datasets[0].data = [..._netHistory.tx];
            _netTxChart.update('none');
        }
        if (_netRxChart) {
            _netRxChart.data.labels = [..._netHistory.labels];
            _netRxChart.data.datasets[0].data = [..._netHistory.rx];
            _netRxChart.update('none');
        }

        tbody.innerHTML = ifaces.map(i => {
            const sc = i.is_up ? 'running' : 'exited';
            return `<tr>
              <td><b>${i.name}</b></td>
              <td style="font-family:var(--mono);font-size:12px">${(i.addresses||[]).join(', ')||'—'}</td>
              <td>${fmtBytes(i.bytes_sent)}</td>
              <td>${fmtBytes(i.bytes_recv)}</td>
              <td style="font-family:var(--mono);font-size:11px;color:var(--text2)">—/—</td>
              <td><span class="ctr-status ${sc}" style="display:inline-flex;align-items:center;gap:4px"><span class="ctr-status-dot"></span>${i.is_up?'Up':'Down'}</span></td>
            </tr>`;
        }).join('') || '<tr><td colspan="6" style="text-align:center;color:var(--text3)">No interfaces</td></tr>';
    } catch(e) {}
}

// ── Port Map (accordion by container) ────────────────────────────────
async function loadPortMap() {
    const accordion = document.getElementById('port-accordion');
    const summary = document.getElementById('port-summary');
    accordion.innerHTML = '<div class="empty"><div class="empty-icon">⏳</div><div class="empty-text">Loading port assignments...</div></div>';
    try {
        const r = await fetch(API + '/api/ports/map');
        const d = await r.json();
        const ports = d.ports || [];

        // Summary cards
        const activePorts = ports.filter(p => p.status === 'running' && p.host_port);
        const stoppedPorts = ports.filter(p => p.status !== 'running' && p.host_port);
        const unbound = ports.filter(p => !p.host_port);
        summary.innerHTML = `
            <div class="stat-card" style="flex:1;min-width:120px"><div class="stat-card-val" style="color:var(--green)">${activePorts.length}</div><div class="stat-card-label">Active Ports</div></div>
            <div class="stat-card" style="flex:1;min-width:120px"><div class="stat-card-val" style="color:var(--orange)">${stoppedPorts.length}</div><div class="stat-card-label">Stopped</div></div>
            <div class="stat-card" style="flex:1;min-width:120px"><div class="stat-card-val" style="color:var(--text3)">${unbound.length}</div><div class="stat-card-label">Unbound</div></div>
            <div class="stat-card" style="flex:1;min-width:120px"><div class="stat-card-val" style="color:var(--blue)">${d.total_bindings || 0}</div><div class="stat-card-label">Total Bindings</div></div>`;

        if (!ports.length) {
            accordion.innerHTML = '<div class="empty"><div class="empty-icon">🔌</div><div class="empty-text">No port assignments found</div></div>';
            return;
        }

        // Group by container
        const grouped = {};
        ports.forEach(p => {
            if (!grouped[p.container]) grouped[p.container] = {status: p.status, ports: []};
            grouped[p.container].ports.push(p);
        });

        accordion.innerHTML = Object.entries(grouped).map(([name, g]) => {
            const sc = g.status === 'running' ? 'running' : 'exited';
            const icon = ctrIcon(name);
            const portCount = g.ports.length;
            // Collapse groups with many ports or stopped containers by default
            const startCollapsed = portCount > 3 || g.status !== 'running' ? 'collapsed' : '';
            const rows = g.ports.map(p => {
                const hostPort = p.host_port || '—';
                const isWeb = p.host_port && !String(p.container_port).includes('/udp');
                const link = isWeb
                    ? `<a href="#" onclick="openExternalLink('http://localhost:${p.host_port}');return false" style="color:var(--blue);text-decoration:none;font-size:12px">:${p.host_port} ↗</a>`
                    : '<span style="color:var(--text3);font-size:11px">—</span>';
                return `<tr style="border-bottom:1px solid var(--border)">
                    <td style="padding:8px 14px;font-family:var(--mono);font-size:13px;font-weight:700;color:var(--green)">${hostPort}</td>
                    <td style="padding:8px 14px;font-family:var(--mono);font-size:12px;color:var(--text2)">${p.container_port}</td>
                    <td style="padding:8px 14px">${link}</td>
                </tr>`;
            }).join('');
            return `<div class="pm-group ${startCollapsed}" id="pmg-${name}">
              <div class="pm-group-hdr" onclick="pmToggle('${name}')">
                <span style="margin-right:4px">${icon}</span>
                <b style="font-size:13px">${name}</b>
                <span class="ctr-status ${sc}" style="display:inline-flex;align-items:center;gap:3px;padding:2px 7px;font-size:11px;margin-left:6px"><span class="ctr-status-dot"></span>${g.status}</span>
                <span style="font-size:11px;color:var(--text3);margin-left:8px">${portCount} port${portCount!==1?'s':''}</span>
                <span class="pm-group-chevron">▼</span>
              </div>
              <div class="pm-group-body">
                <table style="width:100%;border-collapse:collapse">
                  <thead><tr style="background:var(--surface)"><th style="padding:6px 14px;text-align:left;font-size:11px;color:var(--blue)">Host Port</th><th style="padding:6px 14px;text-align:left;font-size:11px;color:var(--blue)">Container Port</th><th style="padding:6px 14px;text-align:left;font-size:11px;color:var(--blue)">Open</th></tr></thead>
                  <tbody>${rows}</tbody>
                </table>
              </div>
            </div>`;
        }).join('');
    } catch(e) {
        accordion.innerHTML = '<div class="empty"><div class="empty-icon">⚠️</div><div class="empty-text">Failed to load port map</div></div>';
    }
}
function pmToggle(name) {
    document.getElementById('pmg-' + name)?.classList.toggle('collapsed');
}
function pmExpandAll() {
    document.querySelectorAll('.pm-group').forEach(g => g.classList.remove('collapsed'));
}
function pmCollapseAll() {
    document.querySelectorAll('.pm-group').forEach(g => g.classList.add('collapsed'));
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

// ══════════════════════════════════════════════════════════════════════
// 8. FAVORITES SYSTEM (localStorage)
// ══════════════════════════════════════════════════════════════════════
function getFavorites() {
    try { return JSON.parse(localStorage.getItem('arrhub_favorites') || '[]'); } catch(e) { return []; }
}
function saveFavorites(ids) {
    try { localStorage.setItem('arrhub_favorites', JSON.stringify(ids)); } catch(e) {}
}
function toggleFavorite(appId) {
    let favs = getFavorites();
    if (favs.includes(appId)) {
        favs = favs.filter(id => id !== appId);
    } else {
        favs.push(appId);
    }
    saveFavorites(favs);
    renderCatalog(); // re-render to update star and fav section
}
function isFavorite(appId) { return getFavorites().includes(appId); }

// ── Catalog ───────────────────────────────────────────────────────────
async function loadCatalog() {
    showSkeleton('cat-grid', 6);
    try {
        const r = await fetch(API + '/api/catalog');
        const d = await r.json();
        allCatalog = d.apps || [];
        // Build category pills — include "Favorites" pill
        const cats = ['All', 'Favorites', ...new Set(allCatalog.map(a=>a.category).filter(Boolean))].sort((a,b)=>{
            // Pin All/Favorites to front
            if (a==='All') return -1; if (b==='All') return 1;
            if (a==='Favorites') return -1; if (b==='Favorites') return 1;
            return a.localeCompare(b);
        });
        const catEl = document.getElementById('cat-categories');
        catEl.innerHTML = cats.map(c=>`<div class="filter-pill${c==='All'?' active':''}" onclick="filterCat('${c}',this)">${c==='Favorites'?'⭐ ':''  }${c}</div>`).join('');
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

// Real-time search filter (called by oninput)
function filterCatalog() { renderCatalog(); }

function _buildAppCard(a, showFavStar=true) {
    const starred = isFavorite(a.id);
    return `
      <div class="cat-card">
        <div class="cat-card-header">
          <div class="cat-icon">${a.icon||'📦'}</div>
          <div style="flex:1;min-width:0"><div class="cat-name">${a.name}</div><div class="cat-cat">${a.category}</div></div>
          ${showFavStar ? `<button class="fav-btn${starred?' starred':''}" onclick="toggleFavorite('${a.id}')" title="${starred?'Remove from favorites':'Add to favorites'}">${starred?'⭐':'☆'}</button>` : ''}
        </div>
        <div class="cat-desc">${a.description||''}</div>
        <div class="cat-footer">
          <div class="cat-image">${(a.image||'').split(':')[0].split('/').pop()}</div>
          <button class="btn blue" onclick="deployApp('${a.id}','${a.name}')">Deploy</button>
        </div>
      </div>`;
}

function renderCatalog() {
    const search = (document.getElementById('cat-search')?.value || '').toLowerCase();
    const sortVal = document.getElementById('cat-sort')?.value || 'az';
    const favs = getFavorites();

    // Render favorites section at top
    const favApps = allCatalog.filter(a => favs.includes(a.id));
    const favSection = document.getElementById('fav-section');
    const favGrid = document.getElementById('fav-grid');
    if (favSection && favGrid) {
        if (favApps.length > 0) {
            favSection.style.display = '';
            favGrid.innerHTML = favApps.map(a => _buildAppCard(a, true)).join('');
        } else {
            favSection.style.display = 'none';
        }
    }

    let apps = allCatalog;

    // Apply category filter
    if (catFilter === 'Favorites') {
        apps = apps.filter(a => favs.includes(a.id));
    } else if (catFilter !== 'All') {
        apps = apps.filter(a => a.category === catFilter);
    }

    // Apply search
    if (search) apps = apps.filter(a => (a.name + a.description + a.category + a.id).toLowerCase().includes(search));

    // Apply sort
    if (sortVal === 'az') apps = [...apps].sort((a,b) => a.name.localeCompare(b.name));
    else if (sortVal === 'za') apps = [...apps].sort((a,b) => b.name.localeCompare(a.name));
    else if (sortVal === 'cat') apps = [...apps].sort((a,b) => (a.category||'').localeCompare(b.category||'') || a.name.localeCompare(b.name));

    const grid = document.getElementById('cat-grid');
    const countEl = document.getElementById('cat-count');

    // Update count badge
    if (countEl) countEl.textContent = `Showing ${apps.length} of ${allCatalog.length} apps`;

    if (!apps.length) {
        grid.innerHTML = `<div class="empty" style="grid-column:1/-1">
          <div class="empty-icon">🔍</div>
          <div class="empty-text">No apps match your search</div>
          <div style="font-size:12px;color:var(--text3);margin-top:4px">Try adjusting your filters or search term</div>
        </div>`;
        return;
    }
    grid.innerHTML = apps.map(a => _buildAppCard(a, true)).join('');
}

async function deployApp(id, name) {
    if (!confirm('Deploy ' + name + '?')) return;
    showToast('Deploying ' + name + '…', 'info', 10000);
    try {
        const r = await fetch(API + '/api/deploy', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({app_id:id})});
        const d = await r.json();
        if (d.error) {
            showToast('Deploy failed: ' + d.error, 'error');
        } else {
            showToast(name + ' deployed successfully!', 'success', 5000);
            setTimeout(loadContainers, 2000);
        }
    } catch(e) { showToast('Deploy request failed', 'error'); }
}

// ── Stack Manager ─────────────────────────────────────────────────────
async function loadStackManager() {
    showSkeleton('stacks-list', 3);
    try {
        const r = await fetch(API + '/api/stacks');
        const d = await r.json();
        const el = document.getElementById('stacks-list');
        const stacks = d.stacks || [];
        if (!stacks.length) {
            el.innerHTML = '<div class="empty"><div class="empty-icon">📋</div><div class="empty-text">No compose stacks found in /docker/<br><span style="font-size:11px;color:var(--text3)">Deploy apps to create stacks</span></div></div>';
            return;
        }
        el.innerHTML = stacks.map(s => {
            const isRunning = s.status === 'running' || (s.running_containers && s.running_containers > 0);
            const sc = isRunning ? 'running' : 'exited';
            const svcInfo = s.services ? `${s.services} services` : '—';
            const runInfo = s.running_containers !== undefined ? `${s.running_containers} running` : '';
            return `<div class="stack-card" id="stack-${s.name}">
              <div class="stack-card-hdr">
                <span style="font-size:18px">📋</span>
                <div style="flex:1">
                  <div class="stack-card-name">${s.name}</div>
                  <div style="font-size:11px;color:var(--text3);font-family:var(--mono)">${s.path}</div>
                </div>
                <span class="ctr-status ${sc}" style="display:inline-flex;align-items:center;gap:4px;padding:3px 9px;font-size:11px">
                  <span class="ctr-status-dot"></span>${isRunning ? 'Running' : 'Stopped'}
                </span>
              </div>
              <div style="font-size:12px;color:var(--text2);margin-bottom:10px">${svcInfo}${runInfo ? ' · ' + runInfo : ''}</div>
              <div class="stack-card-actions">
                <button class="btn green" style="padding:4px 10px;font-size:12px" onclick="stackAction('${s.name}','up')">
                  <svg width="11" height="11" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg> Up
                </button>
                <button class="btn red" style="padding:4px 10px;font-size:12px" onclick="stackAction('${s.name}','down')">
                  <svg width="11" height="11" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg> Down
                </button>
                <button class="btn orange" style="padding:4px 10px;font-size:12px" onclick="stackRestart('${s.name}')">
                  <svg width="11" height="11" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg> Restart
                </button>
                <button class="btn purple" style="padding:4px 10px;font-size:12px" onclick="stackPull('${s.name}')">
                  <svg width="11" height="11" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/></svg> Pull
                </button>
              </div>
            </div>`;
        }).join('');
    } catch(e) { document.getElementById('stacks-list').innerHTML = '<div class="empty"><div class="empty-icon">⚠️</div><div class="empty-text">Failed to load stacks</div></div>'; }
}

async function stackRestart(name) {
    showToast('Restarting stack ' + name + '…', 'info', 3000);
    try {
        await fetch(API + `/api/stack/${name}/down`, {method:'POST'});
        await new Promise(r => setTimeout(r, 1500));
        const r = await fetch(API + `/api/stack/${name}/up`, {method:'POST'});
        const d = await r.json();
        if (d.error) showToast('Restart error: ' + d.error, 'error');
        else { showToast('Stack ' + name + ' restarted', 'success'); setTimeout(loadStackManager, 1000); }
    } catch(e) { showToast('Request failed', 'error'); }
}

async function stackPull(name) {
    showToast('Pulling latest images for ' + name + '…', 'info', 8000);
    try {
        const r = await fetch(API + `/api/stack/${name}/pull`, {method:'POST'});
        const d = await r.json();
        if (d.error) showToast('Pull error: ' + d.error, 'error');
        else showToast('✓ ' + name + ' images pulled — run Up to apply', 'success', 6000);
    } catch(e) { showToast('Pull request failed', 'error'); }
}

async function loadDeployHistory() {
    try {
        const r = await fetch(API + '/api/deploy/history');
        const d = await r.json();
        const el = document.getElementById('deploy-history');
        const h = d.history || [];
        if (!h.length) { el.innerHTML = '<div style="padding:12px;color:var(--text3)">No deploys yet</div>'; return; }
        el.innerHTML = '<table><thead><tr><th>Time</th><th>App</th><th>Action</th><th>Status</th></tr></thead><tbody>' +
          h.map(e=>`<tr><td style="font-size:12px">${e.timestamp||'—'}</td><td>${e.app_name||e.app_id||'—'}</td><td>${e.action||'—'}</td><td><span class="ctr-status ${e.status==='success'?'running':'exited'}" style="display:inline-flex;align-items:center;gap:4px"><span class="ctr-status-dot"></span>${e.status}</span></td></tr>`).join('') +
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
        el.innerHTML = b.map(x=>`<div class="ctr-row"><span>${x.name}</span><span>${fmtBytes(x.size)}</span></div>`).join('');
    } catch(e) {}
}

async function createBackup() {
    showToast('Creating backup…', 'info', 10000);
    try {
        const r = await fetch(API + '/api/backup', {method:'POST'});
        const d = await r.json();
        if (d.error) showToast('Backup error: ' + d.error, 'error');
        else showToast('Backup created: ' + (d.file || d.path || 'done'), 'success', 5000);
        loadBackups();
    } catch(e) { showToast('Backup failed', 'error'); }
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
        // Service integration fields
        setInput('svc-radarr-url',  s.radarr_url);
        setInput('svc-radarr-key',  s.radarr_api_key);
        setInput('svc-sonarr-url',  s.sonarr_url);
        setInput('svc-sonarr-key',  s.sonarr_api_key);
        setInput('svc-plex-url',    s.plex_url);
        setInput('svc-plex-token',  s.plex_token);
        setInput('svc-seerr-url',   s.seerr_url);
        setInput('svc-seerr-key',   s.seerr_api_key);
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
        if (d.error) showToast('Error: ' + d.error, 'error');
        else showToast('Settings saved!', 'success');
    } catch(e) { showToast('Save failed', 'error'); }
}

// ── Weather & Docker Info ─────────────────────────────────────────────
const WEATHER_ICONS = {'113':'☀️','116':'⛅','119':'☁️','122':'☁️','143':'🌫️','176':'🌦️','179':'🌨️','182':'🌧️','185':'🌧️','200':'⛈️','227':'🌨️','230':'❄️','248':'🌫️','260':'🌫️','263':'🌦️','266':'🌧️','281':'🌧️','284':'🌧️','293':'🌦️','296':'🌧️','299':'🌧️','302':'🌧️','305':'🌧️','308':'🌧️','311':'🌧️','314':'🌧️','317':'🌧️','320':'🌨️','323':'🌨️','326':'🌨️','329':'❄️','332':'❄️','335':'❄️','338':'❄️','350':'🌧️','353':'🌦️','356':'🌧️','359':'🌧️','362':'🌧️','365':'🌧️','368':'🌨️','371':'❄️','374':'🌧️','377':'🌧️','386':'⛈️','389':'⛈️','392':'⛈️','395':'❄️'};

async function loadWeather() {
    try {
        const r = await fetch(API + '/api/weather');
        const d = await r.json();
        if (d.error) return;
        if (!d.daily || d.daily.length === 0) return;
        const w = d.daily[0];   // today

        // Current conditions (from open-meteo /current)
        const temp = w.temp_max !== undefined ? Math.round(w.temp_max) : '—';
        setEl('weather-temp', temp + '°C');
        setEl('weather-desc', w.desc || 'Clear');
        setEl('weather-location', d.location || '');
        setEl('weather-humidity', d.humidity != null ? d.humidity + '%' : '—');
        setEl('weather-wind', d.wind_mph != null ? d.wind_mph + ' mph' : '—');
        setEl('weather-feels', d.feels_like != null ? Math.round(d.feels_like) + '°C' : '—');
        const iconEl = document.getElementById('weather-icon');
        if (iconEl && w.icon) iconEl.textContent = w.icon;

        // 5-day forecast strip
        const forecastEl = document.getElementById('weather-forecast');
        if (forecastEl && d.daily) {
            forecastEl.innerHTML = d.daily.map((day, i) => {
                const label = i === 0 ? 'Today' : new Date(day.date + 'T12:00').toLocaleDateString(undefined, {weekday:'short'});
                const hi = day.temp_max != null ? Math.round(day.temp_max) + '°' : '—';
                const lo = day.temp_min != null ? Math.round(day.temp_min) + '°' : '—';
                return `<div style="flex:1;min-width:56px;text-align:center;background:var(--surface);border-radius:var(--r);padding:6px 4px;">
                  <div style="font-size:10px;color:var(--text3);margin-bottom:2px">${label}</div>
                  <div style="font-size:20px;line-height:1.2">${day.icon || '🌤️'}</div>
                  <div style="font-size:11px;font-weight:600;color:var(--text)">${hi}</div>
                  <div style="font-size:10px;color:var(--text3)">${lo}</div>
                </div>`;
            }).join('');
        }
    } catch(e) {}
}

async function loadDockerInfo() {
    try {
        const r = await fetch(API + '/api/docker/info');
        const d = await r.json();
        if (d.error) return;
        setEl('docker-images', d.images || '—');
        setEl('docker-volumes', d.volumes || '—');
        setEl('docker-networks', d.networks || '—');
        setEl('docker-disk', d.disk_usage || '—');
    } catch(e) {}
}

// ── RSS Feeds ─────────────────────────────────────────────────────────
let rssCatFilter = 'All';
let rssView = 'feeds';

function setRSSView(v) {
    rssView = v;
    document.getElementById('rss-feeds-view').style.display = v === 'feeds' ? '' : 'none';
    document.getElementById('rss-live-view').style.display  = v === 'live'  ? '' : 'none';
    document.getElementById('rss-iptv-view').style.display  = v === 'iptv'  ? '' : 'none';
    document.querySelectorAll('#rss-view-feeds,#rss-view-live,#rss-view-iptv').forEach(b => b.classList.remove('active'));
    document.getElementById('rss-view-' + v).classList.add('active');
    // Hide the expand/collapse controls on non-feed views
    const ctl = document.getElementById('rss-all-controls');
    if (ctl) ctl.style.display = v === 'feeds' ? '' : 'none';
}

async function loadRSSFeeds() {
    const content = document.getElementById('rss-content');
    if (content) content.innerHTML = '<div style="color:var(--text3);padding:20px">Loading feeds…</div>';
    try {
        const r = await fetch(API + '/api/rss/feeds');
        const d = await r.json();
        const cats = d.categories || {};

        // Build category pills
        const pillsEl = document.getElementById('rss-cat-pills');
        if (pillsEl) {
            const allCats = ['All', ...Object.keys(cats)];
            pillsEl.innerHTML = allCats.map(c =>
                `<div class="filter-pill${c===rssCatFilter?' active':''}" onclick="rssSetCat('${c}')">${c}</div>`
            ).join('');
        }
        renderRSSFeeds(cats);
    } catch(e) {
        if (content) content.innerHTML = '<div style="color:var(--red);padding:20px">Failed to load feeds</div>';
    }
}

function rssSetCat(cat) {
    rssCatFilter = cat;
    document.querySelectorAll('#rss-cat-pills .filter-pill').forEach(p => {
        p.classList.toggle('active', p.textContent === cat);
    });
    loadRSSFeeds();
}

async function renderRSSFeeds(cats) {
    const content = document.getElementById('rss-content');
    if (!content) return;

    const isAll = rssCatFilter === 'All';
    const toLoad = isAll
        ? Object.entries(cats)
        : Object.entries(cats).filter(([k]) => k === rssCatFilter);

    // Show/hide Expand All / Collapse All controls
    const allCtrl = document.getElementById('rss-all-controls');
    if (allCtrl) allCtrl.classList.toggle('visible', isAll);

    // Each category: collapsible in "All" view, always open in single-cat view
    content.innerHTML = toLoad.map(([cat, feeds]) => {
        const catId = cat.replace(/\s/g,'_');
        return `
        <div class="panel rss-col${isAll ? ' collapsed' : ''}" id="rss-col-${catId}">
          <div class="panel-title rss-col-hdr" onclick="toggleRssCol('${catId}')">
            <span style="font-size:13px;font-weight:700">${feeds[0]&&feeds[0].icon ? feeds[0].icon : '📰'} ${cat}</span>
            <span class="rss-col-chevron" id="rss-chev-${catId}">
              <span class="rss-col-count" id="rss-count-${catId}">${feeds.length} source${feeds.length!==1?'s':''}</span>
              ${isAll ? '▶' : '▼'}
            </span>
          </div>
          <div class="rss-feed-tabs" style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:10px;margin-top:6px">
            ${feeds.map((f,i) => `<button class="filter-pill${i===0?' active':''}" style="font-size:10px;padding:2px 8px"
                onclick="rssTabClick('${catId}','${encodeURIComponent(f.url)}','${encodeURIComponent(f.name)}',this)">${f.icon} ${f.name}</button>`).join('')}
          </div>
          <div class="rss-items" id="rss-items-${catId}">
            <div class="skeleton" style="height:80px;border-radius:var(--r);margin-bottom:6px"></div>
            <div class="skeleton" style="height:80px;border-radius:var(--r);margin-bottom:6px"></div>
            <div class="skeleton" style="height:80px;border-radius:var(--r)"></div>
          </div>
        </div>`;
    }).join('');

    // Load first feed for each category in parallel
    await Promise.all(toLoad.map(([cat, feeds]) => {
        if (feeds.length > 0)
            return loadFeedItems(cat.replace(/\s/g,'_'), encodeURIComponent(feeds[0].url), encodeURIComponent(feeds[0].name), null);
    }));
}

// Toggle a single category column open/closed
function toggleRssCol(catId) {
    const col  = document.getElementById('rss-col-' + catId);
    const chev = document.getElementById('rss-chev-' + catId);
    if (!col) return;
    const nowCollapsed = col.classList.toggle('collapsed');
    if (chev) {
        const countEl = document.getElementById('rss-count-' + catId);
        const countTxt = countEl ? countEl.outerHTML : '';
        chev.innerHTML = countTxt + (nowCollapsed ? ' ▶' : ' ▼');
    }
}

// Source-tab click: auto-expands the column then loads items
function rssTabClick(catId, encodedUrl, encodedName, btnEl) {
    const col = document.getElementById('rss-col-' + catId);
    if (col && col.classList.contains('collapsed')) toggleRssCol(catId);
    loadFeedItems(catId, encodedUrl, encodedName, btnEl);
}

// Expand / collapse all columns at once
function rssExpandAll() {
    document.querySelectorAll('#rss-content .rss-col').forEach(col => {
        const catId = col.id.replace('rss-col-', '');
        if (col.classList.contains('collapsed')) toggleRssCol(catId);
    });
}
function rssCollapseAll() {
    document.querySelectorAll('#rss-content .rss-col').forEach(col => {
        const catId = col.id.replace('rss-col-', '');
        if (!col.classList.contains('collapsed')) toggleRssCol(catId);
    });
}

async function loadFeedItems(catId, encodedUrl, encodedName, btnEl) {
    // Highlight the selected source tab
    const col = document.getElementById('rss-col-' + catId);
    if (col && btnEl) {
        col.querySelectorAll('.rss-feed-tabs .filter-pill').forEach(b => b.classList.remove('active'));
        btnEl.classList.add('active');
    }

    const itemsEl = document.getElementById('rss-items-' + catId);
    if (!itemsEl) return;
    // Skeleton while fetching
    itemsEl.innerHTML = '<div class="skeleton" style="height:80px;border-radius:var(--r);margin-bottom:6px"></div>'.repeat(3);

    try {
        const url = decodeURIComponent(encodedUrl);
        const r = await fetch(API + `/api/rss/fetch?url=${encodeURIComponent(url)}`);
        const d = await r.json();
        const items = d.items || [];

        if (!items.length) {
            itemsEl.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:8px;text-align:center">No items found</div>';
            _rssUpdateCount(catId, 0);
            return;
        }

        // Rich card layout: thumbnail left, title+excerpt+date right
        itemsEl.innerHTML = items.slice(0, 12).map(item => `
            <a href="${item.link}" target="_blank" rel="noopener"
               style="display:flex;gap:10px;text-decoration:none;padding:8px 4px;border-bottom:1px solid var(--border);transition:background .12s ease;"
               onmouseover="this.style.background='var(--surface)'" onmouseout="this.style.background=''">
              ${item.thumb
                ? `<img src="${item.thumb}" loading="lazy" style="width:72px;height:52px;object-fit:cover;border-radius:5px;flex-shrink:0;background:var(--surface2)"
                        onerror="this.style.display='none'">`
                : `<div style="width:72px;height:52px;flex-shrink:0;background:var(--surface2);border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:20px;color:var(--text3)">📰</div>`}
              <div style="flex:1;min-width:0;overflow:hidden">
                <div style="font-size:12px;font-weight:600;color:var(--text);line-height:1.35;margin-bottom:3px;
                     display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${item.title}</div>
                ${item.excerpt
                    ? `<div style="font-size:11px;color:var(--text2);line-height:1.3;margin-bottom:3px;
                            display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${item.excerpt}</div>`
                    : ''}
                <div style="font-size:10px;color:var(--text3)">${item.date || ''}</div>
              </div>
            </a>`).join('');

        // Update chevron badge with article count
        _rssUpdateCount(catId, items.length);
    } catch(e) {
        itemsEl.innerHTML = '<div style="color:var(--red);font-size:11px;padding:8px">Failed to load feed</div>';
    }
}

// Update the item-count badge in the column chevron
function _rssUpdateCount(catId, n) {
    const countEl = document.getElementById('rss-count-' + catId);
    if (!countEl) return;
    countEl.textContent = n > 0 ? `${n} article${n!==1?'s':''}` : 'no articles';
}

// ══════════════════════════════════════════════════════════════════════
// 10. ALERTS BAR
// ══════════════════════════════════════════════════════════════════════
let _alertsCollapsed = false;

function toggleAlerts() {
    _alertsCollapsed = !_alertsCollapsed;
    const body = document.getElementById('alerts-body');
    const chev = document.getElementById('alerts-chevron');
    if (body) body.classList.toggle('collapsed', _alertsCollapsed);
    if (chev) chev.textContent = _alertsCollapsed ? '▶' : '▼';
}

async function refreshAlerts() {
    const body = document.getElementById('alerts-body');
    const title = document.getElementById('alerts-title');
    if (!body) return;
    try {
        const alerts = [];

        // Check containers for non-running state
        if (allContainers.length) {
            allContainers.filter(c => c.status !== 'running').forEach(c => {
                alerts.push({level:'red', msg:`Container <b>${c.name}</b> is ${c.status}`});
            });
        }

        // Check disk usage from storage API
        try {
            const r = await fetch(API + '/api/storage');
            const d = await r.json();
            (d.filesystems || []).forEach(fs => {
                if (fs.percent > 85) {
                    alerts.push({level:'yellow', msg:`Disk usage high: <b>${fs.mountpoint}</b> at ${fs.percent}%`});
                }
            });
        } catch(e) {}

        // Update title
        if (title) {
            if (alerts.length === 0) {
                title.textContent = '🟢 All systems nominal';
                title.style.color = 'var(--green)';
            } else {
                const hasRed = alerts.some(a => a.level === 'red');
                title.textContent = (hasRed ? '🔴 ' : '🟡 ') + alerts.length + ' alert' + (alerts.length > 1 ? 's' : '');
                title.style.color = hasRed ? 'var(--red)' : 'var(--yellow)';
            }
        }

        if (alerts.length === 0) {
            body.innerHTML = '<div class="alert-row"><span>🟢</span><span>All systems nominal</span></div>';
        } else {
            body.innerHTML = alerts.map(a => {
                const icon = a.level === 'red' ? '🔴' : a.level === 'yellow' ? '🟡' : '🟢';
                return `<div class="alert-row"><span>${icon}</span><span>${a.msg}</span></div>`;
            }).join('');
        }
    } catch(e) {}
}

// Refresh alerts every 30 seconds
setInterval(refreshAlerts, 30000);

// ── Overview Extras ───────────────────────────────────────────────────
async function loadOverviewExtras() {
    // Recent logs excerpt
    try {
        const r = await fetch(API + '/api/logs?lines=8');
        const d = await r.json();
        const el = document.getElementById('ov-log-excerpt');
        if (el) el.textContent = (d.lines || []).slice(-8).join('\n') || '(no logs)';
    } catch(e) {}
    // Network I/O
    try {
        const r = await fetch(API + '/api/network');
        const d = await r.json();
        const io = d.io || {};
        setEl('ov-net-sent', fmtBytes(io.bytes_sent || 0));
        setEl('ov-net-recv', fmtBytes(io.bytes_recv || 0));
    } catch(e) {}
    // Dashboard containers panel
    loadDashboardContainers();
    // Service integration cards
    loadRadarrCard();
    loadSonarrCard();
    loadPlexCard();
    loadSeerrCard();
}

// ── Service Card Loaders ───────────────────────────────────────────────
// Each checks if the service is configured; if not, shows a "configure" prompt.
// Radarr/Sonarr support tabs: Upcoming | Queue | Library

function _svcUnconfigured(bodyId, svcName) {
    const el = document.getElementById(bodyId);
    if (el) el.innerHTML = `<div style="text-align:center;padding:12px 8px;color:var(--text3);font-size:12px">
      <div style="font-size:20px;margin-bottom:4px">⚙️</div>
      ${svcName} not configured — <a href="#" style="color:var(--blue)" onclick="showTab('settings',null);return false">add API key in Settings</a>
    </div>`;
}

function _svcError(bodyId, msg) {
    const el = document.getElementById(bodyId);
    if (el) el.innerHTML = `<div style="color:var(--text3);font-size:11px;padding:8px;text-align:center">⚠ ${msg}</div>`;
}

function _svcEmpty(bodyId, msg) {
    const el = document.getElementById(bodyId);
    if (el) el.innerHTML = `<div style="color:var(--text3);font-size:12px;padding:8px;text-align:center">${msg}</div>`;
}

// Helper: format bytes to human-readable
function _fmtSize(bytes) {
    if (!bytes) return '—';
    const gb = bytes / (1024*1024*1024);
    if (gb >= 1) return gb.toFixed(1) + ' GB';
    const mb = bytes / (1024*1024);
    return mb.toFixed(0) + ' MB';
}

// ── Tab switch handler ──
const _svcActiveTab = { radarr: 'upcoming', sonarr: 'upcoming' };

function svcTabSwitch(svc, tab, btnEl) {
    _svcActiveTab[svc] = tab;
    // Update active tab button
    const card = document.getElementById(svc + '-card');
    if (card) card.querySelectorAll('.svc-tab').forEach(b => b.classList.toggle('active', b === btnEl));
    // Load the right data
    if (svc === 'radarr') {
        if (tab === 'upcoming') loadRadarrCard();
        else if (tab === 'queue') loadRadarrQueue();
        else loadRadarrLibrary();
    } else if (svc === 'sonarr') {
        if (tab === 'upcoming') loadSonarrCard();
        else if (tab === 'queue') loadSonarrQueue();
        else loadSonarrLibrary();
    }
}

// ── Toggle inline detail on click ──
function _toggleDetail(detailId) {
    const d = document.getElementById(detailId);
    if (d) d.classList.toggle('open');
}

// ── Poster helper ──
function _posterImg(url) {
    return url
        ? `<img src="${url}" style="width:28px;height:40px;border-radius:3px;object-fit:cover;flex-shrink:0" loading="lazy" onerror="this.style.display='none'">`
        : '<div style="width:28px;height:40px;background:var(--surface2);border-radius:3px;flex-shrink:0"></div>';
}

// ══════════════════════════════════════════════════════════════════
// RADARR — Upcoming / Queue / Library
// ══════════════════════════════════════════════════════════════════
async function loadRadarrCard() {
    try {
        const r = await fetch(API + '/api/services/radarr/calendar');
        const d = await r.json();
        if (!d.configured) return _svcUnconfigured('radarr-card-body', 'Radarr');
        if (d.error) return _svcError('radarr-card-body', d.error);
        const el = document.getElementById('radarr-card-body');
        if (!el) return;
        if (!d.movies || !d.movies.length) return _svcEmpty('radarr-card-body', 'No upcoming releases (14 days)');
        el.innerHTML = d.movies.map((m, i) => {
            const did = 'rd-d-' + i;
            return `<div class="svc-item" onclick="_toggleDetail('${did}')">
              ${_posterImg(m.poster)}
              <div style="flex:1;min-width:0">
                <div style="font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${m.title} <span style="color:var(--text3);font-weight:400">(${m.year||''})</span></div>
                <div style="font-size:10px;color:var(--text3)">${m.date || 'TBA'}</div>
              </div>
              ${m.hasFile ? '<span style="font-size:10px;color:var(--green);flex-shrink:0">✓ Downloaded</span>' : ''}
            </div>
            <div class="svc-detail" id="${did}">
              <b>${m.title}</b> (${m.year||'?'})<br>
              Release: ${m.date || 'TBA'}<br>
              Status: ${m.hasFile ? '✅ On disk' : '⏳ Awaiting release'}
            </div>`;
        }).join('');
    } catch(e) { _svcError('radarr-card-body', 'Could not reach Radarr'); }
}

async function loadRadarrQueue() {
    const bodyId = 'radarr-card-body';
    try {
        const r = await fetch(API + '/api/services/radarr/queue');
        const d = await r.json();
        if (!d.configured) return _svcUnconfigured(bodyId, 'Radarr');
        if (d.error) return _svcError(bodyId, d.error);
        const el = document.getElementById(bodyId);
        if (!el) return;
        if (!d.queue || !d.queue.length) return _svcEmpty(bodyId, 'Nothing downloading');
        el.innerHTML = `<div style="font-size:10px;color:var(--text3);margin-bottom:4px">${d.totalRecords} item${d.totalRecords!==1?'s':''} in queue</div>` +
            d.queue.map((q, i) => {
            const pct = q.progress || 0;
            const barColor = pct > 90 ? 'var(--green)' : pct > 50 ? 'var(--blue)' : 'var(--yellow)';
            const did = 'rq-d-' + i;
            return `<div class="svc-item" onclick="_toggleDetail('${did}')">
              ${_posterImg(q.poster)}
              <div style="flex:1;min-width:0">
                <div style="font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${q.title}</div>
                <div class="svc-q-bar"><div class="svc-q-fill" style="width:${pct}%;background:${barColor}"></div></div>
              </div>
              <div style="text-align:right;flex-shrink:0">
                <div style="font-size:11px;font-weight:600;color:var(--blue)">${pct.toFixed(0)}%</div>
                <div style="font-size:9px;color:var(--text3)">${q.timeleft || '—'}</div>
              </div>
            </div>
            <div class="svc-detail" id="${did}">
              Quality: ${q.quality || '?'} · Size: ${_fmtSize(q.size)}<br>
              Client: ${q.downloadClient || '?'} · Indexer: ${q.indexer || '?'}<br>
              Status: ${q.status}
            </div>`;
        }).join('');
    } catch(e) { _svcError(bodyId, 'Could not reach Radarr queue'); }
}

async function loadRadarrLibrary() {
    const bodyId = 'radarr-card-body';
    try {
        const r = await fetch(API + '/api/services/radarr/library');
        const d = await r.json();
        if (!d.configured) return _svcUnconfigured(bodyId, 'Radarr');
        if (d.error) return _svcError(bodyId, d.error);
        const el = document.getElementById(bodyId);
        if (!el) return;
        el.innerHTML = `<div class="svc-lib-grid">
          <div class="svc-lib-stat"><div class="svc-lib-val">${d.total}</div><div class="svc-lib-label">Total Movies</div></div>
          <div class="svc-lib-stat"><div class="svc-lib-val">${d.monitored}</div><div class="svc-lib-label">Monitored</div></div>
          <div class="svc-lib-stat"><div class="svc-lib-val" style="color:var(--green)">${d.downloaded}</div><div class="svc-lib-label">Downloaded</div></div>
          <div class="svc-lib-stat"><div class="svc-lib-val" style="color:var(--red)">${d.missing}</div><div class="svc-lib-label">Missing</div></div>
        </div>`;
    } catch(e) { _svcError(bodyId, 'Could not load library stats'); }
}

// ══════════════════════════════════════════════════════════════════
// SONARR — Upcoming / Queue / Library
// ══════════════════════════════════════════════════════════════════
async function loadSonarrCard() {
    try {
        const r = await fetch(API + '/api/services/sonarr/calendar');
        const d = await r.json();
        if (!d.configured) return _svcUnconfigured('sonarr-card-body', 'Sonarr');
        if (d.error) return _svcError('sonarr-card-body', d.error);
        const el = document.getElementById('sonarr-card-body');
        if (!el) return;
        if (!d.episodes || !d.episodes.length) return _svcEmpty('sonarr-card-body', 'No upcoming episodes this week');
        el.innerHTML = d.episodes.map((ep, i) => {
            const did = 'sd-d-' + i;
            return `<div class="svc-item" onclick="_toggleDetail('${did}')">
              ${_posterImg(ep.poster)}
              <div style="flex:1;min-width:0">
                <div style="font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${ep.series}</div>
                <div style="font-size:10px;color:var(--text2)">S${String(ep.season).padStart(2,'0')}E${String(ep.episode).padStart(2,'0')} · ${ep.title}</div>
                <div style="font-size:10px;color:var(--text3)">${ep.airDate || 'TBA'}</div>
              </div>
              ${ep.hasFile ? '<span style="font-size:10px;color:var(--green);flex-shrink:0">✓ Downloaded</span>' : ''}
            </div>
            <div class="svc-detail" id="${did}">
              <b>${ep.series}</b> — ${ep.title}<br>
              Season ${ep.season}, Episode ${ep.episode}<br>
              Air date: ${ep.airDate || 'TBA'}<br>
              Status: ${ep.hasFile ? '✅ On disk' : '⏳ Not yet downloaded'}
            </div>`;
        }).join('');
    } catch(e) { _svcError('sonarr-card-body', 'Could not reach Sonarr'); }
}

async function loadSonarrQueue() {
    const bodyId = 'sonarr-card-body';
    try {
        const r = await fetch(API + '/api/services/sonarr/queue');
        const d = await r.json();
        if (!d.configured) return _svcUnconfigured(bodyId, 'Sonarr');
        if (d.error) return _svcError(bodyId, d.error);
        const el = document.getElementById(bodyId);
        if (!el) return;
        if (!d.queue || !d.queue.length) return _svcEmpty(bodyId, 'Nothing downloading');
        el.innerHTML = `<div style="font-size:10px;color:var(--text3);margin-bottom:4px">${d.totalRecords} item${d.totalRecords!==1?'s':''} in queue</div>` +
            d.queue.map((q, i) => {
            const pct = q.progress || 0;
            const barColor = pct > 90 ? 'var(--green)' : pct > 50 ? 'var(--blue)' : 'var(--yellow)';
            const did = 'sq-d-' + i;
            return `<div class="svc-item" onclick="_toggleDetail('${did}')">
              ${_posterImg(q.poster)}
              <div style="flex:1;min-width:0">
                <div style="font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${q.title}</div>
                <div style="font-size:10px;color:var(--text2)">${q.episode} · ${q.episodeTitle}</div>
                <div class="svc-q-bar"><div class="svc-q-fill" style="width:${pct}%;background:${barColor}"></div></div>
              </div>
              <div style="text-align:right;flex-shrink:0">
                <div style="font-size:11px;font-weight:600;color:var(--blue)">${pct.toFixed(0)}%</div>
                <div style="font-size:9px;color:var(--text3)">${q.timeleft || '—'}</div>
              </div>
            </div>
            <div class="svc-detail" id="${did}">
              Quality: ${q.quality || '?'} · Size: ${_fmtSize(q.size)}<br>
              Client: ${q.downloadClient || '?'} · Indexer: ${q.indexer || '?'}<br>
              Status: ${q.status}
            </div>`;
        }).join('');
    } catch(e) { _svcError(bodyId, 'Could not reach Sonarr queue'); }
}

async function loadSonarrLibrary() {
    const bodyId = 'sonarr-card-body';
    try {
        const r = await fetch(API + '/api/services/sonarr/library');
        const d = await r.json();
        if (!d.configured) return _svcUnconfigured(bodyId, 'Sonarr');
        if (d.error) return _svcError(bodyId, d.error);
        const el = document.getElementById(bodyId);
        if (!el) return;
        el.innerHTML = `<div class="svc-lib-grid">
          <div class="svc-lib-stat"><div class="svc-lib-val">${d.totalSeries}</div><div class="svc-lib-label">Total Series</div></div>
          <div class="svc-lib-stat"><div class="svc-lib-val">${d.monitored}</div><div class="svc-lib-label">Monitored</div></div>
          <div class="svc-lib-stat"><div class="svc-lib-val" style="color:var(--green)">${d.episodesOnDisk}</div><div class="svc-lib-label">Episodes on Disk</div></div>
          <div class="svc-lib-stat"><div class="svc-lib-val" style="color:var(--blue)">${d.episodes}</div><div class="svc-lib-label">Total Episodes</div></div>
        </div>`;
    } catch(e) { _svcError(bodyId, 'Could not load library stats'); }
}

// ══════════════════════════════════════════════════════════════════
// PLEX — Now Playing
// ══════════════════════════════════════════════════════════════════
async function loadPlexCard() {
    try {
        const r = await fetch(API + '/api/services/plex/sessions');
        const d = await r.json();
        if (!d.configured) return _svcUnconfigured('plex-card-body', 'Plex');
        if (d.error) return _svcError('plex-card-body', d.error);
        const el = document.getElementById('plex-card-body');
        if (!el) return;
        const count = document.getElementById('plex-stream-count');
        if (count) count.textContent = (d.sessions||[]).length + ' stream' + ((d.sessions||[]).length===1?'':'s');
        if (!d.sessions || !d.sessions.length) return _svcEmpty('plex-card-body', 'Nothing playing right now');
        el.innerHTML = d.sessions.map(s => {
            const pct = s.progress || 0;
            const barColor = pct > 80 ? 'var(--green)' : 'var(--blue)';
            return `<div style="padding:4px 0;border-bottom:1px solid var(--border)">
              <div style="display:flex;align-items:center;gap:8px;margin-bottom:3px">
                <span style="font-size:14px">▶</span>
                <div style="flex:1;min-width:0">
                  <div style="font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${s.title}</div>
                  ${s.subtitle ? `<div style="font-size:10px;color:var(--text2)">${s.subtitle}</div>` : ''}
                  <div style="font-size:10px;color:var(--text3)">${s.user || 'Unknown user'} · ${s.player || ''}</div>
                </div>
                <span style="font-size:11px;font-weight:600;color:var(--blue);flex-shrink:0">${pct}%</span>
              </div>
              <div class="svc-q-bar"><div class="svc-q-fill" style="width:${pct}%;background:${barColor}"></div></div>
            </div>`;
        }).join('');
    } catch(e) { _svcError('plex-card-body', 'Could not reach Plex'); }
}

// ══════════════════════════════════════════════════════════════════
// SEERR — Requests
// ══════════════════════════════════════════════════════════════════
async function loadSeerrCard() {
    const STATUS_LABELS = {1:'Pending',2:'Approved',3:'Declined',4:'Available'};
    const STATUS_COLORS = {1:'var(--yellow)',2:'var(--green)',3:'var(--red)',4:'var(--blue)'};
    try {
        const r = await fetch(API + '/api/services/seerr/requests');
        const d = await r.json();
        if (!d.configured) return _svcUnconfigured('seerr-card-body', 'Seerr');
        if (d.error) return _svcError('seerr-card-body', d.error);
        const el = document.getElementById('seerr-card-body');
        if (!el) return;
        if (!d.requests || !d.requests.length) return _svcEmpty('seerr-card-body', 'No recent requests');
        el.innerHTML = d.requests.map((req, i) => {
            const lbl   = STATUS_LABELS[req.status] || 'Unknown';
            const color = STATUS_COLORS[req.status] || 'var(--text3)';
            const typeIcon = req.type === 'movie' ? '🎬' : req.type === 'tv' ? '📺' : '❓';
            const did = 'sr-d-' + i;
            return `<div class="svc-item" onclick="_toggleDetail('${did}')">
              <span style="font-size:16px;flex-shrink:0">${typeIcon}</span>
              <div style="flex:1;min-width:0">
                <div style="font-size:12px;font-weight:600;color:var(--text2)">TMDB #${req.title || req.id}</div>
                <div style="font-size:10px;color:var(--text3)">by ${req.requestedBy || '?'} · ${req.createdAt}</div>
              </div>
              <span style="font-size:10px;font-weight:600;color:${color};flex-shrink:0">${lbl}</span>
            </div>
            <div class="svc-detail" id="${did}">
              Type: ${req.type || '?'} · Status: ${lbl}<br>
              Requested by: ${req.requestedBy || '?'}<br>
              Date: ${req.createdAt}
            </div>`;
        }).join('');
    } catch(e) { _svcError('seerr-card-body', 'Could not reach Seerr'); }
}

// ── Service settings save/load ─────────────────────────────────────────
async function saveSvcSettings() {
    const fields = {
        radarr_url: 'svc-radarr-url', radarr_api_key: 'svc-radarr-key',
        sonarr_url: 'svc-sonarr-url', sonarr_api_key: 'svc-sonarr-key',
        plex_url:   'svc-plex-url',   plex_token:     'svc-plex-token',
        seerr_url:  'svc-seerr-url',  seerr_api_key:  'svc-seerr-key'
    };
    const payload = {};
    for (const [key, id] of Object.entries(fields)) {
        const el = document.getElementById(id);
        if (el) payload[key] = el.value.trim();
    }
    try {
        const r = await fetch(API + '/api/settings', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify(payload)
        });
        const d = await r.json();
        showToast(d.error ? 'Error: ' + d.error : 'Service settings saved', d.error ? 'error' : 'success');
    } catch(e) { showToast('Save failed', 'error'); }
}

async function loadDashboardContainers() {
    try {
        const r = await fetch(API + '/api/containers');
        const d = await r.json();
        const ctrs = d.containers || [];
        const running = ctrs.filter(c => c.status === 'running');
        const stopped = ctrs.filter(c => c.status !== 'running');

        const badge = document.getElementById('ov-ctr-badge');
        if (badge) badge.textContent = running.length + ' / ' + ctrs.length;

        const list = document.getElementById('ov-ctr-list');
        if (!list) return;

        if (!ctrs.length) {
            list.innerHTML = '<div style="color:var(--text3);font-size:12px;text-align:center;padding:8px">No containers found</div>';
            return;
        }

        const rows = [...running, ...stopped].map(c => {
            const isRunning = c.status === 'running';
            const statusColor = isRunning ? 'var(--green)' : (c.status === 'restarting' ? 'var(--orange)' : 'var(--red)');
            const statusBg = isRunning ? 'var(--green2)' : (c.status === 'restarting' ? 'var(--orange2,#2d1f00)' : 'var(--red2)');
            const icon = ctrIcon(c.name);
            const ports = c.ports && c.ports.length ? c.ports.slice(0,3).join('  ') : '—';
            const uptime = c.uptime || '—';
            return `<div style="display:flex;align-items:center;gap:10px;background:var(--bg3);border-radius:8px;padding:8px 12px;opacity:${isRunning ? 1 : 0.55}">
  <span style="font-size:18px;flex-shrink:0">${icon}</span>
  <div style="flex:1;min-width:0;overflow:hidden">
    <div style="font-size:13px;font-weight:600;color:var(--text1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${c.name}</div>
    <div style="font-size:10px;color:var(--text3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${c.image}</div>
  </div>
  <span style="flex-shrink:0;font-size:10px;font-weight:600;padding:2px 8px;border-radius:10px;background:${statusBg};color:${statusColor}">${c.status}</span>
  ${isRunning
    ? `<div style="flex-shrink:0;font-size:11px;color:var(--text2);text-align:center;min-width:80px">
        <div>CPU <b id="ov-cpu-${c.name}" style="color:var(--blue)">—</b></div>
        <div>MEM <b id="ov-mem-${c.name}" style="color:var(--purple)">—</b></div>
       </div>`
    : `<div style="flex-shrink:0;font-size:11px;color:var(--text3);min-width:80px;text-align:center">up ${uptime}</div>`}
  <div style="flex-shrink:0;font-size:10px;color:var(--text3);text-align:right;max-width:120px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${ports}">${ports}</div>
  <div style="flex-shrink:0;display:flex;gap:4px">
    ${isRunning
      ? `<button class="btn orange" style="padding:2px 7px;font-size:11px" title="Restart" onclick="ctrAction('${c.name}','restart')">↺</button>
         <button class="btn red" style="padding:2px 7px;font-size:11px" title="Stop" onclick="ctrAction('${c.name}','stop')">■</button>`
      : `<button class="btn green" style="padding:2px 7px;font-size:11px" title="Start" onclick="ctrAction('${c.name}','start')">▶</button>`}
  </div>
</div>`;
        });
        list.innerHTML = rows.join('');

        // Fetch per-container stats for running ones
        running.forEach(c => loadOvCtrStats(c.name));
    } catch(e) {
        const list = document.getElementById('ov-ctr-list');
        if (list) list.innerHTML = '<div style="color:var(--text3);font-size:12px;text-align:center;padding:8px">Docker not available</div>';
    }
}

async function loadOvCtrStats(name) {
    try {
        const r = await fetch(API + `/api/container/${name}/stats`);
        const d = await r.json();
        if (d.error) return;
        const cpuEl = document.getElementById('ov-cpu-' + name);
        const memEl = document.getElementById('ov-mem-' + name);
        if (cpuEl) cpuEl.textContent = d.cpu_pct + '%';
        if (memEl) memEl.textContent = d.mem_usage_mb + ' MB';
    } catch(e) {}
}

// ── Stack Actions ─────────────────────────────────────────────────────
async function stackAction(name, action) {
    showToast('Stack ' + name + ' ' + action + '…', 'info', 3000);
    try {
        const r = await fetch(API + `/api/stack/${name}/${action}`, {method:'POST'});
        const d = await r.json();
        if (d.error) {
            showToast('Stack error: ' + d.error, 'error');
        } else {
            showToast('Stack ' + name + ' ' + (action === 'up' ? 'started' : 'stopped'), 'success');
            setTimeout(loadStackManager, 800);
        }
    } catch(e) { showToast('Request failed', 'error'); }
}

// ── Logs Auto-refresh ─────────────────────────────────────────────────
let _logsAutoRefresh = null;
function toggleLogsAutoRefresh(btn) {
    if (_logsAutoRefresh) {
        clearInterval(_logsAutoRefresh);
        _logsAutoRefresh = null;
        if (btn) btn.textContent = 'Auto-refresh: Off';
    } else {
        _logsAutoRefresh = setInterval(loadLogs, 10000);
        if (btn) btn.textContent = 'Auto-refresh: On';
        loadLogs();
    }
}

// ── Polling ───────────────────────────────────────────────────────────
// Active containers tab: refresh full list every 8s
setInterval(() => {
    if (currentTab === 'containers') loadContainers();
}, 8000);

// Slower polls for other tabs
setInterval(() => {
    if (currentTab === 'storage') loadStorage();
    else if (currentTab === 'network') loadNetwork();
    else if (currentTab === 'overview') loadDashboardContainers();
}, 10000);

// Background stats refresh for running containers (every 30s when NOT on containers tab)
setInterval(() => {
    if (currentTab !== 'containers') {
        allContainers.filter(c => c.status === 'running').forEach(c => loadCtrStats(c.name, false));
    }
}, 30000);

// ── Boot ──────────────────────────────────────────────────────────────
initGauges();
loadOverview();
loadContainers();   // also populates allContainers for alerts
loadWeather();
loadDockerInfo();
loadOverviewExtras();   // also calls loadDashboardContainers()
startSSE();
// Initial alerts render (will be updated once containers load)
setTimeout(refreshAlerts, 2000);

// ── Theme & Appearance ────────────────────────────────────────────────────
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t || 'dark');
  localStorage.setItem('arrhub_theme', t);
  document.querySelectorAll('.theme-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.t === t));
}
function applyAccent(a) {
  if (a === 'blue') {
    document.documentElement.removeAttribute('data-accent');
  } else {
    document.documentElement.setAttribute('data-accent', a);
  }
  localStorage.setItem('arrhub_accent', a);
  document.querySelectorAll('.accent-swatch').forEach(b =>
    b.classList.toggle('active', b.dataset.a === a));
}
// Resolve Unsplash page URLs to direct image URLs
function _resolveUnsplash(url) {
    // Unsplash photo page format: https://unsplash.com/photos/description-slug-PHOTOID
    // The actual photo ID is the LAST dash-separated token (e.g. MjH55Ef3w_0)
    const m = url.match(/unsplash\.com\/photos\/([\w-]+)/);
    if (m) {
        const slug = m[1].split('?')[0];
        const parts = slug.split('-');
        const photoId = parts[parts.length - 1]; // last segment is the short photo ID
        // source.unsplash.com understands short IDs and serves the CDN image
        return `https://source.unsplash.com/${photoId}/1920x1080`;
    }
    // https://unsplash.com/s/photos/... (search page) — can't resolve directly
    if (url.includes('unsplash.com/s/')) return '';
    return url;
}

function saveAppearance() {
  let url = document.getElementById('bg-url-input')?.value.trim() || '';
  if (url) {
    const resolved = _resolveUnsplash(url);
    if (!resolved) { showToast('Cannot use Unsplash search pages — paste a photo page URL', 'error', 4000); return; }
    url = resolved;
    const ui = document.getElementById('bg-url-input');
    if (ui) ui.value = url; // update input to show resolved URL
  }
  const blur    = parseInt(document.getElementById('bg-blur-input')?.value || 4);
  const overlay = parseInt(document.getElementById('bg-overlay-input')?.value || 70);
  _applyBg(url, blur, overlay);
  localStorage.setItem('arrhub_bg', JSON.stringify({url, blur, overlay}));
  showToast('Background applied', 'success', 2000);
}
function resetAppearance() {
  applyTheme('dark');
  applyAccent('blue');
  _applyBg('', 4, 70);
  localStorage.removeItem('arrhub_bg');
  localStorage.removeItem('arrhub_theme');
  localStorage.removeItem('arrhub_accent');
  showToast('Appearance reset to defaults', 'info', 2000);
}
function _applyBg(url, blur, overlay) {
  const el = document.getElementById('bg-layer');
  if (!el) return;
  if (url) {
    el.style.cssText = `display:block;position:fixed;inset:0;z-index:0;`
      + `background:url(${url}) center/cover no-repeat;`
      + `filter:blur(${blur}px) brightness(${(100 - overlay) / 100});`
      + `transform:scale(1.05);`; // slightly enlarge to hide blur edges
  } else {
    el.style.display = 'none';
  }
  // Also sync sliders if visible
  const bi = document.getElementById('bg-blur-input');
  const oi = document.getElementById('bg-overlay-input');
  if (bi) { bi.value = blur; document.getElementById('bg-blur-val').textContent = blur; }
  if (oi) { oi.value = overlay; document.getElementById('bg-overlay-val').textContent = overlay; }
  const ui = document.getElementById('bg-url-input');
  if (ui) ui.value = url;
}
// Restore saved appearance on load
(function _restoreAppearance() {
  const t  = localStorage.getItem('arrhub_theme') || 'dark';
  const a  = localStorage.getItem('arrhub_accent') || 'blue';
  applyTheme(t);
  applyAccent(a);
  const bgRaw = localStorage.getItem('arrhub_bg');
  if (bgRaw) {
    try { const bg = JSON.parse(bgRaw); _applyBg(bg.url||'', bg.blur||4, bg.overlay||70); } catch(e) {}
  }
})();

// ── GridStack Drag-and-Drop Dashboard ─────────────────────────────────────
// Requires gridstack@10 loaded below. Activated only when "Edit Layout" clicked.
// ── HLS.js player helper ─────────────────────────────────────────────────────
const _hlsInstances = {};  // track HLS instances keyed by videoId to avoid duplicates

function hlsPlay(videoId, url) {
    if (!url) { showToast('No stream URL provided', 'error'); return; }
    const video = document.getElementById(videoId);
    if (!video) return;
    video.style.display = 'block'; // show the video element (may be hidden by default)

    // Destroy any existing HLS instance for this element
    if (_hlsInstances[videoId]) {
        try { _hlsInstances[videoId].destroy(); } catch(e) {}
        delete _hlsInstances[videoId];
    }

    if (typeof Hls !== 'undefined' && Hls.isSupported()) {
        const hls = new Hls({ enableWorker: true, lowLatencyMode: true });
        _hlsInstances[videoId] = hls;
        hls.loadSource(url);
        hls.attachMedia(video);
        hls.on(Hls.Events.MANIFEST_PARSED, () => {
            video.play().catch(() => {}); // autoplay may be blocked — that's fine
        });
        hls.on(Hls.Events.ERROR, (event, data) => {
            if (data.fatal) {
                showToast('Stream error — CORS or stream unavailable. Try another stream.', 'error', 5000);
            }
        });
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        // Safari/iOS have native HLS support
        video.src = url;
        video.play().catch(() => {});
    } else {
        showToast('HLS not supported in this browser', 'error');
    }
}

let _gs  = null;
let _gsEditing = false;

// Toggle .widget-compact class based on actual pixel width/height of the widget
function _applyCompactClass(item) {
  const rect = item.getBoundingClientRect();
  item.classList.toggle('widget-compact', rect.width < 400 || rect.height < 200);
}

function _gsInit() {
  if (_gs || typeof GridStack === 'undefined') return false;
  const el = document.getElementById('ov-grid');
  if (!el) return false;
  const isMobile = window.innerWidth < 900;
  _gs = GridStack.init({
    cellHeight: 60,           // smaller cells → finer positional control
    column: isMobile ? 1 : 12,
    margin: 8,
    staticGrid: true,
    animate: true,
    float: false,
    disableDrag: isMobile,
    disableResize: isMobile,
    resizable: { handles: 'e,se,s,sw,w' },  // resize from all sides
    draggable: { handle: '.panel-title' },   // drag by title bar only
  }, el);
  // When a widget is resized, tell Chart.js canvases inside to resize + toggle compact class
  _gs.on('resizestop', (event, element) => {
    element.querySelectorAll('canvas').forEach(canvas => {
      const chart = (typeof Chart !== 'undefined' && Chart.getChart) ? Chart.getChart(canvas) : null;
      if (chart) { chart.resize(); }
    });
    element.querySelectorAll('.panel,.stat-grid').forEach(el => { el.style.opacity = '0.99'; requestAnimationFrame(() => { el.style.opacity = ''; }); });
    _applyCompactClass(element);
  });
  // Apply compact class to all widgets initially
  el.querySelectorAll('.grid-stack-item').forEach(item => _applyCompactClass(item));
  // Restore saved layout — invalidate if widget set changed (layout version bump)
  const _GRID_VER = 2;  // bump when adding/removing widgets
  const saved = localStorage.getItem('arrhub_grid');
  const savedVer = parseInt(localStorage.getItem('arrhub_grid_ver') || '0');
  if (saved && savedVer === _GRID_VER) {
    try {
      const items = JSON.parse(saved);
      _gs.load(items, false);
    } catch(e) { localStorage.removeItem('arrhub_grid'); }
  } else {
    localStorage.removeItem('arrhub_grid');
    localStorage.setItem('arrhub_grid_ver', String(_GRID_VER));
  }
  // Show Reset button if a saved layout exists
  const resetBtn = document.getElementById('ov-reset-btn');
  if (resetBtn && localStorage.getItem('arrhub_grid')) resetBtn.style.display = '';
  // Mark grid as ready — removes the visibility:hidden that prevents stacking flash
  el.classList.add('gs-ready');
  return true;
}

// ── Widget palette definitions ────────────────────────────────────────────────
const WIDGET_DEFS = {
  gauges:   { label: 'System Gauges',    icon: '📊',  dw:12, dh:3, dx:0,  dy:0  },
  sysinfo:  { label: 'System Info',      icon: 'ℹ️',  dw:6,  dh:4, dx:0,  dy:3  },
  weather:  { label: 'Weather',          icon: '🌤️', dw:6,  dh:4, dx:6,  dy:3  },
  services: { label: 'Service Cards',    icon: '🃏',  dw:12, dh:5, dx:0,  dy:7  },
  infra:    { label: 'Docker & Network', icon: '🐳',  dw:12, dh:3, dx:0,  dy:12 },
  logs:     { label: 'Recent Logs',      icon: '📋',  dw:4,  dh:4, dx:0,  dy:15 },
  ctrs:     { label: 'Containers',       icon: '📦',  dw:8,  dh:4, dx:4,  dy:15 },
  launcher: { label: 'Service Launcher', icon: '🚀',  dw:12, dh:3, dx:0,  dy:19 },
};

let _hiddenWidgets = new Set();

// Save full widget config (hidden list + grid positions) to server
async function _saveWidgetConfig() {
  try {
    const config = {
      hidden: [..._hiddenWidgets],
      grid: _gs ? _gs.save(false) : null
    };
    await fetch('/api/widget_config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config)
    });
  } catch(e) {}
}

// Load widget config from server and apply hidden list
async function _loadWidgetConfig() {
  try {
    const r = await fetch('/api/widget_config');
    const data = await r.json();
    if (Array.isArray(data.hidden) && data.hidden.length) {
      _hiddenWidgets = new Set(data.hidden);
      _hiddenWidgets.forEach(id => {
        const el = document.querySelector(\`.grid-stack-item[gs-id="\${id}"]\`);
        if (el) el.style.display = 'none';
      });
    }
    // Grid positions from server take priority over localStorage
    if (data.grid && Array.isArray(data.grid)) {
      localStorage.setItem('arrhub_grid', JSON.stringify(data.grid));
    }
  } catch(e) {}
}

// Remove a widget during edit mode
function removeWidget(gsId) {
  if (!_gsEditing || !_gs) return;
  const el = document.querySelector(\`.grid-stack-item[gs-id="\${gsId}"]\`);
  if (!el) return;
  _hiddenWidgets.add(gsId);
  _gs.removeWidget(el, false);       // remove from grid but keep DOM node
  el.style.display = 'none';
  document.getElementById('ov-grid').appendChild(el);  // keep in DOM so it can be restored
  const addBtn = document.getElementById('ov-add-btn');
  if (addBtn) addBtn.style.display = '';
  showToast(\`"\${WIDGET_DEFS[gsId]?.label || gsId}" hidden — use Add Widget to restore\`, 'info', 3000);
}

// Restore a hidden widget
function restoreWidget(gsId) {
  const def = WIDGET_DEFS[gsId];
  if (!def || !_gs) return;
  _hiddenWidgets.delete(gsId);
  const el = document.querySelector(\`.grid-stack-item[gs-id="\${gsId}"]\`);
  if (el) {
    el.style.display = '';
    _gs.makeWidget(el);
  }
  if (_hiddenWidgets.size === 0) {
    const addBtn = document.getElementById('ov-add-btn');
    if (addBtn) addBtn.style.display = 'none';
  }
  document.getElementById('widget-palette-modal').style.display = 'none';
  showToast(\`"\${def.label}" restored\`, 'success', 2000);
}

// Show widget palette modal
function showWidgetPalette() {
  const body = document.getElementById('widget-palette-body');
  if (!body) return;
  body.innerHTML = '';
  Object.entries(WIDGET_DEFS).forEach(([id, def]) => {
    const isHidden = _hiddenWidgets.has(id);
    const div = document.createElement('div');
    div.className = 'widget-palette-card' + (isHidden ? '' : ' active');
    div.title = isHidden ? 'Click to restore' : 'Click to hide';
    div.innerHTML = \`<div class="wpc-icon">\${def.icon}</div><div class="wpc-name">\${def.label}</div><div class="wpc-status">\${isHidden ? '➕ Hidden' : '✅ Visible'}</div>\`;
    div.onclick = () => {
      if (isHidden) restoreWidget(id);
      else removeWidget(id);
      showWidgetPalette();  // refresh palette
    };
    body.appendChild(div);
  });
  document.getElementById('widget-palette-modal').style.display = 'flex';
}

// ── Service Launcher ─────────────────────────────────────────────────────────
const _svcIcons = {
  radarr:'🎥', sonarr:'📺', lidarr:'🎵', bazarr:'💬', prowlarr:'🔍',
  jellyfin:'🎬', plex:'▶️', emby:'📽️', qbittorrent:'⬇️', transmission:'⬇️',
  seerr:'🎬', tautulli:'📊', portainer:'🐳', dozzle:'📋', grafana:'📈',
  uptime_kuma:'🟢', tdarr:'📦', fileflows:'🔄', handbrake:'🔧',
  nextcloud:'☁️', immich:'🖼️', navidrome:'🎼', watchtower:'🔄',
  vaultwarden:'🔐', n8n:'⚡', node_red:'🔴', komga:'📚', kavita:'📖',
  arrhub_webui:'🏠', pihole:'🕳️', adguardhome:'🛡️',
};
function _launcherIcon(name) {
  const k = name.toLowerCase().replace(/[^a-z0-9_]/g,'_');
  for (const [key, icon] of Object.entries(_svcIcons)) {
    if (k.includes(key)) return icon;
  }
  return '📦';
}

async function loadServiceLauncher() {
  const el = document.getElementById('launcher-tiles');
  if (!el) return;
  try {
    const r = await fetch('/api/containers');
    const data = await r.json();
    const running = (data.containers || []).filter(c => c.status === 'running');
    if (!running.length) {
      el.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:8px">No running containers found.</div>';
      return;
    }
    el.innerHTML = running.map(c => {
      const name = (c.name || '').replace(/^\\//, '');
      const ports = c.ports || [];
      // Pick first host port that looks like an HTTP port
      const portEntry = ports.find(p => /^\\d+:\\d+/.test(p));
      const hostPort = portEntry ? portEntry.split(':')[0] : null;
      const url = hostPort ? \`http://\${window.location.hostname}:\${hostPort}\` : null;
      const icon = _launcherIcon(name);
      const tileHtml = \`<div class="launcher-tile-icon">\${icon}</div>
        <div class="launcher-tile-name">\${name}</div>
        \${hostPort ? \`<div class="launcher-tile-port">:\${hostPort}</div>\` : ''}\`;
      return url
        ? \`<a href="\${url}" target="_blank" rel="noopener" class="launcher-tile">\${tileHtml}</a>\`
        : \`<div class="launcher-tile" style="opacity:.5;cursor:default">\${tileHtml}</div>\`;
    }).join('');
  } catch(e) {
    el.innerHTML = '<div style="color:var(--text3);font-size:12px">Failed to load containers.</div>';
  }
}

function toggleGridEdit() {
  _gsInit();
  if (!_gs) { showToast('GridStack not loaded yet', 'error'); return; }
  _gsEditing = !_gsEditing;
  const btn      = document.getElementById('ov-edit-btn');
  const resetBtn = document.getElementById('ov-reset-btn');
  const addBtn   = document.getElementById('ov-add-btn');
  const grid     = document.getElementById('ov-grid');
  if (_gsEditing) {
    _gs.setStatic(false);
    _gs.on('change', () => {
      localStorage.setItem('arrhub_grid', JSON.stringify(_gs.save(false)));
      if (resetBtn) resetBtn.style.display = '';
    });
    btn.innerHTML = '<svg width="11" height="11" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7H5a2 2 0 00-2 2v9a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-3m-1 4l-3 3m0 0l-3-3m3 3V4"/></svg> Save Layout';
    btn.style.background = 'var(--blue2)';
    btn.style.color      = 'var(--blue)';
    grid.classList.add('gs-editing');
    if (addBtn) addBtn.style.display = '';
    showToast('Drag by title bar · resize from edges · ✕ to hide widgets · click Save when done', 'info', 5000);
  } else {
    _gs.setStatic(true);
    const gridData = _gs.save(false);
    localStorage.setItem('arrhub_grid', JSON.stringify(gridData));
    btn.innerHTML = '<svg width="11" height="11" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg> Edit Layout';
    btn.style.background = '';
    btn.style.color      = '';
    grid.classList.remove('gs-editing');
    if (addBtn) addBtn.style.display = 'none';
    _saveWidgetConfig();  // persist to server
    showToast('Layout saved', 'success', 2000);
  }
}

function resetGridLayout() {
  if (!confirm('Reset overview layout to defaults? (all widget positions + hidden state reset)')) return;
  localStorage.removeItem('arrhub_grid');
  _hiddenWidgets.clear();
  _saveWidgetConfig();
  const resetBtn = document.getElementById('ov-reset-btn');
  if (resetBtn) resetBtn.style.display = 'none';
  location.reload();
}

// Init GridStack in static mode on load to apply any saved positions
window.addEventListener('load', async () => {
  await _loadWidgetConfig();   // apply hidden widgets from server before init
  _gsInit();                   // GridStack is available by window.load time
  loadServiceLauncher();       // populate launcher widget
});
</script>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
</body>
</html>

"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9999, debug=False)
