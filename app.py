#!/usr/bin/env python3
#
"""
ArrHub Monitor — Enhanced Server Administration Dashboard
Version: 3.15.21 · Full deployment, update management, and real-time monitoring
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
from flask import Flask, jsonify, request, Response, make_response

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
_logs_cache    = {"data": None, "ts": 0}   # short-lived; refreshed every 10 s
_network_cache = {"data": None, "ts": 0}   # refreshed every 30 s
_overview_static_cache = {"data": None, "ts": 0}  # hostname/kernel/arch — rarely change
CACHE_WEATHER = 1800  # 30 minutes
CACHE_RSS = 900       # 15 minutes
CACHE_CATALOG = 300   # 5 minutes
CACHE_LOGS = 10       # 10 seconds
CACHE_NETWORK = 30    # 30 seconds
CACHE_STATIC = 300    # 5 minutes for hostname/kernel info

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
    """System overview: CPU, memory, load average, uptime.
    Static fields (hostname, kernel, arch) are cached for 5 min.
    cpu_percent uses non-blocking interval=0 after a warm-up read."""
    try:
        # Static info — rarely changes; cache for 5 minutes
        now = time.time()
        static = _overview_static_cache.get("data")
        if not static or now - _overview_static_cache.get("ts", 0) > CACHE_STATIC:
            static = {
                "hostname": os.uname().nodename,
                "os":       os.uname().sysname,
                "kernel":   os.uname().release,
                "arch":     os.uname().machine,
                "python":   "3.12",
                "cpu_count": psutil.cpu_count(),
            }
            _overview_static_cache["data"] = static
            _overview_static_cache["ts"] = now

        # Non-blocking cpu_percent — first call seeds the counter, returns 0.0
        # Subsequent calls (SSE already calls this every 2 s) return a real value
        cpu_pct = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        load = os.getloadavg() if hasattr(os, 'getloadavg') else (0, 0, 0)
        uptime = now - psutil.boot_time()

        # Temperature
        temps = {}
        try:
            if hasattr(psutil, 'sensors_temperatures'):
                temps = psutil.sensors_temperatures()
        except Exception:
            pass

        return jsonify({
            **static,
            "cpu_percent": round(cpu_pct, 1),
            "mem_percent": round(mem.percent, 1),
            "mem_used": mem.used,
            "mem_total": mem.total,
            "memory": {
                "total": mem.total, "used": mem.used,
                "percent": round(mem.percent, 1), "available": mem.available
            },
            "load_avg": {"1m": round(load[0], 2), "5m": round(load[1], 2), "15m": round(load[2], 2)},
            "uptime_seconds": uptime,
            "uptime_display": _format_uptime(uptime),
            "temperatures": temps,
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
        # Docker bind-mounts for metadata files all point to the same device.
        # Skip known metadata paths and deduplicate by (device, total_bytes).
        _SKIP_MOUNTS  = {'/etc/resolv.conf','/etc/hostname','/etc/hosts','/etc/mtab','/etc/localtime','/etc/timezone'}
        _SKIP_FSTYPES = {'tmpfs','devtmpfs','squashfs','overlay','none','proc','sysfs','devpts','cgroup','cgroup2','mqueue','hugetlbfs','pstore','securityfs','debugfs','tracefs','bpf','autofs'}
        _seen_devs: set = set()
        filesystems = []
        for part in psutil.disk_partitions(all=True):
            if part.mountpoint in _SKIP_MOUNTS:
                continue
            if part.fstype in _SKIP_FSTYPES:
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
                dev_key = (part.device or part.mountpoint, usage.total)
                if dev_key in _seen_devs:
                    continue
                _seen_devs.add(dev_key)
                filesystems.append({
                    "mountpoint": part.mountpoint,
                    "device":     part.device,
                    "total":      usage.total,
                    "used":       usage.used,
                    "free":       usage.free,
                    "percent":    usage.percent,
                    "fstype":     part.fstype
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
    """Network interfaces and statistics — cached for 30 s."""
    _now = time.time()
    if _network_cache["data"] and _now - _network_cache["ts"] < CACHE_NETWORK:
        return jsonify(_network_cache["data"])
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
        _network_cache["data"] = {"interfaces": interfaces, "io": {
            "bytes_sent": net_io.bytes_sent, "bytes_recv": net_io.bytes_recv,
            "packets_sent": net_io.packets_sent, "packets_recv": net_io.packets_recv,
            "errin": net_io.errin, "errout": net_io.errout,
            "dropin": net_io.dropin, "dropout": net_io.dropout,
        }, "connections": connections}
        _network_cache["ts"] = _now
        return jsonify(_network_cache["data"])
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
            "freq": psutil.cpu_freq()._asdict() if psutil.cpu_freq() else {},
            "percent": psutil.cpu_percent(interval=0.2)
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
    """System logs from Docker containers, ArrHub logs, and host. Cached 10 s for generic requests."""
    lines_count = request.args.get('lines', 100, type=int)
    unit = request.args.get('unit', '')
    # Only cache the generic multi-container request (no unit filter)
    _now = time.time()
    if not unit and _logs_cache["data"] and _now - _logs_cache["ts"] < CACHE_LOGS:
        cached = _logs_cache["data"]
        return jsonify({"lines": cached[-lines_count:]})
    try:

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

        # Sort and cache
        log_lines.sort()
        if not unit:
            _logs_cache["data"] = log_lines
            _logs_cache["ts"] = _now

        return jsonify({"lines": log_lines[-lines_count:]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/dashboard")
def api_dashboard():
    """Combined dashboard endpoint — returns logs + network in one round-trip.
    Called by the overview page every 30 s instead of two separate requests."""
    _now = time.time()
    # Logs (cached separately)
    if not _logs_cache["data"] or _now - _logs_cache["ts"] >= CACHE_LOGS:
        # Populate logs cache inline so dashboard always shows logs
        try:
            fresh = []
            if DOCKER_OK:
                for _lc in _dc.containers.list():
                    try:
                        _ll = _lc.logs(tail=8, timestamps=True).decode('utf-8', errors='replace')
                        for _line in _ll.strip().split('\n'):
                            if _line.strip():
                                fresh.append(f"[{_lc.name}] {_line}")
                    except Exception:
                        pass
            if fresh:
                fresh.sort()
                _logs_cache["data"] = fresh
                _logs_cache["ts"] = _now
        except Exception:
            pass
    log_lines = _logs_cache["data"][-12:] if _logs_cache["data"] else []
    # Network IO (cached separately)
    if _network_cache["data"] and _now - _network_cache["ts"] < CACHE_NETWORK:
        net_io = _network_cache["data"].get("io", {})
    else:
        try:
            nio = psutil.net_io_counters()
            net_io = {"bytes_sent": nio.bytes_sent, "bytes_recv": nio.bytes_recv}
        except Exception:
            net_io = {}
    return jsonify({"logs": log_lines, "net_io": net_io, "ts": int(_now)})

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
            "version": "3.15.21",
            # Service integration keys — returned so the UI can re-populate fields on revisit
            "radarr_url":        _db_get("radarr_url", ""),
            "radarr_api_key":    _db_get("radarr_api_key", ""),
            "sonarr_url":        _db_get("sonarr_url", ""),
            "sonarr_api_key":    _db_get("sonarr_api_key", ""),
            "qbittorrent_url":   _db_get("qbittorrent_url", ""),
            "qbittorrent_user":  _db_get("qbittorrent_user", "admin"),
            "qbittorrent_pass":  _db_get("qbittorrent_pass", ""),
            "downloader_type":   _db_get("downloader_type", "qbittorrent"),
            "transmission_url":  _db_get("transmission_url", ""),
            "transmission_user": _db_get("transmission_user", ""),
            "transmission_pass": _db_get("transmission_pass", ""),
            "deluge_url":        _db_get("deluge_url", ""),
            "deluge_pass":       _db_get("deluge_pass", ""),
            "plex_url":       _db_get("plex_url", ""),
            "plex_token":     _db_get("plex_token", ""),
            "seerr_url":      _db_get("seerr_url", ""),
            "seerr_api_key":  _db_get("seerr_api_key", ""),
            "football_api_key": _db_get("football_api_key", ""),
            "weather_city":     _db_get("weather_city", ""),
            "weather_country":  _db_get("weather_country", ""),
            "reddit_client_id":     _db_get("reddit_client_id", ""),
            "reddit_client_secret": _db_get("reddit_client_secret", ""),
            "reddit_username":      _db_get("reddit_username", ""),
            "reddit_password":      _db_get("reddit_password", ""),
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
        "qbittorrent_url", "qbittorrent_user", "qbittorrent_pass",
        "downloader_type", "transmission_url", "transmission_user", "transmission_pass",
        "deluge_url", "deluge_pass",
        "plex_url", "plex_token",
        "seerr_url", "seerr_api_key",
        "football_api_key",
        "weather_city", "weather_country",
        "reddit_client_id", "reddit_client_secret", "reddit_username", "reddit_password",
    ]
    for key in allowed:
        if key in data:
            _db_set(key, data[key])
    return jsonify({"status": "saved"})

@app.route("/api/config/export")
def api_config_export():
    """Export all settings from the database as a JSON backup."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        payload = {
            "arrhub_backup": True,
            "version": "3.15.35",
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "settings": {k: v for k, v in rows},
        }
        resp = make_response(json.dumps(payload, indent=2))
        resp.headers["Content-Type"] = "application/json"
        resp.headers["Content-Disposition"] = f'attachment; filename="arrhub-config-{time.strftime("%Y%m%d")}.json"'
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/config/import", methods=["POST"])
@require_auth
def api_config_import():
    """Restore settings from a JSON backup (writes every key/value pair)."""
    data = request.json or {}
    if not data.get("arrhub_backup"):
        return jsonify({"error": "Not a valid ArrHub backup file"}), 400
    settings = data.get("settings", {})
    if not settings:
        return jsonify({"error": "No settings found in backup"}), 400
    # Skip internal/runtime keys that should not be restored
    _skip = {"_schema_version", "session_secret"}
    count = 0
    try:
        with sqlite3.connect(DB_PATH) as conn:
            for key, value in settings.items():
                if isinstance(key, str) and key not in _skip:
                    conn.execute(
                        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                        (key, str(value) if value is not None else "")
                    )
                    count += 1
        return jsonify({"ok": True, "restored": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
    return jsonify({"update_available": False, "version": "3.15.21"})

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
        # Check cache (bust if location changed)
        weather_city = _get_setting("weather_city", "").strip()
        weather_country = _get_setting("weather_country", "").strip()
        cache_key = f"{weather_city}|{weather_country}"
        if _weather_cache["data"] and (time.time() - _weather_cache["ts"]) < CACHE_WEATHER and _weather_cache.get("loc_key") == cache_key:
            return jsonify(_weather_cache["data"])

        # Determine coordinates
        if weather_city:
            # Geocode city using open-meteo geocoding API
            import urllib.request as _ur, json as _jr
            q = weather_city + (f",{weather_country}" if weather_country else "")
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={requests.utils.quote(q)}&count=1"
            geo_resp = requests.get(geo_url, timeout=5)
            geo_data = geo_resp.json()
            results = geo_data.get("results", [])
            if results:
                lat = results[0].get("latitude", 0)
                lon = results[0].get("longitude", 0)
                location_name = results[0].get("name", weather_city)
                country_name = results[0].get("country", weather_country)
            else:
                return jsonify({"error": f"Could not find location: {q}"}), 404
        else:
            # Fallback: Get location from ipapi.co
            geo_resp = requests.get("https://ipapi.co/json/", timeout=5)
            geo = geo_resp.json()
            lat, lon = geo.get("latitude", 0), geo.get("longitude", 0)
            location_name = geo.get("city", "Unknown")
            country_name = geo.get("country_name", "")

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
            "location": f"{location_name}, {country_name}",
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
        _weather_cache["loc_key"] = cache_key
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
        "Reddit": [
            {"name": "r/selfhosted", "url": "https://www.reddit.com/r/selfhosted/.rss", "icon": "🤖"},
            {"name": "r/homelab", "url": "https://www.reddit.com/r/homelab/.rss", "icon": "🖥️"},
            {"name": "r/ProxmoxVE", "url": "https://www.reddit.com/r/Proxmox/.rss", "icon": "📦"},
            {"name": "r/docker", "url": "https://www.reddit.com/r/docker/.rss", "icon": "🐳"},
            {"name": "r/linux", "url": "https://www.reddit.com/r/linux/.rss", "icon": "🐧"},
            {"name": "r/netsec", "url": "https://www.reddit.com/r/netsec/.rss", "icon": "🔐"},
        ],
        "YouTube": [
            {"name": "Linus Tech Tips", "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCXuqSBlHAE6Xw-yeJA0Tunw", "icon": "▶️"},
            {"name": "Fireship", "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCsBjURrPoezykLs9EqgamOA", "icon": "🔥"},
            {"name": "NetworkChuck", "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UC9x0AN7BWHpCDHSm9NiJFJQ", "icon": "🌐"},
            {"name": "TechLinked", "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCeeFfhMcJa1kjtfZAGskOCA", "icon": "🔗"},
        ],
    }
    # Append user custom feeds as their own category
    custom = _load_custom_feeds()
    if custom.get("feeds"):
        my_rss = [f for f in custom["feeds"] if f.get("type") != "reddit"]
        my_reddit = [f for f in custom["feeds"] if f.get("type") == "reddit"]
        if my_rss:
            feeds["My Feeds"] = my_rss
        if my_reddit:
            # Merge into Reddit category
            feeds.setdefault("Reddit", [])
            for f in my_reddit:
                if not any(x["name"] == f["name"] for x in feeds["Reddit"]):
                    feeds["Reddit"].append(f)
    return jsonify({"categories": feeds})

CUSTOM_FEEDS_PATH = "/app/custom_feeds.json"

def _load_custom_feeds():
    """Load user-defined custom feeds from JSON."""
    if os.path.exists(CUSTOM_FEEDS_PATH):
        try:
            with open(CUSTOM_FEEDS_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"feeds": []}

def _save_custom_feeds(data):
    """Persist user-defined custom feeds."""
    os.makedirs(os.path.dirname(CUSTOM_FEEDS_PATH) or ".", exist_ok=True)
    with open(CUSTOM_FEEDS_PATH, "w") as f:
        json.dump(data, f, indent=2)

@app.route("/api/rss/custom", methods=["GET"])
def api_rss_custom_get():
    return jsonify(_load_custom_feeds())

@app.route("/api/rss/custom", methods=["POST"])
def api_rss_custom_post():
    try:
        data = _load_custom_feeds()
        new_feed = request.json or {}
        if not new_feed.get("name") or not new_feed.get("url"):
            return jsonify({"error": "name and url required"}), 400
        # Remove any existing feed with same name
        data["feeds"] = [f for f in data["feeds"] if f.get("name") != new_feed["name"]]
        data["feeds"].append(new_feed)
        _save_custom_feeds(data)
        return jsonify({"status": "saved"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/rss/custom/<name>", methods=["DELETE"])
def api_rss_custom_delete(name):
    try:
        data = _load_custom_feeds()
        data["feeds"] = [f for f in data["feeds"] if f.get("name") != name]
        _save_custom_feeds(data)
        return jsonify({"status": "deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/twitter/webviewer")
def api_twitter_webviewer():
    """Proxy twitterwebviewer.com, stripping X-Frame-Options so we can embed it."""
    import re as _re_tw
    handle = request.args.get("handle", "").strip()
    # Strip any leading non-word prefix (e.g. "𝕏 @", "X @", emoji + space)
    handle = _re_tw.sub(r'^[^\w]+', '', handle).strip().lstrip("@")
    if not handle:
        return "handle required", 400
    import urllib.request as _ur
    # twitterwebviewer.com URL format
    target_url = f"https://twitterwebviewer.com/?user={handle}"
    try:
        req = _ur.Request(target_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://twitterwebviewer.com/",
        })
        with _ur.urlopen(req, timeout=15) as r:
            content = r.read()
            content_type = r.headers.get("Content-Type", "text/html; charset=utf-8")
        # Rewrite relative URLs and base href so resources load from twitterwebviewer.com
        html = content.decode("utf-8", errors="replace")
        if "<base " not in html.lower():
            html = html.replace("<head>", '<head><base href="https://twitterwebviewer.com/">', 1)
            html = html.replace("<HEAD>", '<HEAD><base href="https://twitterwebviewer.com/">', 1)
        from flask import Response
        resp = Response(html, status=200, mimetype="text/html")
        # Do NOT set X-Frame-Options so our iframe can load it
        return resp
    except Exception as e:
        return f"<html><body style='font-family:sans-serif;padding:20px;background:#111;color:#ccc'><p>Could not load twitterwebviewer.com: {str(e)}</p><p><a href='https://twitterwebviewer.com/?user={handle}' target='_blank' style='color:#1d9bf0'>Open directly ↗</a></p></body></html>", 200

@app.route("/api/twitter/feed")
def api_twitter_feed():
    """Fetch a Twitter/X handle's recent posts via public nitter RSS instances."""
    import urllib.request as _ur
    handle = request.args.get("handle", "").strip().lstrip("@")
    if not handle:
        return jsonify({"error": "Missing handle", "items": []}), 400
    cache_key = f"twitter_nitter_{handle}"
    if cache_key in _rss_cache and time.time() - _rss_cache[cache_key].get("ts", 0) < 300:
        return jsonify(_rss_cache[cache_key]["data"])
    nitter_instances = [
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
        "https://nitter.cz",
        "https://nitter.1d4.us",
        "https://nitter.lunar.icu",
    ]
    last_err = "All nitter instances failed"
    for instance in nitter_instances:
        try:
            rss_url = f"{instance}/{handle}/rss"
            req = _ur.Request(rss_url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
                "Cache-Control": "no-cache",
            })
            with _ur.urlopen(req, timeout=8) as resp:
                raw = resp.read()
            import xml.etree.ElementTree as _ET2, html as _hm2, re as _re2
            root = _ET2.fromstring(raw)
            ns = {"atom": "http://www.w3.org/2005/Atom", "media": "http://search.yahoo.com/mrss/"}
            items = []
            for item in root.findall(".//item")[:20]:
                title_el = item.find("title")
                link_el  = item.find("link")
                date_el  = item.find("pubDate")
                desc_el  = item.find("description")
                title = (title_el.text or "").strip() if title_el is not None else ""
                link  = (link_el.text  or "").strip() if link_el  is not None else "#"
                # Fix nitter links to point to real twitter.com
                link = link.replace(instance, "https://twitter.com").replace("//twitter.com/", "//x.com/")
                date  = (date_el.text  or "")[:16]  if date_el  is not None else ""
                desc  = ""
                if desc_el is not None and desc_el.text:
                    desc = _re2.sub(r"<[^>]+>", " ", _hm2.unescape(desc_el.text))
                    desc = _re2.sub(r"\s+", " ", desc).strip()[:200]
                # Strip html from title too
                title = _re2.sub(r"<[^>]+>", " ", _hm2.unescape(title)).strip()
                if not title:
                    title = desc[:80] or "Tweet"
                items.append({"title": title, "link": link, "date": date, "excerpt": desc})
            result = {"items": items, "instance": instance}
            _rss_cache[cache_key] = {"data": result, "ts": time.time()}
            resp_obj = jsonify(result)
            resp_obj.headers["Cache-Control"] = "no-cache"
            return resp_obj
        except Exception as e:
            last_err = str(e)
            continue
    return jsonify({"error": last_err, "items": []})

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

    # Per-feed response cache — short TTL to keep content fresh
    bust = request.args.get("bust", "0")  # ?bust=1 forces bypass
    cache_key = "rss_fetch_" + url
    if bust == "0" and cache_key in _rss_cache and (time.time() - _rss_cache[cache_key].get("ts", 0)) < 120:
        return jsonify(_rss_cache[cache_key]["data"])

    try:
        import urllib.request, re as _re, html as _html_mod, datetime as _dt
        is_reddit = "reddit.com" in url

        # ── Reddit: use JSON API (much more reliable than RSS from servers) ──
        if is_reddit:
            m = _re.search(r'reddit\.com/r/([A-Za-z0-9_]+)', url)
            if m:
                subreddit = m.group(1)
                json_url = f"https://www.reddit.com/r/{subreddit}.json?limit=25&raw_json=1&include_over_18=1"
                req_json = urllib.request.Request(json_url, headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.reddit.com/",
                    "Cookie": "over18=1; reddit_session=; redesign_optout=true",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                    "DNT": "1",
                })
                with urllib.request.urlopen(req_json, timeout=15) as resp_j:
                    rdata = json.loads(resp_j.read())
                posts = rdata.get("data", {}).get("children", [])
                reddit_items = []
                for post in posts[:25]:
                    pd = post.get("data", {})
                    title = pd.get("title", "Untitled")
                    permalink = "https://www.reddit.com" + pd.get("permalink", "#")
                    post_url = pd.get("url", permalink)
                    created = pd.get("created_utc")
                    date = _dt.datetime.utcfromtimestamp(created).strftime("%Y-%m-%d %H:%M") if created else ""

                    # ── Detect post type ────────────────────────────────────
                    post_hint = pd.get("post_hint", "")
                    is_video = pd.get("is_video", False)
                    is_gallery = pd.get("is_gallery", False)
                    domain = pd.get("domain", "")
                    # v.redd.it, youtube, streamable, etc.
                    is_video_link = is_video or post_hint == "rich:video" or "v.redd.it" in domain or "youtube.com" in domain or "youtu.be" in domain or "streamable.com" in domain
                    is_image = post_hint == "image" or (post_url or "").lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".gifv"))
                    is_gif = (post_url or "").lower().endswith((".gif", ".gifv")) or "i.imgur.com" in domain

                    # ── Thumbnail — best quality source first ────────────────
                    thumb = None
                    # 1. Preview images (highest quality, handles most types)
                    try:
                        imgs = pd["preview"]["images"]
                        if imgs:
                            # Try mp4 preview for GIFs (avoids huge GIF files)
                            if is_gif:
                                try:
                                    thumb = imgs[0]["variants"]["mp4"]["source"]["url"].replace("&amp;", "&")
                                except Exception:
                                    pass
                            if not thumb:
                                thumb = imgs[0]["source"]["url"].replace("&amp;", "&")
                    except Exception:
                        pass
                    # 2. Reddit video thumbnail
                    if not thumb and is_video:
                        try:
                            thumb = pd["media"]["reddit_video"]["fallback_url"].replace("&amp;", "&").split("?")[0].rsplit("/", 1)[0] + "/DASH_480.mp4"
                        except Exception:
                            pass
                    # 3. Gallery first image
                    if not thumb and is_gallery:
                        try:
                            first_id = list(pd["media_metadata"].keys())[0]
                            m = pd["media_metadata"][first_id]
                            thumb = m["s"]["u"].replace("&amp;", "&")
                        except Exception:
                            pass
                    # 4. Direct image URL
                    if not thumb and is_image:
                        thumb = post_url
                    # 5. Fallback thumbnail
                    if not thumb:
                        tn = pd.get("thumbnail", "")
                        if tn and tn.startswith("http") and tn not in ("self", "default", "spoiler"):
                            thumb = tn

                    # ── Video URL for direct-play embed ─────────────────────
                    video_url = None
                    if is_video:
                        try:
                            video_url = pd["media"]["reddit_video"]["fallback_url"].replace("&amp;", "&")
                        except Exception:
                            pass

                    excerpt = (pd.get("selftext") or "")[:200]
                    flair = pd.get("link_flair_text") or ""
                    subreddit_name = pd.get("subreddit_name_prefixed", "")
                    score = pd.get("score", 0)
                    num_comments = pd.get("num_comments", 0)

                    reddit_items.append({
                        "title": title,
                        "link": permalink,
                        "post_url": post_url,
                        "date": date,
                        "thumb": thumb,
                        "excerpt": excerpt,
                        "post_type": "video" if is_video_link else ("gif" if is_gif else ("gallery" if is_gallery else ("image" if is_image else "text"))),
                        "video_url": video_url,
                        "flair": flair,
                        "score": score,
                        "num_comments": num_comments,
                        "subreddit": subreddit_name,
                    })
                result = {"items": reddit_items}
                _rss_cache[cache_key] = {"data": result, "ts": time.time()}
                return jsonify(result)

        # ── Non-Reddit: standard RSS/Atom fetch ───────────────────────────
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
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
            # 1. media:thumbnail (various namespace URIs)
            # 2. media:content — try ALL, prefer one with image medium/type
            # 3. enclosure with image MIME
            # 4. First <img> in description / content:encoded (handles feeds that embed HTML)
            # 5. YouTube thumbnail from videoId in link
            # 6. Regex scan the raw XML string for any media thumbnail URL (last resort)
            thumb = None
            # 1. media:thumbnail (various namespace URIs)
            for _mt_tag in [
                "media:thumbnail",
                "{http://search.yahoo.com/mrss/}thumbnail",
                "{http://search.yahoo.com/mrss}thumbnail",
            ]:
                _mt = item.find(_mt_tag) if _mt_tag.startswith("{") else item.find(_mt_tag, ns)
                if _mt is not None and _mt.get("url"):
                    thumb = _mt.get("url"); break
            # 2. media:content — try ALL, prefer one with image medium/type
            if not thumb:
                for _mc_tag in [
                    "media:content",
                    "{http://search.yahoo.com/mrss/}content",
                    "{http://search.yahoo.com/mrss}content",
                ]:
                    _mcs = item.findall(_mc_tag) if _mc_tag.startswith("{") else item.findall(_mc_tag, ns)
                    # prefer explicitly-typed image, fall back to any with a URL
                    _best = None
                    for _mc in _mcs:
                        _u = _mc.get("url","")
                        if not _u: continue
                        _med = _mc.get("medium","") + _mc.get("type","")
                        if "image" in _med:
                            _best = _u; break
                        if _best is None:
                            _best = _u
                    if _best:
                        thumb = _best; break
            # 3. enclosure with image MIME
            if not thumb:
                enc = item.find("enclosure")
                if enc is not None and "image" in (enc.get("type","") or ""):
                    thumb = enc.get("url")
            # 4. First <img> in description / content:encoded (handles feeds that embed HTML)
            if not thumb:
                for _tag in ["content:encoded", "description"]:
                    _raw_el = item.find(_tag, ns) or item.find(_tag)
                    if _raw_el is not None and _raw_el.text:
                        _img = _first_img(_raw_el.text)
                        if _img and _img.startswith("http"):
                            thumb = _img; break
            # 5. YouTube thumbnail from videoId in link
            if not thumb:
                yt_match = _re.search(r"[?&]v=([A-Za-z0-9_-]{11})", link)
                if yt_match:
                    thumb = f"https://i.ytimg.com/vi/{yt_match.group(1)}/mqdefault.jpg"
            # 6. Regex scan the raw XML string for any media thumbnail URL (last resort)
            if not thumb:
                try:
                    _item_str = ET.tostring(item, encoding="unicode")
                    _rm = _re.search(r'(?:thumbnail|media:content)[^>]+url=["\']([^"\']{10,})["\']', _item_str, _re.I)
                    if _rm:
                        _u = _rm.group(1)
                        if _u.startswith("http"):
                            thumb = _u
                except Exception:
                    pass
            # 7. Try extracting from full item XML — broader namespace patterns
            if not thumb:
                import re as _re_thumb
                _raw_item = ET.tostring(item, encoding='unicode', method='xml') if hasattr(item, 'tag') else ''
                # Match any url= attribute in media-related tags
                for _tp in [
                    r'<media:thumbnail[^>]+url=["\']([^"\']{10,})["\']',
                    r'<media:content[^>]+url=["\']([^"\']{10,})["\']',
                    r'<enclosure[^>]+url=["\']([^"\']{10,})["\']',
                    r'url=["\']([^"\']*(?:\.jpg|\.jpeg|\.png|\.webp)[^"\']*)["\']',
                ]:
                    _tm = _re_thumb.search(_tp, _raw_item, _re_thumb.I)
                    if _tm and _tm.group(1).startswith('http'):
                        thumb = _tm.group(1)
                        break

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
        resp = jsonify(result)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        return resp
    except Exception as e:
        return jsonify({"error": str(e), "items": []}), 200

def _iptv_get_custom():
    raw = _db_get("iptv_custom_channels", None)
    if raw:
        try: return json.loads(raw)
        except Exception: pass
    return []

@app.route("/api/iptv/channels")
def api_iptv_channels():
    """Return merged fallback + user-added channels."""
    custom = _iptv_get_custom()
    # Merge: custom channels first, then fallback (skip duplicates by id)
    custom_ids = {c["id"] for c in custom}
    merged = custom + [c for c in _IPTV_FALLBACK_CHANNELS if c["id"] not in custom_ids]
    return jsonify({"channels": merged, "count": len(merged)})

@app.route("/api/iptv/channels/custom", methods=["POST"])
def api_iptv_add_custom_channel():
    data = request.get_json() or {}
    ch_id   = (data.get("id") or "").strip()
    ch_name = (data.get("name") or "").strip()
    ch_group = (data.get("group") or "Custom").strip()
    if not ch_id or not ch_name:
        return jsonify({"error": "Missing id or name"}), 400
    channels = _iptv_get_custom()
    if not any(c["id"] == ch_id for c in channels):
        channels.append({"id": ch_id, "name": ch_name, "group": ch_group, "logo": "", "custom": True})
        _db_set("iptv_custom_channels", json.dumps(channels))
    return jsonify({"ok": True})

@app.route("/api/iptv/channels/custom/<ch_id>", methods=["DELETE"])
def api_iptv_delete_custom_channel(ch_id):
    channels = _iptv_get_custom()
    channels = [c for c in channels if c["id"] != ch_id]
    _db_set("iptv_custom_channels", json.dumps(channels))
    return jsonify({"ok": True})

@app.route("/api/iptv/schedule")
def api_iptv_schedule():
    """Proxy live-sports schedule from streamed.su."""
    endpoint = request.args.get("type", "live")  # live | all
    url = f"https://streamed.su/api/matches/{endpoint}"
    try:
        import requests as _req
        r = _req.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; ArrHub/3.15)"}, timeout=4)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e), "matches": []}), 200

# Channel list — streams via live.moviebite.cc/channels/{slug}
_IPTV_FALLBACK_CHANNELS = [
    # ── News ────────────────────────────────────────────────────────────────
    {"id":"CNN",            "name":"CNN",                  "group":"News",          "logo":""},
    {"id":"FOX-NEWS",       "name":"Fox News",             "group":"News",          "logo":""},
    {"id":"MSNBC",          "name":"MSNBC",                "group":"News",          "logo":""},
    {"id":"CNBC",           "name":"CNBC",                 "group":"News",          "logo":""},
    {"id":"NBC-NEWS",       "name":"NBC News",             "group":"News",          "logo":""},
    {"id":"ABC-NEWS",       "name":"ABC News",             "group":"News",          "logo":""},
    {"id":"CBS-NEWS",       "name":"CBS News",             "group":"News",          "logo":""},
    {"id":"BBC-NEWS",       "name":"BBC News",             "group":"News",          "logo":""},
    {"id":"SKY-NEWS",       "name":"Sky News",             "group":"News",          "logo":""},
    {"id":"BLOOMBERG",      "name":"Bloomberg TV",         "group":"News",          "logo":""},
    {"id":"AL-JAZEERA",     "name":"Al Jazeera English",   "group":"News",          "logo":""},
    {"id":"CNN-INT",        "name":"CNN International",    "group":"News",          "logo":""},
    {"id":"DW-NEWS",        "name":"DW News",              "group":"News",          "logo":""},
    {"id":"EURONEWS",       "name":"Euronews English",     "group":"News",          "logo":""},
    # ── Sports ──────────────────────────────────────────────────────────────
    {"id":"ESPN",           "name":"ESPN",                 "group":"Sports",        "logo":""},
    {"id":"ESPN2",          "name":"ESPN2",                "group":"Sports",        "logo":""},
    {"id":"ESPN-NEWS",      "name":"ESPN News",            "group":"Sports",        "logo":""},
    {"id":"FS1",            "name":"Fox Sports 1",         "group":"Sports",        "logo":""},
    {"id":"FS2",            "name":"Fox Sports 2",         "group":"Sports",        "logo":""},
    {"id":"NBA-TV",         "name":"NBA TV",               "group":"Sports",        "logo":""},
    {"id":"NFL-NETWORK",    "name":"NFL Network",          "group":"Sports",        "logo":""},
    {"id":"MLB-NETWORK",    "name":"MLB Network",          "group":"Sports",        "logo":""},
    {"id":"GOLF-CHANNEL",   "name":"Golf Channel",         "group":"Sports",        "logo":""},
    {"id":"SKY-SPORTS",     "name":"Sky Sports Main",      "group":"Sports",        "logo":""},
    {"id":"SKY-SPORTS-NEWS","name":"Sky Sports News",      "group":"Sports",        "logo":""},
    {"id":"SKY-SPORTS-F1",  "name":"Sky Sports F1",        "group":"Sports",        "logo":""},
    {"id":"BEIN-SPORTS-1",  "name":"beIN Sports 1",        "group":"Sports",        "logo":""},
    {"id":"BEIN-SPORTS-2",  "name":"beIN Sports 2",        "group":"Sports",        "logo":""},
    {"id":"EUROSPORT-1",    "name":"Eurosport 1",          "group":"Sports",        "logo":""},
    {"id":"DAZN-1",         "name":"DAZN 1",               "group":"Sports",        "logo":""},
    {"id":"TNT-SPORTS-1",   "name":"TNT Sports 1",         "group":"Sports",        "logo":""},
    {"id":"TNT-SPORTS-2",   "name":"TNT Sports 2",         "group":"Sports",        "logo":""},
    # ── Entertainment ───────────────────────────────────────────────────────
    {"id":"TNT",            "name":"TNT",                  "group":"Entertainment", "logo":""},
    {"id":"AMC",            "name":"AMC",                  "group":"Entertainment", "logo":""},
    {"id":"FX",             "name":"FX",                   "group":"Entertainment", "logo":""},
    {"id":"SYFY",           "name":"Syfy",                 "group":"Entertainment", "logo":""},
    {"id":"COMEDY-CENTRAL", "name":"Comedy Central",       "group":"Entertainment", "logo":""},
    {"id":"DISCOVERY",      "name":"Discovery Channel",    "group":"Entertainment", "logo":""},
    {"id":"DISCOVERY-SCI",  "name":"Discovery Science",    "group":"Entertainment", "logo":""},
    {"id":"HISTORY",        "name":"History Channel",      "group":"Entertainment", "logo":""},
    {"id":"TLC",            "name":"TLC",                  "group":"Entertainment", "logo":""},
    {"id":"NATGEO",         "name":"National Geographic",  "group":"Entertainment", "logo":""},
    {"id":"NATGEO-WILD",    "name":"Nat Geo Wild",         "group":"Entertainment", "logo":""},
    {"id":"ANIMAL-PLANET",  "name":"Animal Planet",        "group":"Entertainment", "logo":""},
    {"id":"CARTOON-NETWORK","name":"Cartoon Network",      "group":"Entertainment", "logo":""},
    {"id":"NICKELODEON",    "name":"Nickelodeon",          "group":"Entertainment", "logo":""},
    {"id":"DISNEY-CHANNEL", "name":"Disney Channel",       "group":"Entertainment", "logo":""},
    {"id":"NASA-TV",        "name":"NASA TV",              "group":"Entertainment", "logo":""},
    {"id":"E-ENTERTAINMENT","name":"E! Entertainment",     "group":"Entertainment", "logo":""},
    {"id":"BRAVO",          "name":"Bravo",                "group":"Entertainment", "logo":""},
    # ── Movies ──────────────────────────────────────────────────────────────
    {"id":"HBO",            "name":"HBO",                  "group":"Movies",        "logo":""},
    {"id":"SHOWTIME",       "name":"Showtime",             "group":"Movies",        "logo":""},
    {"id":"STARZ",          "name":"Starz",                "group":"Movies",        "logo":""},
    {"id":"CINEMAX",        "name":"Cinemax",              "group":"Movies",        "logo":""},
    {"id":"SKY-CINEMA",     "name":"Sky Cinema Premiere",  "group":"Movies",        "logo":""},
    # ── Music ───────────────────────────────────────────────────────────────
    {"id":"MTV",            "name":"MTV",                  "group":"Music",         "logo":""},
    {"id":"VH1",            "name":"VH1",                  "group":"Music",         "logo":""},
    {"id":"BET",            "name":"BET",                  "group":"Music",         "logo":""},
    {"id":"FUSE",           "name":"Fuse TV",              "group":"Music",         "logo":""},
    # ── International ───────────────────────────────────────────────────────
    {"id":"BBC-ONE",        "name":"BBC One",              "group":"International", "logo":""},
    {"id":"BBC-TWO",        "name":"BBC Two",              "group":"International", "logo":""},
    {"id":"ITV",            "name":"ITV",                  "group":"International", "logo":""},
    {"id":"CHANNEL-4",      "name":"Channel 4 UK",         "group":"International", "logo":""},
    {"id":"ARD",            "name":"ARD Germany",          "group":"International", "logo":""},
    {"id":"ZDF",            "name":"ZDF Germany",          "group":"International", "logo":""},
    {"id":"RAI-1",          "name":"RAI 1 Italy",          "group":"International", "logo":""},
    {"id":"TF1",            "name":"TF1 France",           "group":"International", "logo":""},
    {"id":"CBC-CANADA",     "name":"CBC Canada",           "group":"International", "logo":""},
    {"id":"CTV-CANADA",     "name":"CTV Canada",           "group":"International", "logo":""},
    {"id":"TSN-1",          "name":"TSN 1",                "group":"International", "logo":""},
]

# ── Feeds subscription store ────────────────────────────────────────────
def _feeds_get_subs():
    """Return saved feed subscriptions from DB, with sensible defaults.
    Also migrates existing saves to include news feeds if missing."""
    _NEWS_DEFAULTS = [
        {"id": "cnn",        "name": "CNN",         "url": "http://rss.cnn.com/rss/edition.rss"},
        {"id": "wsj_world",  "name": "WSJ World",   "url": "https://feeds.a.dj.com/rss/RSSWorldNews.xml"},
        {"id": "bbc_main",   "name": "BBC News",    "url": "https://feeds.bbci.co.uk/news/rss.xml"},
        {"id": "aljazeera",  "name": "Al Jazeera",  "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    ]
    _DEFAULT = {
        "_type_meta": {
            "rss":     {"name": "RSS",     "icon": "📰"},
            "reddit":  {"name": "Reddit",  "icon": "🤖"},
            "youtube": {"name": "YouTube", "icon": "▶"},
            "twitter": {"name": "Twitter", "icon": "𝕏"},
        },
        "rss": [
            {"id": "selfhst",    "name": "selfh.st",       "url": "https://selfh.st/rss/"},
            {"id": "lsio",       "name": "linuxserver.io",  "url": "https://blog.linuxserver.io/feed/"},
            {"id": "theverge",   "name": "The Verge",       "url": "https://www.theverge.com/rss/index.xml"},
            {"id": "hn",         "name": "Hacker News",     "url": "https://hnrss.org/frontpage"},
            {"id": "arstechnica","name": "Ars Technica",    "url": "https://feeds.arstechnica.com/arstechnica/index"},
        ] + _NEWS_DEFAULTS,
        "reddit": [
            {"id": "homelab",    "name": "r/homelab",       "url": "https://www.reddit.com/r/homelab/.rss"},
            {"id": "selfhosted", "name": "r/selfhosted",    "url": "https://www.reddit.com/r/selfhosted/.rss"},
            {"id": "proxmox",    "name": "r/Proxmox",       "url": "https://www.reddit.com/r/Proxmox/.rss"},
            {"id": "docker",     "name": "r/docker",        "url": "https://www.reddit.com/r/docker/.rss"},
        ],
        "youtube": [
            {"id": "UCR-DXc1voovS8nhAvccRZhg", "name": "Jeff Geerling",  "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCR-DXc1voovS8nhAvccRZhg"},
            {"id": "UCsBjURrPoezykLs9EqgamOA", "name": "Fireship",       "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCsBjURrPoezykLs9EqgamOA"},
            {"id": "UCVS-4mLrAKFNZWoZ4eHiYbA", "name": "NetworkChuck",  "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCVS-4mLrAKFNZWoZ4eHiYbA"},
        ],
        "twitter": [
            {"id": "jeffgeerling",  "name": "@JeffGeerling",  "url": "JeffGeerling"},
            {"id": "networkchuck",  "name": "@NetworkChuck",  "url": "NetworkChuck"},
            {"id": "theprimagen",   "name": "@ThePrimeagen",  "url": "ThePrimeagen"},
            {"id": "linustech",     "name": "@LinusTech",     "url": "LinusTech"},
        ]
    }
    raw = _db_get("feeds_subscriptions", None)
    if not raw:
        return _DEFAULT
    try:
        subs = json.loads(raw)
        # Migrate: add news feeds if not already present
        existing_ids = {s["id"] for s in subs.get("rss", [])}
        added = False
        for feed in _NEWS_DEFAULTS:
            if feed["id"] not in existing_ids:
                subs.setdefault("rss", []).append(feed)
                added = True
        # Migrate: add twitter defaults if not present
        if "twitter" not in subs:
            subs["twitter"] = _DEFAULT["twitter"]
            if "_type_meta" in subs:
                subs["_type_meta"]["twitter"] = {"name": "Twitter", "icon": "𝕏"}
            added = True
        if added:
            _db_set("feeds_subscriptions", json.dumps(subs))
        return subs
    except Exception:
        return _DEFAULT

@app.route("/api/feeds/subscriptions", methods=["GET"])
def api_feeds_get_subscriptions():
    return jsonify(_feeds_get_subs())

@app.route("/api/feeds/categories", methods=["POST"])
def api_feeds_add_category():
    """Create a new custom feed category type."""
    data = request.get_json() or {}
    cat_id   = (data.get("id") or "").strip().lower().replace(" ", "_")
    cat_name = (data.get("name") or "").strip()
    cat_icon = (data.get("icon") or "📡").strip()
    if not cat_id or not cat_name:
        return jsonify({"error": "Missing id or name"}), 400
    if cat_id in ("rss", "reddit", "youtube", "_type_meta"):
        return jsonify({"error": "Reserved category name"}), 400
    subs = _feeds_get_subs()
    meta = subs.setdefault("_type_meta", {})
    if cat_id not in meta:
        meta[cat_id] = {"name": cat_name, "icon": cat_icon}
    if cat_id not in subs:
        subs[cat_id] = []
    _db_set("feeds_subscriptions", json.dumps(subs))
    return jsonify({"ok": True})

@app.route("/api/feeds/categories/<cat_id>", methods=["DELETE"])
def api_feeds_delete_category(cat_id):
    """Delete a custom feed category type and all its subscriptions."""
    if cat_id in ("rss", "reddit", "youtube"):
        return jsonify({"error": "Cannot delete built-in categories"}), 400
    subs = _feeds_get_subs()
    subs.get("_type_meta", {}).pop(cat_id, None)
    subs.pop(cat_id, None)
    _db_set("feeds_subscriptions", json.dumps(subs))
    return jsonify({"ok": True})

@app.route("/api/feeds/subscriptions", methods=["POST"])
def api_feeds_add_subscription():
    data = request.get_json() or {}
    sub_type = data.get("type", "")
    sub_id   = (data.get("id") or "").strip()
    sub_name = (data.get("name") or "").strip()
    sub_url  = (data.get("url") or "").strip()
    if not sub_type or not sub_id or not sub_name or not sub_url:
        return jsonify({"error": "Missing fields"}), 400
    subs = _feeds_get_subs()
    # Ensure the type exists in subs (custom types may not have been initialised yet)
    if sub_type not in subs:
        subs[sub_type] = []
    if not any(s["id"] == sub_id for s in subs[sub_type]):
        subs[sub_type].append({"id": sub_id, "name": sub_name, "url": sub_url})
        _db_set("feeds_subscriptions", json.dumps(subs))
    return jsonify({"ok": True})

@app.route("/api/feeds/subscriptions/<sub_type>/<path:sub_id>", methods=["DELETE"])
def api_feeds_delete_subscription(sub_type, sub_id):
    subs = _feeds_get_subs()
    if sub_type in subs:
        subs[sub_type] = [s for s in subs[sub_type] if s["id"] != sub_id]
        _db_set("feeds_subscriptions", json.dumps(subs))
    return jsonify({"ok": True})

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
            "selfh.st": "https://selfh.st/rss/"
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

_og_cache: dict = {}

@app.route("/api/feeds/og")
def api_feeds_og():
    """Fetch og:image from an article URL for thumbnail enrichment."""
    import urllib.request as _ur2, re as _re_og
    url = request.args.get("url", "").strip()
    if not url or not url.startswith("http"):
        return jsonify({"img": None})
    cache_key = "og_" + url
    if cache_key in _og_cache and time.time() - _og_cache[cache_key].get("ts", 0) < 7200:
        return jsonify(_og_cache[cache_key]["data"])
    try:
        req = _ur2.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
        })
        with _ur2.urlopen(req, timeout=10) as r:
            # Only read first 80KB — og:image is always in <head>
            html = r.read(80000).decode("utf-8", "ignore")
        # Extract og:image / twitter:image — handle both quote styles
        img = None
        patterns = [
            r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\'<>]{10,})["\']',
            r'<meta[^>]+content=["\']([^"\'<>]{10,})["\'][^>]+property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\'<>]{10,})["\']',
            r'<meta[^>]+content=["\']([^"\'<>]{10,})["\'][^>]+name=["\']twitter:image["\']',
            # Some sites use data attributes or JSON-LD with imageUrl
            r'"thumbnailUrl"\s*:\s*"([^"]{10,})"',
            r'"image"\s*:\s*\{"@type"[^}]+"url"\s*:\s*"([^"]{10,})"',
        ]
        for pat in patterns:
            m = _re_og.search(pat, html, _re_og.I)
            if m:
                candidate = m.group(1).strip()
                if candidate.startswith("http") and len(candidate) > 10:
                    img = candidate
                    break
        result = {"img": img}
        _og_cache[cache_key] = {"data": result, "ts": time.time()}
        return jsonify(result)
    except Exception as e:
        return jsonify({"img": None, "error": str(e)[:80]})

@app.route("/api/reddit/feed")
def api_reddit_feed():
    """Server-side Reddit proxy — session login (username+password only), OAuth fallback, then anonymous."""
    import urllib.request as _ur, urllib.parse as _up, json as _jr, base64 as _b64, http.cookiejar as _cj
    sub   = request.args.get("sub",   "").strip()
    sort  = request.args.get("sort",  "hot").strip()
    after = request.args.get("after", "").strip()
    limit = min(int(request.args.get("limit", "25")), 100)
    if not sub:
        return jsonify({"error": "Missing subreddit"}), 400
    cache_key = f"reddit_{sub}_{sort}_{after}_{limit}"
    if cache_key in _rss_cache and time.time() - _rss_cache[cache_key].get("ts", 0) < 120:
        return jsonify(_rss_cache[cache_key]["data"])

    client_id     = _get_setting("reddit_client_id",     "").strip()
    client_secret = _get_setting("reddit_client_secret", "").strip()
    reddit_user   = _get_setting("reddit_username",      "").strip()
    reddit_pass   = _get_setting("reddit_password",      "").strip()
    app_ua = f"ArrHub:v1 (by /u/{reddit_user or 'homelab_user'})"
    browser_ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

    session_error = None
    oauth_error   = None

    # --- Try 1: Cookie session login (username + password only — no API app needed) ---
    # Reddit requires a pre-existing anonymous loid/session cookie before it accepts login POSTs.
    # We GET the Reddit homepage first with a CookieJar so Reddit sets those cookies, then POST login.
    _browser_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    if reddit_user and reddit_pass:
        try:
            session_cache_key = f"reddit_session_{reddit_user}"
            session_cookie = None
            if session_cache_key in _rss_cache and time.time() - _rss_cache[session_cache_key].get("ts", 0) < 3600:
                session_cookie = _rss_cache[session_cache_key]["data"]
            if not session_cookie:
                jar = _cj.CookieJar()
                opener = _ur.build_opener(_ur.HTTPCookieProcessor(jar))
                # Step 1: GET Reddit homepage to obtain anonymous loid/session cookies
                try:
                    seed_req = _ur.Request("https://www.reddit.com/", headers={
                        "User-Agent": _browser_ua,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.5",
                    })
                    with opener.open(seed_req, timeout=10) as _sr:
                        _sr.read()
                except Exception:
                    pass  # Even if this fails, proceed — some cookies may have been set
                # Step 2: POST login with browser-like headers and the loid cookies now in jar
                login_payload = _up.urlencode({"user": reddit_user, "passwd": reddit_pass, "api_type": "json"}).encode()
                for login_url in ["https://www.reddit.com/api/login", "https://old.reddit.com/api/login"]:
                    try:
                        login_req = _ur.Request(
                            login_url,
                            data=login_payload,
                            headers={
                                "User-Agent": _browser_ua,
                                "Content-Type": "application/x-www-form-urlencoded",
                                "Accept": "application/json",
                                "Origin": "https://www.reddit.com",
                                "Referer": "https://www.reddit.com/login",
                            }
                        )
                        with opener.open(login_req, timeout=12) as lr:
                            resp = _jr.loads(lr.read())
                        errors = resp.get("json", {}).get("errors", [])
                        if errors:
                            session_error = f"Login failed: {errors[0][1] if errors[0] else errors}"
                        else:
                            for ck in jar:
                                if ck.name in ("reddit_session", "token_v2"):
                                    session_cookie = ck.value
                                    _rss_cache[session_cache_key] = {"data": session_cookie, "ts": time.time()}
                                    break
                        if session_cookie:
                            break
                    except Exception as _le:
                        session_error = f"Session login failed ({login_url.split('/')[2]}): {str(_le)[:100]}"
            if session_cookie:
                fetch_url = f"https://www.reddit.com/r/{sub}/{sort}.json?limit={limit}&raw_json=1&include_over_18=1"
                if after: fetch_url += f"&after={after}"
                fetch_req = _ur.Request(fetch_url, headers={
                    "User-Agent": _browser_ua,
                    "Cookie": f"reddit_session={session_cookie}; over18=1",
                    "Accept": "application/json",
                })
                with _ur.urlopen(fetch_req, timeout=15) as r:
                    data = _jr.loads(r.read())
                if isinstance(data, dict) and data.get("data"):
                    result = {"data": data["data"], "ok": True, "method": "session"}
                    _rss_cache[cache_key] = {"data": result, "ts": time.time()}
                    return jsonify(result)
        except Exception as e:
            session_error = f"Session login failed: {str(e)[:120]}"

    # --- Try 2: OAuth password grant (script app — needs client_id + secret + username + password) ---
    if client_id and client_secret and reddit_user and reddit_pass:
        try:
            token_cache_key = f"reddit_token_{client_id}"
            token = None
            if token_cache_key in _rss_cache and time.time() - _rss_cache[token_cache_key].get("ts", 0) < 3300:
                token = _rss_cache[token_cache_key]["data"].get("access_token")
            if not token:
                creds = _b64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
                post_data = _up.urlencode({"grant_type": "password", "username": reddit_user, "password": reddit_pass}).encode()
                tok_req = _ur.Request(
                    "https://www.reddit.com/api/v1/access_token",
                    data=post_data,
                    headers={"Authorization": f"Basic {creds}", "User-Agent": app_ua,
                             "Content-Type": "application/x-www-form-urlencoded"}
                )
                with _ur.urlopen(tok_req, timeout=10) as tr:
                    tok_data = _jr.loads(tr.read())
                if tok_data.get("error"):
                    oauth_error = f"OAuth error: {tok_data.get('error')}"
                else:
                    token = tok_data.get("access_token")
                    _rss_cache[token_cache_key] = {"data": tok_data, "ts": time.time()}
            if token:
                url = f"https://oauth.reddit.com/r/{sub}/{sort}.json?limit={limit}&raw_json=1"
                if after: url += f"&after={after}"
                api_req = _ur.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": app_ua})
                with _ur.urlopen(api_req, timeout=15) as r:
                    data = _jr.loads(r.read())
                if isinstance(data, dict) and data.get("data"):
                    result = {"data": data["data"], "ok": True, "method": "oauth"}
                    _rss_cache[cache_key] = {"data": result, "ts": time.time()}
                    return jsonify(result)
        except Exception as e:
            oauth_error = f"OAuth failed: {str(e)[:100]}"

    # --- Try 3: Anonymous JSON API (old.reddit.com, then www) ---
    browser_ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    base_urls = [
        f"https://old.reddit.com/r/{sub}/{sort}.json?limit={limit}&raw_json=1&over18=1",
        f"https://www.reddit.com/r/{sub}/{sort}.json?limit={limit}&raw_json=1&include_over_18=1",
    ]
    anon_err = None
    for base_url in base_urls:
        url = base_url + (f"&after={after}" if after else "")
        try:
            req = _ur.Request(url, headers={
                "User-Agent": browser_ua,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "en-US,en;q=0.9",
                "Cookie": "over18=1; _options=%7B%22pref_over_18%22%3A%20true%7D; redesign_optout=true",
                "Cache-Control": "no-cache",
                "Referer": "https://www.reddit.com/",
            })
            with _ur.urlopen(req, timeout=15) as r:
                raw = r.read()
            data = _jr.loads(raw)
            if isinstance(data, dict) and data.get("data"):
                result = {"data": data["data"], "ok": True, "method": "anonymous"}
                _rss_cache[cache_key] = {"data": result, "ts": time.time()}
                return jsonify(result)
        except Exception as exc:
            anon_err = str(exc)[:120]

    # --- Try 4: Reddit RSS feed as last resort ---
    try:
        import xml.etree.ElementTree as _et
        rss_url = f"https://www.reddit.com/r/{sub}/{sort}/.rss?limit={limit}"
        req = _ur.Request(rss_url, headers={"User-Agent": browser_ua, "Cookie": "over18=1"})
        with _ur.urlopen(req, timeout=15) as r:
            rss_content = r.read()
        root = _et.fromstring(rss_content)
        ns = {'atom': 'http://www.w3.org/2005/Atom'}
        entries = root.findall('.//atom:entry', ns) or root.findall('.//entry')
        if entries:
            children = []
            for entry in entries[:limit]:
                title_el = entry.find('atom:title', ns) or entry.find('title')
                link_el = entry.find('atom:link', ns) or entry.find('link')
                content_el = entry.find('atom:content', ns) or entry.find('content')
                author_el = entry.find('atom:author/atom:name', ns)
                updated_el = entry.find('atom:updated', ns) or entry.find('updated')
                link_href = link_el.get('href', '') if link_el is not None else ''
                permalink = link_href.replace('https://www.reddit.com', '')
                # Try to extract thumbnail from content HTML
                thumb = None
                content_html = content_el.text if content_el is not None else ''
                import re as _re_rss
                img_m = _re_rss.search(r'<img[^>]+src=["\']([^"\']+)["\']', content_html or '')
                if img_m:
                    thumb = img_m.group(1)
                children.append({"kind": "t3", "data": {
                    "title": title_el.text if title_el is not None else "Untitled",
                    "permalink": permalink,
                    "url": link_href,
                    "score": 0,
                    "num_comments": 0,
                    "created_utc": 0,
                    "thumbnail": thumb or "self",
                    "post_hint": "",
                    "is_video": False,
                    "is_gallery": False,
                    "domain": "",
                    "author": author_el.text if author_el is not None else "",
                }})
            result = {"data": {"children": children, "after": None}, "ok": True, "method": "rss"}
            _rss_cache[cache_key] = {"data": result, "ts": time.time()}
            return jsonify(result)
    except Exception:
        pass

    # All methods failed — give the most actionable error message
    if not reddit_user:
        error_msg = "Reddit username not set. Go to Settings → Reddit Login and enter your Reddit username and password. No app creation required."
    elif session_error and "WRONG_PASSWORD" in str(session_error):
        error_msg = "Wrong Reddit password. Check your credentials in Settings → Reddit Login."
    elif session_error and ("403" in str(session_error) or "Blocked" in str(session_error)):
        error_msg = ("Reddit is blocking the login request (403). This can happen if your username/password is wrong, "
                     "or if Reddit is temporarily blocking automated logins. "
                     "Try: 1) Double-check your password in Settings → Reddit Login. "
                     "2) If still blocked, create a free Reddit script app at reddit.com/prefs/apps "
                     "and enter the Client ID + Secret in the Advanced section of Settings → Reddit Login.")
    elif session_error:
        error_msg = f"Reddit login failed: {session_error}. Check your username/password in Settings → Reddit Login."
    elif anon_err and "403" in str(anon_err):
        error_msg = "This subreddit requires Reddit login (NSFW or restricted). Enter your credentials in Settings → Reddit Login."
    else:
        error_msg = session_error or oauth_error or anon_err or "All Reddit access methods failed. Check Settings → Reddit Login."
    return jsonify({"error": error_msg, "ok": False})

@app.route("/api/reddit/comments")
def api_reddit_comments():
    """Server-side proxy for Reddit comment threads."""
    import urllib.request as _ur, json as _jr
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "Missing url"}), 400
    try:
        req = _ur.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": "over18=1; redesign_optout=true",
            "Cache-Control": "no-cache",
            "Referer": "https://www.reddit.com/",
        })
        with _ur.urlopen(req, timeout=15) as r:
            data = _jr.loads(r.read())
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)[:120]})

# ── Premier League API (ESPN free endpoints — no API key needed) ────────
_epl_cache: dict = {}

@app.route("/api/epl/standings")
def api_epl_standings():
    """Fetch Premier League standings from ESPN free API."""
    import urllib.request as _ur, json as _jr
    cache_key = "epl_standings"
    if cache_key in _epl_cache and time.time() - _epl_cache[cache_key].get("ts", 0) < 900:
        return jsonify(_epl_cache[cache_key]["data"])
    try:
        req = _ur.Request(
            "https://site.api.espn.com/apis/v2/sports/soccer/eng.1/standings",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"}
        )
        with _ur.urlopen(req, timeout=12) as r:
            data = _jr.loads(r.read())
        standings = []
        entries = []
        for child in data.get("children", []):
            entries.extend(child.get("standings", {}).get("entries", []))
        if not entries:
            entries = data.get("standings", {}).get("entries", [])
        for entry in entries:
            team_info = entry.get("team", {})
            stats = {s["name"]: s.get("value", s.get("displayValue", 0)) for s in entry.get("stats", [])}
            standings.append({
                "pos": int(stats.get("rank", 0)),
                "team": team_info.get("shortDisplayName", team_info.get("displayName", "?")),
                "crest": team_info.get("logos", [{}])[0].get("href", "") if team_info.get("logos") else "",
                "played": int(stats.get("gamesPlayed", 0)),
                "won": int(stats.get("wins", 0)),
                "drawn": int(stats.get("ties", 0)),
                "lost": int(stats.get("losses", 0)),
                "gf": int(stats.get("pointsFor", 0)),
                "ga": int(stats.get("pointsAgainst", 0)),
                "gd": int(stats.get("pointDifferential", 0)),
                "pts": int(stats.get("points", 0)),
            })
        standings.sort(key=lambda x: x["pos"])
        result = {"standings": standings}
        _epl_cache[cache_key] = {"data": result, "ts": time.time()}
        return jsonify(result)
    except Exception as e:
        return jsonify({"standings": [], "error": str(e)[:120]})

@app.route("/api/epl/matches")
def api_epl_matches():
    """Fetch upcoming/recent matches. Tries football-data.org (if key set) then ESPN."""
    import urllib.request as _ur, json as _jr, datetime as _dt
    mtype  = request.args.get("type", "upcoming")  # upcoming | results
    league = request.args.get("league", "eng.1").strip()
    cache_key = f"epl_matches_{mtype}_{league}"
    if cache_key in _epl_cache and time.time() - _epl_cache[cache_key].get("ts", 0) < 600:
        return jsonify(_epl_cache[cache_key]["data"])

    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    today = _dt.date.today()

    # ── football-data.org league code map ──────────────────────────────
    _FDO_CODES = {
        "eng.1": "PL",  "esp.1": "PD",   "ger.1": "BL1",
        "ita.1": "SA",  "fra.1": "FL1",  "por.1": "PPL",
        "ned.1": "DED", "bel.1": "BSA",  "eur.1": "CL",
        "eur.2": "EL",  "int.world": "WC",
    }

    # ── Try football-data.org first if API key is configured ───────────
    fdo_key = _db_get("football_api_key", "")
    if fdo_key:
        try:
            fdo_code = _FDO_CODES.get(league)
            if fdo_code:
                status_filter = "FINISHED" if mtype == "results" else "SCHEDULED,LIVE"
                fdo_url = f"https://api.football-data.org/v4/competitions/{fdo_code}/matches?status={status_filter}&limit=50"
                fdo_req = _ur.Request(fdo_url, headers={"X-Auth-Token": fdo_key, "User-Agent": ua})
                with _ur.urlopen(fdo_req, timeout=10) as r:
                    fdo_data = _jr.loads(r.read())
                matches = []
                for m in fdo_data.get("matches", []):
                    home_team = m.get("homeTeam", {})
                    away_team = m.get("awayTeam", {})
                    score = m.get("score", {})
                    full = score.get("fullTime", {})
                    is_finished = m.get("status") == "FINISHED"
                    is_live     = m.get("status") in ("IN_PLAY", "PAUSED", "HALFTIME")
                    home_goals = full.get("home")
                    away_goals = full.get("away")
                    matches.append({
                        "id":       str(m.get("id", "")),
                        "home":     home_team.get("shortName", home_team.get("name", "?")),
                        "homeCrest":home_team.get("crest", ""),
                        "away":     away_team.get("shortName", away_team.get("name", "?")),
                        "awayCrest":away_team.get("crest", ""),
                        "date":     m.get("utcDate", ""),
                        "status":   "IN_PLAY" if is_live else ("FINISHED" if is_finished else "SCHEDULED"),
                        "scoreH":   home_goals,
                        "scoreA":   away_goals,
                        "matchday": m.get("matchday"),
                        "source":   "football-data.org",
                    })
                result = {"matches": matches, "source": "football-data.org"}
                _epl_cache[cache_key] = {"data": result, "ts": time.time()}
                return jsonify(result)
        except Exception:
            pass  # fall through to ESPN

    # ── ESPN fallback — use correct date-range format ──────────────────
    # ESPN scoreboard supports dates=YYYYMMDD-YYYYMMDD (range), NOT multiple params
    try:
        if mtype == "results":
            start = (today - _dt.timedelta(days=30)).strftime("%Y%m%d")
            end   = today.strftime("%Y%m%d")
        else:
            start = today.strftime("%Y%m%d")
            end   = (today + _dt.timedelta(days=60)).strftime("%Y%m%d")
        url = (f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}"
               f"/scoreboard?dates={start}-{end}&limit=200")
        req = _ur.Request(url, headers={"User-Agent": ua})
        with _ur.urlopen(req, timeout=12) as r:
            data = _jr.loads(r.read())
        matches = []
        for event in data.get("events", []):
            comp = event.get("competitions", [{}])[0]
            competitors = comp.get("competitors", [])
            home = next((c for c in competitors if c.get("homeAway") == "home"), {})
            away = next((c for c in competitors if c.get("homeAway") == "away"), {})
            home_team = home.get("team", {})
            away_team = away.get("team", {})
            status_obj  = comp.get("status", {}).get("type", {})
            status_name = status_obj.get("name", "")
            is_finished = status_name == "STATUS_FINAL"
            is_live     = status_name in ("STATUS_IN_PROGRESS", "STATUS_HALFTIME")
            if mtype == "results" and not is_finished:
                continue
            if mtype == "upcoming" and is_finished:
                continue
            matches.append({
                "id":       event.get("id"),
                "home":     home_team.get("shortDisplayName", home_team.get("displayName", "?")),
                "homeCrest":home_team.get("logo", ""),
                "away":     away_team.get("shortDisplayName", away_team.get("displayName", "?")),
                "awayCrest":away_team.get("logo", ""),
                "date":     event.get("date", ""),
                "status":   "IN_PLAY" if is_live else ("FINISHED" if is_finished else "SCHEDULED"),
                "scoreH":   int(home.get("score", 0)) if (is_finished or is_live) else None,
                "scoreA":   int(away.get("score", 0)) if (is_finished or is_live) else None,
                "matchday": None,
                "source":   "ESPN",
            })
        result = {"matches": matches, "source": "ESPN"}
        _epl_cache[cache_key] = {"data": result, "ts": time.time()}
        return jsonify(result)
    except Exception as e:
        return jsonify({"matches": [], "error": str(e)[:200]})

@app.route("/api/football/team_fixtures")
def api_football_team_fixtures():
    """Proxy ESPN team schedule server-side — tries multiple season/type combos for reliability."""
    import urllib.request as _ur, json as _jr, datetime as _dt
    team_id = request.args.get("team_id", "").strip()
    league  = request.args.get("league", "eng.1").strip()
    if not team_id:
        return jsonify({"error": "team_id required"}), 400
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    today = _dt.date.today()
    # Soccer seasons run Aug → May. ESPN indexes by START year.
    # Jan-Jul 2026 → 2025-26 season → start year 2025
    # Aug-Dec 2026 → 2026-27 season → start year 2026
    sy = today.year if today.month >= 8 else today.year - 1
    season_candidates = list(dict.fromkeys([sy, sy + 1, sy - 1]))  # current first
    # League competition name keywords — used to filter out cup/non-league matches after fetch
    _COMP_KW = {
        "eng.1": ["premier league", "premier", "barclays"],
        "esp.1": ["laliga", "la liga", "primera"],
        "ger.1": ["bundesliga"],
        "ita.1": ["serie a"],
        "fra.1": ["ligue 1"],
        "por.1": ["primeira liga", "liga nos"],
        "eur.1": ["champions league", "champions"],
        "eur.2": ["europa league", "europa"],
    }

    candidate_urls = []
    for s in season_candidates:
        # Try bare URL first (all competitions), then regular-season-only as backup
        candidate_urls.append(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/teams/{team_id}/schedule?season={s}")
        candidate_urls.append(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/teams/{team_id}/schedule?season={s}&seasontype=2")
    all_events = []
    seen_ids = set()
    for url in candidate_urls:
        try:
            req = _ur.Request(url, headers={"User-Agent": ua})
            with _ur.urlopen(req, timeout=10) as r:
                data = _jr.loads(r.read())
            events = data.get("events", [])
            for ev in events:
                eid = ev.get("id")
                if eid and eid not in seen_ids:
                    seen_ids.add(eid)
                    all_events.append(ev)
        except Exception:
            pass
    # Filter to league matches only (removes cup/secondary competition matches)
    _kws = _COMP_KW.get(league, [])
    if _kws and all_events:
        def _ev_matches_league(ev):
            comp = (ev.get("competitions") or [{}])[0]
            slug  = (ev.get("season") or {}).get("slug", "").lower()
            name  = (ev.get("name") or "").lower()
            notes = " ".join(
                (n.get("headline","") + " " + n.get("value",""))
                for n in (comp.get("notes") or [])
            ).lower()
            haystack = f"{slug} {name} {notes}"
            return any(kw.lower() in haystack for kw in _kws)
        filtered = [ev for ev in all_events if _ev_matches_league(ev)]
        if filtered:
            all_events = filtered  # Only use filtered set when it yields results

    # Sort by date ascending so upcoming matches appear after past ones
    all_events.sort(key=lambda ev: ev.get("date", ""))
    return jsonify({"events": all_events, "debug": {"total": len(all_events), "season_tried": sy}})

@app.route("/api/football/team_news")
def api_football_team_news():
    """Proxy ESPN team news server-side — tries multiple ESPN news endpoints."""
    import urllib.request as _ur, json as _jr
    team_id = request.args.get("team_id", "").strip()
    league  = request.args.get("league", "eng.1").strip()
    if not team_id:
        return jsonify({"error": "team_id required"}), 400
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    # Try multiple ESPN news endpoints — different leagues/teams use different paths
    news_urls = [
        f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/teams/{team_id}/news?limit=20",
        f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/news?team={team_id}&limit=20",
        f"https://now.core.api.espn.com/v1/sports/news?leagues={league}&teams={team_id}&limit=20",
    ]
    for url in news_urls:
        try:
            req = _ur.Request(url, headers={"User-Agent": ua})
            with _ur.urlopen(req, timeout=12) as r:
                data = _jr.loads(r.read())
            # ESPN returns articles under different keys depending on endpoint
            articles = (data.get("articles") or data.get("items") or
                        data.get("feed") or data.get("headlines") or [])
            if articles:
                return jsonify({"articles": articles})
        except Exception:
            pass
    return jsonify({"articles": [], "error": "No news available from ESPN at this time"})

@app.route("/api/epl/highlights")
def api_epl_highlights():
    """Fetch PL highlight videos from scorebat free API."""
    import urllib.request as _ur, json as _jr
    cache_key = "epl_highlights"
    if cache_key in _epl_cache and time.time() - _epl_cache[cache_key].get("ts", 0) < 1200:
        return jsonify(_epl_cache[cache_key]["data"])
    try:
        req = _ur.Request("https://www.scorebat.com/video-api/v3/feed/?token=free", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with _ur.urlopen(req, timeout=12) as r:
            all_vids = _jr.loads(r.read())
        # Filter for Premier League
        epl_keywords = ["premier league", "english premier", "epl"]
        highlights = []
        for v in (all_vids if isinstance(all_vids, list) else all_vids.get("response", [])):
            comp = (v.get("competition", "") or v.get("competitionName", "")).lower()
            if any(k in comp for k in epl_keywords):
                embed = ""
                for e in (v.get("videos", []) or []):
                    if e.get("embed"):
                        embed = e["embed"]
                        break
                highlights.append({
                    "title": v.get("title", ""),
                    "thumb": v.get("thumbnail", ""),
                    "embed": embed,
                    "date": v.get("date", ""),
                    "competition": v.get("competition", v.get("competitionName", "")),
                })
        result = {"highlights": highlights[:20]}
        _epl_cache[cache_key] = {"data": result, "ts": time.time()}
        return jsonify(result)
    except Exception as e:
        return jsonify({"highlights": [], "error": str(e)[:120]})

@app.route("/api/services/qbittorrent/torrents")  # legacy compat
@app.route("/api/services/downloader/torrents")
def api_downloader_torrents():
    """Unified downloader API — dispatches to qBittorrent, Transmission, or Deluge."""
    import urllib.request as _udl, urllib.parse as _uparse, http.cookiejar as _cj, json as _jdl
    dtype = _get_setting("downloader_type", "qbittorrent")

    def _fmt(b):
        try: b = float(b)
        except Exception: return "0 B"
        for u in ["B","KB","MB","GB","TB"]:
            if b < 1024: return f"{b:.1f} {u}"
            b /= 1024
        return f"{b:.1f} TB"

    # ── qBittorrent ──────────────────────────────────────────────────────
    if dtype == "qbittorrent":
        url  = _get_setting("qbittorrent_url",  "").rstrip("/")
        user = _get_setting("qbittorrent_user", "admin")
        pwd  = _get_setting("qbittorrent_pass", "adminadmin")
        if not url:
            return jsonify({"error": "qBittorrent URL not configured", "torrents": []})
        try:
            jar = _cj.CookieJar()
            opener = _udl.build_opener(_udl.HTTPCookieProcessor(jar))
            login_data = _uparse.urlencode({"username": user, "password": pwd}).encode()
            with opener.open(_udl.Request(f"{url}/api/v2/auth/login", data=login_data,
                             headers={"Referer": url}), timeout=8) as r:
                if r.read().decode().strip() != "Ok.":
                    return jsonify({"error": "qBittorrent login failed", "torrents": []})
            with opener.open(_udl.Request(f"{url}/api/v2/torrents/info",
                             headers={"Referer": url}), timeout=8) as r:
                torrents = _jdl.loads(r.read())
            speed_info = {}
            try:
                with opener.open(_udl.Request(f"{url}/api/v2/transfer/info",
                                 headers={"Referer": url}), timeout=5) as r:
                    speed_info = _jdl.loads(r.read())
            except Exception: pass
            result = [{"name": t.get("name",""), "state": t.get("state",""),
                       "progress": round(t.get("progress",0)*100, 1),
                       "size": _fmt(t.get("size",0)), "dlspeed": _fmt(t.get("dlspeed",0))+"/s",
                       "upspeed": _fmt(t.get("upspeed",0))+"/s", "eta": t.get("eta",0),
                       "num_seeds": t.get("num_seeds",0), "category": t.get("category","")}
                      for t in torrents]
            return jsonify({"torrents": result,
                            "dl_speed": _fmt(speed_info.get("dl_info_speed",0))+"/s",
                            "ul_speed": _fmt(speed_info.get("up_info_speed",0))+"/s"})
        except Exception as e:
            return jsonify({"error": str(e), "torrents": []})

    # ── Transmission ─────────────────────────────────────────────────────
    elif dtype == "transmission":
        url  = _get_setting("transmission_url",  "").rstrip("/")
        user = _get_setting("transmission_user", "")
        pwd  = _get_setting("transmission_pass", "")
        if not url:
            return jsonify({"error": "Transmission URL not configured", "torrents": []})
        rpc_url = f"{url}/transmission/rpc"
        if "/rpc" in url:
            rpc_url = url
        try:
            import base64
            headers = {"Content-Type": "application/json", "User-Agent": "ArrHub/3.15"}
            if user:
                creds = base64.b64encode(f"{user}:{pwd}".encode()).decode()
                headers["Authorization"] = f"Basic {creds}"
            payload = _jdl.dumps({"method":"torrent-get","arguments":{"fields":[
                "id","name","status","percentDone","totalSize","rateDownload","rateUpload","eta","labels"]}}).encode()
            # Need CSRF token — first request gets 409 with X-Transmission-Session-Id
            token = ""
            try:
                _udl.urlopen(_udl.Request(rpc_url, data=payload, headers={**headers, "X-Transmission-Session-Id": ""}), timeout=5)
            except _udl.HTTPError as e:
                token = e.headers.get("X-Transmission-Session-Id", "")
            headers["X-Transmission-Session-Id"] = token
            with _udl.urlopen(_udl.Request(rpc_url, data=payload, headers=headers), timeout=8) as r:
                data = _jdl.loads(r.read())
            state_map = {0:"stopped",1:"check_wait",2:"checking",3:"dl_wait",4:"downloading",5:"seed_wait",6:"seeding"}
            result = [{"name": t.get("name",""),
                       "state": state_map.get(t.get("status",0), str(t.get("status",0))),
                       "progress": round(t.get("percentDone",0)*100, 1),
                       "size": _fmt(t.get("totalSize",0)),
                       "dlspeed": _fmt(t.get("rateDownload",0))+"/s",
                       "upspeed": _fmt(t.get("rateUpload",0))+"/s",
                       "eta": t.get("eta",-1), "num_seeds": 0,
                       "category": (t.get("labels") or [""])[0]}
                      for t in data.get("arguments",{}).get("torrents",[])]
            total_dl = sum(t.get("rateDownload",0) for t in data.get("arguments",{}).get("torrents",[]))
            total_ul = sum(t.get("rateUpload",0)   for t in data.get("arguments",{}).get("torrents",[]))
            return jsonify({"torrents": result,
                            "dl_speed": _fmt(total_dl)+"/s", "ul_speed": _fmt(total_ul)+"/s"})
        except Exception as e:
            return jsonify({"error": str(e), "torrents": []})

    # ── Deluge ───────────────────────────────────────────────────────────
    elif dtype == "deluge":
        url  = _get_setting("deluge_url",  "").rstrip("/")
        pwd  = _get_setting("deluge_pass", "deluge")
        if not url:
            return jsonify({"error": "Deluge URL not configured", "torrents": []})
        json_url = f"{url}/json"
        if url.endswith("/json"):
            json_url = url
        try:
            jar = _cj.CookieJar()
            opener = _udl.build_opener(_udl.HTTPCookieProcessor(jar))
            hdrs = {"Content-Type": "application/json", "User-Agent": "ArrHub/3.15"}
            def _rpc(method, params):
                body = _jdl.dumps({"id":1,"method":method,"params":params}).encode()
                with opener.open(_udl.Request(json_url, data=body, headers=hdrs), timeout=8) as r:
                    return _jdl.loads(r.read())
            # Login
            _rpc("auth.login", [pwd])
            # Get torrents
            fields = ["name","state","progress","total_size","download_payload_rate","upload_payload_rate","eta","label"]
            resp = _rpc("core.get_torrents_status", [{}, fields])
            torrents_raw = resp.get("result", {})
            state_map = {"Downloading":"downloading","Seeding":"seeding","Paused":"paused",
                         "Error":"error","Queued":"queued","Checking":"checking","Moving":"moving"}
            result = [{"name": v.get("name",""), "state": state_map.get(v.get("state",""), v.get("state","")),
                       "progress": round(v.get("progress",0), 1),
                       "size": _fmt(v.get("total_size",0)),
                       "dlspeed": _fmt(v.get("download_payload_rate",0))+"/s",
                       "upspeed": _fmt(v.get("upload_payload_rate",0))+"/s",
                       "eta": v.get("eta",-1), "num_seeds": 0, "category": v.get("label","")}
                      for v in torrents_raw.values()]
            total_dl = sum(v.get("download_payload_rate",0) for v in torrents_raw.values())
            total_ul = sum(v.get("upload_payload_rate",0)   for v in torrents_raw.values())
            return jsonify({"torrents": result,
                            "dl_speed": _fmt(total_dl)+"/s", "ul_speed": _fmt(total_ul)+"/s"})
        except Exception as e:
            return jsonify({"error": str(e), "torrents": []})

    return jsonify({"error": f"Unknown downloader type: {dtype}", "torrents": []})

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
    """Recent Seerr/Overseerr requests (latest 8) with resolved titles."""
    url = _get_setting("seerr_url")
    key = _get_setting("seerr_api_key")
    if not url:
        return jsonify({"configured": False})
    try:
        data = _svc_get(url, "/api/v1/request?take=8&skip=0&sort=added", key)
        reqs = []
        for r in data.get("results", []):
            media = r.get("media", {})
            media_type = media.get("mediaType", "")
            tmdb_id = media.get("tmdbId", "")

            # Resolve actual title from Overseerr media endpoint
            title = ""
            try:
                if media_type == "movie" and tmdb_id:
                    mdata = _svc_get(url, f"/api/v1/movie/{tmdb_id}", key)
                    title = mdata.get("title", "") or mdata.get("originalTitle", "")
                elif media_type == "tv" and tmdb_id:
                    mdata = _svc_get(url, f"/api/v1/tv/{tmdb_id}", key)
                    title = mdata.get("name", "") or mdata.get("originalName", "")
            except Exception:
                pass

            reqs.append({
                "id": r.get("id"),
                "type": media_type,
                "status": r.get("status"),  # 1=pending 2=approved 3=declined 4=available
                "requestedBy": r.get("requestedBy", {}).get("displayName", ""),
                "title": title or f"TMDB #{tmdb_id}",
                "poster": media.get("posterPath", ""),
                "createdAt": r.get("createdAt", "")[:10],
                "tmdbId": tmdb_id,
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
    """Extract ports from container as hostPort:containerPort strings (only bound ports)."""
    ports = []
    try:
        port_info = container.ports or {}
        for key, val in port_info.items():
            if val:
                for binding in val:
                    host_port = binding.get("HostPort")
                    if host_port and host_port.isdigit():
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
[data-theme="light"] #tab-overview .panel{background:rgba(255,255,255,0.7);border-color:rgba(0,0,0,0.08);box-shadow:0 2px 20px rgba(0,0,0,0.06);}
[data-theme="light"] .metric-card{background:rgba(255,255,255,0.6);border-color:rgba(0,0,0,0.08);box-shadow:0 1px 12px rgba(0,0,0,0.06);}
[data-theme="light"] .stat-card{background:rgba(255,255,255,0.6);border-color:rgba(0,0,0,0.08);box-shadow:0 1px 12px rgba(0,0,0,0.06);}
[data-theme="light"] #alerts-bar{background:rgba(255,255,255,0.6);border-color:rgba(0,0,0,0.08);}
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

/* ── Apple-smooth scrolling and selection ── */
html { scroll-behavior: smooth; }
* { box-sizing: border-box; }
::selection { background: rgba(99,102,241,0.3); color: inherit; }

/* ── Background image layer ─────────────────────────────────────────── */
#bg-layer{display:none;position:fixed;inset:0;z-index:0;background-size:cover;background-position:center;}
#app{position:relative;z-index:1;}

/* ── Homarr-style Dashboard Layout — 12-col span grid, dense auto-flow ── */
/* Widgets use data-span="N" for column width. Hidden widgets leave no gap  */
/* because grid-auto-flow:dense re-packs remaining visible cells.           */
.ov-dash{
  display:grid;
  grid-template-columns:repeat(12,1fr);
  grid-auto-flow:dense;
  gap:14px;
  padding:0;
}
/* Span widths */
.dash-cell[data-span="12"]{ grid-column:span 12; }
.dash-cell[data-span="8"] { grid-column:span 8;  }
.dash-cell[data-span="6"] { grid-column:span 6;  }
.dash-cell[data-span="4"] { grid-column:span 4;  }
.dash-cell[data-span="3"] { grid-column:span 3;  }
/* Base cell — min-width/height:0 CRITICAL: prevents grid children overflowing their lane */
.dash-cell{
  position:relative;
  min-width:0;
  min-height:0;
  width:100%;
  box-sizing:border-box;
}
.dash-cell > .panel,
.dash-cell > .metric-grid,
.dash-cell > div { margin:0; }
/* Scrollable cells — opt-in only */
.dash-cell.scrollable{
  overflow-y:auto;overflow-x:hidden;
  scrollbar-width:thin;scrollbar-color:var(--border) transparent;
}
#dash-storage.scrollable { max-height:320px; }
#dash-services.scrollable{ max-height:440px; }
#dash-infra.scrollable   { max-height:380px; }
#dash-logs.scrollable    { max-height:380px; }
#dash-todo.scrollable    { max-height:400px; }
/* Apps & Calendar widget styles */
.apps-tab-btn{ transition:border-color .15s,color .15s; }
.apps-tab-btn:hover{ background:var(--bg3)!important; }
#cal-grid > div:hover{ background:var(--bg3)!important; filter:brightness(1.08); }
/* ── Edit-mode overlay ── */
.ov-dash.edit-mode .dash-cell{
  outline:2px dashed var(--blue);
  outline-offset:3px;
  border-radius:var(--r,8px);
}
/* Per-widget remove button — hidden until edit mode */
.widget-remove-btn{
  display:none;
  position:absolute;top:6px;right:6px;z-index:20;
  width:22px;height:22px;border-radius:50%;
  background:var(--red2,rgba(248,81,73,.15));
  color:var(--red,#f85149);
  border:1px solid var(--red,#f85149);
  cursor:pointer;font-size:14px;font-weight:700;line-height:1;
  align-items:center;justify-content:center;
  transition:background .15s;
}
.widget-remove-btn:hover{ background:var(--red,#f85149);color:#fff; }
.ov-dash.edit-mode .widget-remove-btn{ display:flex!important; }
/* Responsive */
@media(max-width:900px){
  .dash-cell[data-span="8"] { grid-column:span 6; }
  .dash-cell[data-span="4"] { grid-column:span 6; }
}
@media(max-width:600px){
  .dash-cell[data-span="12"],
  .dash-cell[data-span="8"],
  .dash-cell[data-span="6"],
  .dash-cell[data-span="4"],
  .dash-cell[data-span="3"]{ grid-column:span 12; }
}
/* Service card tabs ── */
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
  display:flex;flex-direction:column;align-items:center;gap:5px;padding:14px 8px 10px;
  background:var(--surface);border:2px solid var(--border);border-radius:10px;
  cursor:pointer;transition:border-color .15s,background .15s;text-align:center;
  position:relative;
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
  margin-bottom:20px;
  padding-bottom:0;
  border-bottom:none;
}
.section-title{font-size:34px;font-weight:200;color:var(--text);letter-spacing:-.8px;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','Segoe UI',sans-serif;}
.section-sub{font-size:13px;color:var(--text3);margin-top:2px;font-weight:400;letter-spacing:.01em;}

/* ── Apple-style overview panels ── */
#tab-overview .panel{
  background:rgba(255,255,255,0.04);
  backdrop-filter:blur(40px) saturate(180%);
  -webkit-backdrop-filter:blur(40px) saturate(180%);
  border:1px solid rgba(255,255,255,0.08);
  border-radius:16px;
  box-shadow:0 2px 20px rgba(0,0,0,0.15);
  transition:transform .2s ease, box-shadow .2s ease;
  overflow:hidden;         /* prevent content spilling outside rounded corners */
  min-width:0;             /* prevent flex/grid child blowout */
  width:100%;
  box-sizing:border-box;
}
#tab-overview .panel:hover{
  transform:translateY(-1px);
  box-shadow:0 4px 30px rgba(0,0,0,0.2);
}
#tab-overview .panel-title{
  font-size:14px;font-weight:500;letter-spacing:.02em;
  color:var(--text2);
  border-bottom:1px solid rgba(255,255,255,0.06);
  padding-bottom:10px;margin-bottom:14px;
}

/* ── Stat cards row ── */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-bottom:20px;}
.stat-card{
  background:rgba(255,255,255,0.04);
  backdrop-filter:blur(40px) saturate(180%);
  -webkit-backdrop-filter:blur(40px) saturate(180%);
  border:1px solid rgba(255,255,255,0.08);
  border-radius:16px;padding:16px 18px;
  display:flex;flex-direction:column;gap:6px;
  transition:transform .2s ease, box-shadow .2s ease;
  container-type:inline-size;overflow:hidden;min-height:0;
  box-shadow:0 1px 12px rgba(0,0,0,0.12);
}
.stat-card:hover{transform:translateY(-1px);box-shadow:0 4px 20px rgba(0,0,0,0.18);border-color:rgba(255,255,255,0.12);}
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
.metric-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px;}
.metric-card{
  background:rgba(255,255,255,0.05);
  backdrop-filter:blur(40px) saturate(180%);
  -webkit-backdrop-filter:blur(40px) saturate(180%);
  border:1px solid rgba(255,255,255,0.08);border-radius:16px;
  padding:16px 18px;display:flex;flex-direction:column;gap:6px;overflow:hidden;
  box-shadow:0 1px 12px rgba(0,0,0,0.12);
  transition:transform .2s ease, box-shadow .2s ease;
}
.metric-card:hover{transform:translateY(-1px);box-shadow:0 4px 20px rgba(0,0,0,0.18);}
/* When gauge widget is narrow, stack metric cards 2×2 */
@container (max-width:600px){.metric-grid{grid-template-columns:repeat(2,1fr);}}
.metric-top{display:flex;align-items:center;justify-content:space-between;}
.metric-name{font-size:12px;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:.06em;}
.metric-badge{font-size:11px;padding:2px 7px;border-radius:10px;font-weight:600;}

/* ── Container grid ── */
.container-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--ctr-card-min,280px),1fr));gap:14px;}
.ctr-card{
  background:rgba(255,255,255,0.05);backdrop-filter:blur(20px);
  border:1px solid rgba(255,255,255,0.08);border-radius:14px;
  overflow:hidden;transition:border-color var(--transition),backdrop-filter var(--transition);
}
.ctr-card:hover{border-color:rgba(255,255,255,0.12);background:rgba(255,255,255,0.07);}

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
.panel{background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);padding:16px;margin-bottom:16px;transition:box-shadow .25s cubic-bezier(.25,.46,.45,.94),transform .25s cubic-bezier(.25,.46,.45,.94),border-color .2s;}
.panel-title{font-size:13px;font-weight:600;color:var(--text);margin-bottom:12px;display:flex;align-items:center;gap:8px;}
.panel:hover{box-shadow:0 8px 32px rgba(0,0,0,0.22);transform:translateY(-1px);}

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
#app.sb-collapsed #sidebar{width:56px;min-width:56px;overflow:hidden;}
#app.sb-collapsed .sb-title,
#app.sb-collapsed .sb-version,
#app.sb-collapsed .sb-section-label,
#app.sb-collapsed .sb-badge{display:none!important;}
/* font-size:0 collapses bare text nodes (label text) without touching SVG */
#app.sb-collapsed .sb-item{
  font-size:0;
  justify-content:center;
  padding:10px 0;
  margin:1px 6px;
  border-radius:8px;
}
/* Keep icon sized and centered */
#app.sb-collapsed .sb-item .sb-icon{
  width:20px;height:20px;flex-shrink:0;margin:0;opacity:.85;
}
#app.sb-collapsed .sb-item.active .sb-icon{opacity:1;}
#app.sb-collapsed .sb-brand{justify-content:center;padding:14px 0;}
#app.sb-collapsed .sb-logo{margin:0;}
#app.sb-collapsed #sidebar .sb-section{padding:4px 0;}

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
  transition:color var(--transition);flex-shrink:0;
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

/* ── Comprehensive Mobile Layout ── */
@media(max-width:768px){
  /* IPTV: stack channel list above player */
  #iptv-channels-view > div{
    grid-template-columns:1fr!important;
    height:auto!important;
    min-height:unset!important;
  }
  #iptv-channel-list{max-height:200px!important;}
  #iptv-player-wrap{min-height:240px!important;}
  /* Overview gauges: keep 2 columns on tablet */
  .metric-grid{grid-template-columns:repeat(2,1fr)!important;}
  /* Topbar: hide less critical stats */
  #topbar .tb-stat:nth-child(n+3){display:none;}
  /* Section headers: wrap */
  .section-header{flex-wrap:wrap;gap:8px;}
  /* Modals: full width */
  .modal-content{width:96vw!important;max-width:96vw!important;}
  /* GridStack: single column */
  .grid-stack{min-height:unset!important;}
}

@media(max-width:480px){
  /* Overview gauges: single column on phones */
  .metric-grid{grid-template-columns:1fr!important;}
  /* Gauges: shrink canvas to fit phone */
  #cpu-gauge-canvas,#mem-gauge-canvas{width:130px!important;height:130px!important;}
  /* IPTV sub-nav: wrap */
  #tab-iptv .section-header > div:last-child{flex-wrap:wrap;gap:6px;}
  /* Reduce padding throughout */
  .panel{padding:10px!important;}
  .metric-card{padding:10px 8px!important;}
  /* Section title font */
  .section-title{font-size:14px!important;}
  /* Bottom nav labels: smaller */
  .bn-item span{font-size:9px!important;}
  /* RSS cards: single column */
  #tab-rss .cat-grid{grid-template-columns:1fr!important;}
  /* Tables: horizontal scroll */
  .table-wrap,#ctr-table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;}
  /* Port map table */
  .pm-group table{min-width:420px;}
  /* Topbar load stat hidden */
  #topbar .tb-stat:nth-child(n+2){display:none;}
  /* Deployment grid: single column */
  #launcher-grid{grid-template-columns:repeat(3,1fr)!important;}
}

/* ── Football Hub mobile ── */
@media(max-width:700px){
  /* View buttons: wrap & reduce padding */
  #tab-epl .section-header > div:last-child{flex-wrap:wrap;gap:4px;}
  #tab-epl .view-btn{padding:4px 8px;font-size:11px;}
  /* League selector: scrollable rows */
  #football-league-selector > div:not(:first-child):not(:nth-child(3)){
    flex-wrap:nowrap;overflow-x:auto;padding-bottom:4px;-webkit-overflow-scrolling:touch;scrollbar-width:none;
  }
  .league-pill{font-size:10px;padding:4px 10px;}
  /* Team modal: full width on phone */
  #football-team-modal{padding:0;align-items:flex-end;}
  #football-team-modal > .panel{width:100vw!important;max-width:100vw!important;max-height:85vh;border-radius:16px 16px 0 0;}
  /* Match rows on small screens */
  #football-fixtures-list > div, #football-results-list > div{padding:8px 8px;}
}
/* ── Feeds tab mobile ── */
@media(max-width:600px){
  #feeds-rss-grid,#feeds-reddit-grid,#feeds-yt-grid{
    grid-template-columns:repeat(auto-fill,minmax(160px,1fr))!important;
    gap:8px!important;
  }
  #feeds-pills-row{flex-wrap:nowrap;overflow-x:auto;padding-bottom:4px;-webkit-overflow-scrolling:touch;}
  #feeds-pills-row .filter-pill{white-space:nowrap;flex-shrink:0;}
  #tab-feeds .section-header{flex-direction:column;align-items:flex-start;gap:6px;}
}
/* ── IPTV modal mobile ── */
@media(max-width:480px){
  #iptv-add-modal .panel{width:98vw!important;padding:14px!important;}
  #feeds-add-modal .panel, #feeds-newcat-modal .panel{width:98vw!important;padding:14px!important;}
}
/* ── Feeds on very small screens ── */
@media(max-width:400px){
  #feeds-rss-grid,#feeds-reddit-grid,#feeds-yt-grid{
    grid-template-columns:1fr!important;
  }
}

/* Ensure tap targets are large enough on all mobile sizes */
@media(max-width:900px){
  .sb-item,.channel-item{min-height:44px;display:flex;align-items:center;}
  input[type="text"],input[type="search"],select{font-size:16px!important;} /* prevent iOS zoom */
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
.league-pill{
  display:inline-flex;align-items:center;gap:4px;
  padding:5px 13px;border-radius:20px;border:1px solid var(--border);
  background:var(--bg2);color:var(--text2);font-size:11px;cursor:pointer;
  transition:all .15s;white-space:nowrap;
}
.league-pill:hover{background:var(--bg3);color:var(--text);}
.league-pill.active{background:var(--accent,#2563eb);color:#fff;border-color:var(--accent,#2563eb);}

/* ══════════════════════════════════════════════════════════════
   10. ALERTS BAR
   ══════════════════════════════════════════════════════════════ */
#alerts-bar{
  border-radius:16px;margin-bottom:16px;
  border:1px solid rgba(255,255,255,0.08);
  overflow:hidden;
  background:rgba(255,255,255,0.03);
  backdrop-filter:blur(40px) saturate(180%);
  -webkit-backdrop-filter:blur(40px) saturate(180%);
}
#alerts-bar-header{
  display:flex;align-items:center;justify-content:space-between;
  padding:10px 14px;background:transparent;cursor:pointer;
  font-size:13px;font-weight:500;
}
#alerts-bar-header:hover{background:rgba(255,255,255,0.03);}
#alerts-body{padding:0 14px 10px;background:transparent;}
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
      <div class="sb-version">v3.15.35</div>
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
      Infrastructure
    </div>
    <div class="sb-item" onclick="showTab('ports',this)">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
      Port Map
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
    <div class="sb-item" onclick="showTab('feeds',this)">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 5c7.18 0 13 5.82 13 13M6 11a7 7 0 017 7m-6 0a1 1 0 11-2 0 1 1 0 012 0z"/></svg>
      Feeds
    </div>
    <div class="sb-item" onclick="showTab('iptv',this)">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.069A1 1 0 0121 8.868v6.264a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
      IPTV Player
    </div>
    <div class="sb-item" onclick="showTab('epl',this)">
      <svg class="sb-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" stroke-width="2"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 2l3 7h7l-5.5 4.5L18 21l-6-4.5L6 21l1.5-7.5L2 9h7z"/></svg>
      Football
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
      <div class="section-header" style="margin-bottom:24px">
        <div>
          <div class="section-title" id="ov-greeting" style="margin-bottom:2px">Good morning</div>
          <div class="section-sub" id="ov-date" style="font-size:14px;opacity:.6"></div>
          <div class="section-sub" id="ov-hostname" style="font-size:11px;opacity:.4;margin-top:2px">Loading...</div>
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <!-- Always-visible widget controls — Homarr-style -->
          <button id="ov-edit-btn" class="btn" onclick="toggleGridEdit()" style="font-size:11px;padding:4px 12px;gap:4px" title="Enter edit mode to show/hide widgets">
            <svg width="11" height="11" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg>
            Edit
          </button>
          <button id="ov-add-btn" class="btn blue" onclick="showWidgetPalette()" style="font-size:11px;padding:4px 12px;gap:4px" title="Add or remove dashboard widgets">
            <svg width="11" height="11" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/></svg>
            Widgets
          </button>
          <button id="ov-reset-btn" class="btn" onclick="resetGridLayout()" style="font-size:11px;padding:4px 12px;gap:4px;display:none" title="Restore all hidden widgets">
            <svg width="11" height="11" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
            Reset
          </button>
        </div>
      </div>

      <!-- ── Overview CSS Grid Dashboard ─────────────────────────────────
           Clean, fixed CSS Grid layout with Glance-style design.
           No drag-and-drop or localStorage positioning.        -->
      <div id="ov-dashboard" class="ov-dash">

        <!-- ⓪ System Gauges widget -->
        <div class="dash-cell" id="dash-gauges" data-span="8" data-widget="gauges">
          <button class="widget-remove-btn" onclick="removeWidget('gauges')" title="Hide widget">✕</button>
            <div class="metric-grid" id="gauge-row" style="margin:0;padding:10px 12px;display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;width:100%;box-sizing:border-box;min-width:0">

              <!-- ── CPU ── -->
              <div class="metric-card" style="align-items:center;text-align:center;padding:16px 12px;gap:8px">
                <div class="metric-top" style="width:100%;justify-content:center;gap:10px">
                  <span class="metric-name">CPU</span>
                  <span class="metric-badge" id="cpu-badge" style="background:var(--green2);color:var(--green)">0%</span>
                </div>
                <div style="position:relative;width:160px;height:160px">
                  <canvas id="cpu-gauge-canvas" width="160" height="160"></canvas>
                  <div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;pointer-events:none">
                    <span id="cpu-gauge-text" style="font-size:32px;font-weight:700;font-family:var(--mono);color:#3fb950;line-height:1">0%</span>
                    <span style="font-size:10px;color:var(--text3);margin-top:4px;letter-spacing:.04em;text-transform:uppercase">Usage</span>
                  </div>
                </div>
                <div id="cpu-cores" style="font-size:12px;color:var(--text2);font-weight:500">— cores · — MHz</div>
                <div class="pbar-wrap" style="width:100%;margin-top:2px"><div class="pbar blue" id="cpu-pbar" style="width:0%"></div></div>
              </div>

              <!-- ── Memory ── -->
              <div class="metric-card" style="align-items:center;text-align:center;padding:16px 12px;gap:8px">
                <div class="metric-top" style="width:100%;justify-content:center;gap:10px">
                  <span class="metric-name">Memory</span>
                  <span class="metric-badge" id="mem-badge" style="background:var(--green2);color:var(--green)">0%</span>
                </div>
                <div style="position:relative;width:160px;height:160px">
                  <canvas id="mem-gauge-canvas" width="160" height="160"></canvas>
                  <div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;pointer-events:none">
                    <span id="mem-gauge-text" style="font-size:32px;font-weight:700;font-family:var(--mono);color:#bc8cff;line-height:1">0%</span>
                    <span style="font-size:10px;color:var(--text3);margin-top:4px;letter-spacing:.04em;text-transform:uppercase">RAM</span>
                  </div>
                </div>
                <div id="mem-detail" style="font-size:12px;color:var(--text2);font-weight:500">— / — GB</div>
                <div class="pbar-wrap" style="width:100%;margin-top:2px"><div class="pbar" id="mem-pbar" style="width:0%;background:var(--purple)"></div></div>
              </div>

              <!-- ── Load & Uptime ── -->
              <div class="metric-card" style="gap:10px;padding:16px">
                <div class="metric-top">
                  <span class="metric-name">Load Average</span>
                </div>
                <div style="display:flex;flex-direction:column;gap:8px;flex:1;justify-content:center">
                  <div style="display:flex;flex-direction:column;gap:3px">
                    <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text3)"><span>1 min</span><span id="load-1m" style="color:var(--text);font-family:var(--mono)">—</span></div>
                    <div class="pbar-wrap"><div id="load-1m-bar" class="pbar blue" style="width:0%;transition:width .4s"></div></div>
                  </div>
                  <div style="display:flex;flex-direction:column;gap:3px">
                    <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text3)"><span>5 min</span><span id="load-5m" style="color:var(--text);font-family:var(--mono)">—</span></div>
                    <div class="pbar-wrap"><div id="load-5m-bar" class="pbar blue" style="width:0%;transition:width .4s"></div></div>
                  </div>
                  <div style="display:flex;flex-direction:column;gap:3px">
                    <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text3)"><span>15 min</span><span id="load-15m" style="color:var(--text);font-family:var(--mono)">—</span></div>
                    <div class="pbar-wrap"><div id="load-15m-bar" class="pbar blue" style="width:0%;transition:width .4s"></div></div>
                  </div>
                </div>
                <div style="border-top:1px solid var(--border);padding-top:10px;margin-top:2px">
                  <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text3)"><span>Uptime</span><span id="uptime-val" style="color:var(--green);font-family:var(--mono)">—</span></div>
                </div>
              </div>

              <!-- ── Containers ── -->
              <div class="metric-card" style="gap:10px;padding:16px">
                <div class="metric-top">
                  <span class="metric-name">Containers</span>
                </div>
                <div style="display:flex;flex-direction:column;gap:6px;flex:1;justify-content:center">
                  <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 10px;background:rgba(63,185,80,.08);border:1px solid rgba(63,185,80,.2);border-radius:6px">
                    <span style="font-size:11px;color:var(--text2);display:flex;align-items:center;gap:6px">
                      <span style="width:7px;height:7px;border-radius:50%;background:var(--green);display:inline-block"></span>Running
                    </span>
                    <span id="ctr-running-count" style="font-size:22px;font-weight:700;font-family:var(--mono);color:var(--green)">—</span>
                  </div>
                  <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 10px;background:var(--bg3);border:1px solid var(--border);border-radius:6px">
                    <span style="font-size:11px;color:var(--text2);display:flex;align-items:center;gap:6px">
                      <span style="width:7px;height:7px;border-radius:50%;background:var(--text3);display:inline-block"></span>Stopped
                    </span>
                    <span id="ctr-stopped-count" style="font-size:22px;font-weight:700;font-family:var(--mono);color:var(--text3)">—</span>
                  </div>
                  <div style="display:flex;justify-content:space-between;padding:6px 10px 0;font-size:11px;color:var(--text3)">
                    <span>Total</span><span id="ctr-total-count" style="font-family:var(--mono);color:var(--text2)">—</span>
                  </div>
                </div>
                <button class="btn blue" style="margin-top:4px;width:100%;justify-content:center;font-size:11px;padding:5px 8px" onclick="showTab('containers',null)">
                  View All Containers →
                </button>
              </div>

            </div>
        </div>

        <!-- ① System Info widget -->
        <div class="dash-cell" id="dash-sysinfo" data-span="4" data-widget="sysinfo">
          <button class="widget-remove-btn" onclick="removeWidget('sysinfo')" title="Hide widget">✕</button>
            <div class="panel" style="margin:0">
              <div class="panel-title">
                <svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                System Info
              </div>
              <!-- Terminal-style system info rows -->
              <div id="sys-info-grid" style="display:flex;flex-direction:column;gap:0">

                <!-- OS -->
                <div style="display:flex;align-items:center;gap:10px;padding:7px 12px;border-bottom:1px solid var(--border)">
                  <div style="width:26px;height:26px;background:var(--surface2);border-radius:6px;display:flex;align-items:center;justify-content:center;flex-shrink:0">
                    <svg width="13" height="13" fill="none" stroke="var(--blue)" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2v-4M9 21H5a2 2 0 01-2-2v-4m0 0h18"/></svg>
                  </div>
                  <div style="flex:1;min-width:0">
                    <div style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;line-height:1">Operating System</div>
                    <div id="si-os" style="font-size:12px;font-weight:600;color:var(--text);font-family:var(--mono);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">—</div>
                  </div>
                  <div style="width:7px;height:7px;border-radius:50%;background:var(--green);flex-shrink:0"></div>
                </div>

                <!-- Kernel -->
                <div style="display:flex;align-items:center;gap:10px;padding:7px 12px;border-bottom:1px solid var(--border)">
                  <div style="width:26px;height:26px;background:var(--surface2);border-radius:6px;display:flex;align-items:center;justify-content:center;flex-shrink:0">
                    <svg width="13" height="13" fill="none" stroke="var(--purple,#bc8cff)" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4"/></svg>
                  </div>
                  <div style="flex:1;min-width:0">
                    <div style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;line-height:1">Kernel</div>
                    <div id="si-kernel" style="font-size:12px;font-weight:600;color:var(--text);font-family:var(--mono);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">—</div>
                  </div>
                </div>

                <!-- Architecture + Python side by side -->
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:0">
                  <div style="display:flex;align-items:center;gap:10px;padding:7px 12px;border-bottom:1px solid var(--border);border-right:1px solid var(--border)">
                    <div style="width:26px;height:26px;background:var(--surface2);border-radius:6px;display:flex;align-items:center;justify-content:center;flex-shrink:0">
                      <svg width="13" height="13" fill="none" stroke="var(--yellow,#e3b341)" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 3l6 18M3 9h18M3 15h18"/></svg>
                    </div>
                    <div style="min-width:0">
                      <div style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;line-height:1">Architecture</div>
                      <div id="si-arch" style="font-size:12px;font-weight:600;color:var(--text);font-family:var(--mono);margin-top:2px">—</div>
                    </div>
                  </div>
                  <div style="display:flex;align-items:center;gap:10px;padding:7px 12px;border-bottom:1px solid var(--border)">
                    <div style="width:26px;height:26px;background:var(--surface2);border-radius:6px;display:flex;align-items:center;justify-content:center;flex-shrink:0">
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--green)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2z"/><path d="M8 12h8M12 8v8"/></svg>
                    </div>
                    <div style="min-width:0">
                      <div style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;line-height:1">Python</div>
                      <div id="si-python" style="font-size:12px;font-weight:600;color:var(--text);font-family:var(--mono);margin-top:2px">—</div>
                    </div>
                  </div>
                </div>

                <!-- Hostname + Uptime side by side -->
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:0">
                  <div style="display:flex;align-items:center;gap:10px;padding:7px 12px;border-right:1px solid var(--border)">
                    <div style="width:26px;height:26px;background:var(--surface2);border-radius:6px;display:flex;align-items:center;justify-content:center;flex-shrink:0">
                      <svg width="13" height="13" fill="none" stroke="var(--text2)" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2m-2-4h.01M17 16h.01"/></svg>
                    </div>
                    <div style="min-width:0">
                      <div style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;line-height:1">Hostname</div>
                      <div id="si-hostname" style="font-size:12px;font-weight:600;color:var(--text);font-family:var(--mono);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">—</div>
                    </div>
                  </div>
                  <div style="display:flex;align-items:center;gap:10px;padding:7px 12px">
                    <div style="width:26px;height:26px;background:var(--surface2);border-radius:6px;display:flex;align-items:center;justify-content:center;flex-shrink:0">
                      <svg width="13" height="13" fill="none" stroke="var(--text2)" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6l4 2"/></svg>
                    </div>
                    <div style="min-width:0">
                      <div style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;line-height:1">Uptime</div>
                      <div id="si-uptime" style="font-size:12px;font-weight:600;color:var(--text);font-family:var(--mono);margin-top:2px">—</div>
                    </div>
                  </div>
                </div>

              </div>
            </div>
        </div>

        <!-- ② Weather widget -->
        <div class="dash-cell" id="dash-weather" data-span="4" data-widget="weather">
          <button class="widget-remove-btn" onclick="removeWidget('weather')" title="Hide widget">✕</button>
            <div class="panel" id="weather-panel" style="margin:0">
              <div class="panel-title">
                <svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 15a4 4 0 004 4h9a5 5 0 10-.1-9.999 5.002 5.002 0 10-9.78 2.051A4.002 4.002 0 003 15z"/></svg>
                Weather
                <span id="weather-location" style="margin-left:8px;font-weight:400;font-size:11px;color:var(--text3)"></span>
              </div>
              <div id="weather-widget">
                <div style="display:flex;align-items:center;gap:14px;padding-bottom:10px;border-bottom:1px solid var(--border)">
                  <div id="weather-icon" style="font-size:42px;line-height:1">🌤️</div>
                  <div style="flex:1">
                    <div style="display:flex;align-items:baseline;gap:6px">
                      <div id="weather-temp" style="font-size:30px;font-weight:700;font-family:var(--mono);color:var(--text)">—</div>
                      <div id="weather-desc" style="font-size:12px;color:var(--text2)">Loading…</div>
                    </div>
                    <div style="font-size:11px;color:var(--text3);margin-top:3px">
                      Feels <span id="weather-feels">—</span>&ensp;·&ensp;Wind <span id="weather-wind">—</span>&ensp;·&ensp;Humidity <span id="weather-humidity">—</span>
                    </div>
                  </div>
                </div>
                <div id="weather-forecast" style="display:grid;grid-template-columns:repeat(7,1fr);gap:3px;margin-top:10px"></div>
              </div>
            </div>
        </div>

        <!-- ② Storage widget -->
        <div class="dash-cell scrollable" id="dash-storage" data-span="4" data-widget="storage">
          <button class="widget-remove-btn" onclick="removeWidget('storage')" title="Hide widget">✕</button>
            <div class="panel" style="margin:0;height:100%">
              <div class="panel-title">
                <svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4m0 5c0 2.21-3.582 4-8 4s-8-1.79-8-4"/></svg>
                Storage
              </div>
              <div id="dash-storage-list" style="display:flex;flex-direction:column;gap:8px;padding:4px 2px">
                <div style="color:var(--text3);font-size:12px;text-align:center;padding:16px">Loading...</div>
              </div>
            </div>
        </div>



        <!-- ③ Service Cards row -->
        <div class="dash-cell scrollable" id="dash-services" data-span="12" data-widget="services">
          <button class="widget-remove-btn" onclick="removeWidget('services')" title="Hide widget">✕</button>
            <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;padding:8px;box-sizing:border-box" id="service-cards-row">
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
              <div class="panel svc-card" style="margin:0" id="qbit-card">
                <div class="svc-card-hdr">
                  <span class="svc-card-title">⬇️ Downloads</span>
                  <div style="display:flex;align-items:center;gap:8px">
                    <span id="qbit-speed" style="font-size:10px;color:var(--text3);font-family:var(--mono)"></span>
                    <div class="svc-tabs">
                      <button class="svc-tab active" onclick="svcTabSwitch('qbit','active',this)">Active</button>
                      <button class="svc-tab" onclick="svcTabSwitch('qbit','all',this)">All</button>
                    </div>
                  </div>
                </div>
                <div id="qbit-card-body" class="svc-card-body"><div style="color:var(--text3);font-size:12px;text-align:center;padding:12px">Loading…</div></div>
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

        <!-- ④+⑤ Docker & Network I/O -->
        <div class="dash-cell scrollable" id="dash-infra" data-span="8" data-widget="infra">
          <button class="widget-remove-btn" onclick="removeWidget('infra')" title="Hide widget">✕</button>
            <div class="panel" style="margin:0">
              <div class="panel-title">
                <svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/></svg>
                Docker &amp; Network
              </div>
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;min-width:0;width:100%">

                <!-- Docker section -->
                <div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:10px 12px;min-width:0;overflow:hidden">
                  <div style="font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);margin-bottom:8px;display:flex;align-items:center;gap:6px">
                    <svg width="12" height="12" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/></svg>
                    Docker Engine
                  </div>
                  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
                    <div style="background:var(--surface);border-radius:6px;padding:8px 10px">
                      <div id="docker-images" style="font-size:20px;font-weight:700;font-family:var(--mono);color:var(--blue);line-height:1">—</div>
                      <div style="font-size:10px;color:var(--text3);margin-top:3px;display:flex;align-items:center;gap:4px">
                        <svg width="10" height="10" fill="none" stroke="currentColor" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2" stroke-width="2"/></svg>
                        Images
                      </div>
                    </div>
                    <div style="background:var(--surface);border-radius:6px;padding:8px 10px">
                      <div id="docker-volumes" style="font-size:20px;font-weight:700;font-family:var(--mono);color:var(--yellow,#e3b341);line-height:1">—</div>
                      <div style="font-size:10px;color:var(--text3);margin-top:3px;display:flex;align-items:center;gap:4px">
                        <svg width="10" height="10" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2"/></svg>
                        Volumes
                      </div>
                    </div>
                    <div style="background:var(--surface);border-radius:6px;padding:8px 10px">
                      <div id="docker-networks" style="font-size:20px;font-weight:700;font-family:var(--mono);color:var(--purple,#bc8cff);line-height:1">—</div>
                      <div style="font-size:10px;color:var(--text3);margin-top:3px;display:flex;align-items:center;gap:4px">
                        <svg width="10" height="10" fill="none" stroke="currentColor" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3" stroke-width="2"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg>
                        Networks
                      </div>
                    </div>
                    <div style="background:var(--surface);border-radius:6px;padding:8px 10px">
                      <div id="docker-disk" style="font-size:20px;font-weight:700;font-family:var(--mono);color:var(--text);line-height:1">—</div>
                      <div style="font-size:10px;color:var(--text3);margin-top:3px;display:flex;align-items:center;gap:4px">
                        <svg width="10" height="10" fill="none" stroke="currentColor" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" stroke-width="2"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l2 2"/></svg>
                        Disk Used
                      </div>
                    </div>
                  </div>
                </div>

                <!-- Network I/O section -->
                <div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:10px 12px;min-width:0;overflow:hidden">
                  <div style="font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);margin-bottom:8px;display:flex;align-items:center;gap:6px">
                    <svg width="12" height="12" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8.111 16.404a5.5 5.5 0 017.778 0M12 20h.01m-7.08-7.071c3.904-3.905 10.236-3.905 14.14 0M1.394 9.393c5.857-5.857 15.355-5.857 21.213 0"/></svg>
                    Network I/O
                  </div>
                  <!-- Upload -->
                  <div style="margin-bottom:10px">
                    <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px">
                      <span style="font-size:11px;color:var(--text3);display:flex;align-items:center;gap:4px">
                        <svg width="10" height="10" fill="none" stroke="var(--green)" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M5 15l7-7 7 7"/></svg>
                        Upload
                      </span>
                      <span id="ov-net-sent" style="font-size:13px;font-weight:700;font-family:var(--mono);color:var(--green)">—</span>
                    </div>
                    <div style="height:4px;background:var(--surface2);border-radius:2px;overflow:hidden">
                      <div id="net-sent-bar" style="height:100%;width:30%;background:var(--green);border-radius:2px;transition:width .6s"></div>
                    </div>
                  </div>
                  <!-- Download -->
                  <div style="margin-bottom:10px">
                    <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px">
                      <span style="font-size:11px;color:var(--text3);display:flex;align-items:center;gap:4px">
                        <svg width="10" height="10" fill="none" stroke="var(--blue)" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7"/></svg>
                        Download
                      </span>
                      <span id="ov-net-recv" style="font-size:13px;font-weight:700;font-family:var(--mono);color:var(--blue)">—</span>
                    </div>
                    <div style="height:4px;background:var(--surface2);border-radius:2px;overflow:hidden">
                      <div id="net-recv-bar" style="height:100%;width:55%;background:var(--blue);border-radius:2px;transition:width .6s"></div>
                    </div>
                  </div>
                  <!-- Interface label -->
                  <div style="font-size:10px;color:var(--text3);text-align:center;margin-top:4px">
                    Cumulative totals since boot
                  </div>
                </div>

              </div>
            </div>
        </div>

        <!-- ⑥ Recent Logs -->
        <div class="dash-cell scrollable" id="dash-logs" data-span="4" data-widget="logs">
          <button class="widget-remove-btn" onclick="removeWidget('logs')" title="Hide widget">✕</button>
            <div class="panel" style="margin:0">
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

        <!-- ⑧ Apps: Service Launcher + Containers (combined) -->
        <div class="dash-cell" id="dash-apps" data-span="8" data-widget="apps">
          <button class="widget-remove-btn" onclick="removeWidget('apps')" title="Hide widget">✕</button>
          <div class="panel" style="margin:0;height:100%">
            <div style="display:flex;align-items:center;border-bottom:1px solid var(--border);margin-bottom:0">
              <button id="apps-tab-btn-launcher" class="apps-tab-btn active" onclick="appsTabSwitch('launcher')" style="flex:1;padding:8px 10px;background:none;border:none;border-bottom:2px solid var(--accent,#2563eb);color:var(--text);font-size:11px;font-weight:600;cursor:pointer">🚀 Launcher</button>
              <button id="apps-tab-btn-ctrs" class="apps-tab-btn" onclick="appsTabSwitch('ctrs')" style="flex:1;padding:8px 10px;background:none;border:none;border-bottom:2px solid transparent;color:var(--text2);font-size:11px;cursor:pointer">
                📦 Containers
                <span id="ov-ctr-badge" style="background:var(--blue2);color:var(--blue);border-radius:10px;padding:1px 6px;font-size:10px;font-weight:600;margin-left:4px">—</span>
              </button>
            </div>
            <!-- Launcher panel -->
            <div id="apps-panel-launcher" style="padding:6px;overflow-y:auto;max-height:340px">
              <div id="ov-launcher-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(90px,1fr));gap:6px"></div>
            </div>
            <!-- Containers panel -->
            <div id="apps-panel-ctrs" style="display:none;padding:6px 0;overflow-y:auto;max-height:340px">
              <div style="display:flex;justify-content:flex-end;padding:0 8px 6px">
                <button class="btn blue" style="padding:3px 10px;font-size:11px" onclick="showTab('containers',null)">All →</button>
              </div>
              <div id="ov-ctr-list" style="display:flex;flex-direction:column;gap:5px;padding:0 8px">
                <div style="color:var(--text3);font-size:12px;text-align:center;padding:8px">Loading...</div>
              </div>
            </div>
          </div>
        </div>

        <!-- ⑨ To-Do List -->
        <div class="dash-cell scrollable" id="dash-todo" data-span="4" data-widget="todo">
          <button class="widget-remove-btn" onclick="removeWidget('todo')" title="Hide widget">✕</button>
          <div class="panel" style="margin:0;height:100%">
            <div class="panel-title" style="display:flex;align-items:center;gap:6px">
              <svg width="13" height="13" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"/></svg>
              To-Do
              <span id="todo-pending-count" style="background:var(--blue2);color:var(--blue);border-radius:10px;padding:1px 7px;font-size:10px;font-weight:600"></span>
            </div>
            <div style="display:flex;gap:5px;margin-bottom:8px">
              <input id="todo-new-input" type="text" placeholder="Add a task…"
                style="flex:1;background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:5px 8px;border-radius:var(--r);font-size:12px"
                onkeydown="if(event.key==='Enter')todoAdd()">
              <button onclick="todoAdd()" class="btn blue" style="padding:5px 10px;font-size:11px;white-space:nowrap">+ Add</button>
            </div>
            <div id="todo-list" style="display:flex;flex-direction:column;gap:4px;max-height:280px;overflow-y:auto;scrollbar-width:thin"></div>
          </div>
        </div>

        <!-- ⑩ Calendar -->
        <div class="dash-cell" id="dash-calendar" data-span="12" data-widget="calendar">
          <button class="widget-remove-btn" onclick="removeWidget('calendar')" title="Hide widget">✕</button>
          <div class="panel" style="margin:0">
            <div class="panel-title" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
              <span style="display:flex;align-items:center;gap:6px">
                <svg width="13" height="13" fill="none" stroke="currentColor" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="18" rx="2" stroke-width="2"/><path stroke-linecap="round" stroke-width="2" d="M16 2v4M8 2v4M3 10h18"/></svg>
                Calendar
              </span>
              <div style="display:flex;align-items:center;gap:6px">
                <button onclick="calPrevMonth()" class="btn" style="padding:3px 9px;font-size:13px">◀</button>
                <span id="cal-month-label" style="font-size:12px;font-weight:600;min-width:110px;text-align:center"></span>
                <button onclick="calNextMonth()" class="btn" style="padding:3px 9px;font-size:13px">▶</button>
                <button onclick="calGoToday()" class="btn blue" style="padding:3px 9px;font-size:11px">Today</button>
              </div>
            </div>
            <!-- Day-of-week headers -->
            <div id="cal-dow-row" style="display:grid;grid-template-columns:repeat(7,1fr);gap:2px;margin-bottom:3px">
              <div style="text-align:center;font-size:10px;color:var(--text3);font-weight:600;padding:3px">Sun</div>
              <div style="text-align:center;font-size:10px;color:var(--text3);font-weight:600;padding:3px">Mon</div>
              <div style="text-align:center;font-size:10px;color:var(--text3);font-weight:600;padding:3px">Tue</div>
              <div style="text-align:center;font-size:10px;color:var(--text3);font-weight:600;padding:3px">Wed</div>
              <div style="text-align:center;font-size:10px;color:var(--text3);font-weight:600;padding:3px">Thu</div>
              <div style="text-align:center;font-size:10px;color:var(--text3);font-weight:600;padding:3px">Fri</div>
              <div style="text-align:center;font-size:10px;color:var(--text3);font-weight:600;padding:3px">Sat</div>
            </div>
            <!-- Calendar grid -->
            <div id="cal-grid" style="display:grid;grid-template-columns:repeat(7,1fr);gap:2px"></div>
            <!-- Add event modal -->
            <div id="cal-event-form" style="display:none;margin-top:10px;background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:10px;gap:8px">
              <div style="font-size:11px;font-weight:600;color:var(--text2);margin-bottom:6px">
                Add event — <span id="cal-form-date-label" style="color:var(--blue)"></span>
              </div>
              <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
                <input id="cal-event-title" type="text" placeholder="Event title…"
                  style="flex:1;min-width:120px;background:var(--surface);border:1px solid var(--border);color:var(--text);padding:5px 8px;border-radius:var(--r);font-size:12px"
                  onkeydown="if(event.key==='Enter')calSubmitEvent()">
                <select id="cal-event-color" style="background:var(--surface);border:1px solid var(--border);color:var(--text);padding:5px 7px;border-radius:var(--r);font-size:12px">
                  <option value="#2563eb">🔵 Blue</option>
                  <option value="#16a34a">🟢 Green</option>
                  <option value="#dc2626">🔴 Red</option>
                  <option value="#d97706">🟠 Orange</option>
                  <option value="#9333ea">🟣 Purple</option>
                </select>
                <button onclick="calSubmitEvent()" class="btn blue" style="padding:5px 12px;font-size:11px">Save</button>
                <button onclick="calCloseForm()" class="btn" style="padding:5px 10px;font-size:11px">Cancel</button>
              </div>
            </div>
          </div>
        </div>

      </div><!-- /ov-dashboard -->

      <!-- Widget Palette Modal — Homarr-style -->
      <div id="widget-palette-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9999;align-items:center;justify-content:center" onclick="if(event.target===this)this.style.display='none'">
        <div style="background:var(--bg2);border:1px solid var(--border);border-radius:14px;padding:0;width:580px;max-width:95vw;max-height:82vh;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 24px 64px rgba(0,0,0,.5)">
          <!-- Header -->
          <div style="display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid var(--border)">
            <div>
              <div style="font-size:15px;font-weight:700;color:var(--text)">Dashboard Widgets</div>
              <div style="font-size:11px;color:var(--text3);margin-top:2px">Click a widget to show or hide it on your dashboard</div>
            </div>
            <button class="btn" onclick="document.getElementById('widget-palette-modal').style.display='none'" style="padding:4px 12px;font-size:13px">✕</button>
          </div>
          <!-- Widget grid -->
          <div id="widget-palette-body" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:10px;padding:16px;overflow-y:auto"></div>
          <!-- Footer -->
          <div style="padding:10px 20px;border-top:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
            <span id="widget-palette-count" style="font-size:11px;color:var(--text3)"></span>
            <button class="btn" onclick="resetGridLayout();document.getElementById('widget-palette-modal').style.display='none'" style="font-size:11px;padding:4px 14px">
              <svg width="11" height="11" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
              Show All
            </button>
          </div>
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
          <div class="section-title">🏗️ Infrastructure</div>
          <div class="section-sub">Storage, network, and hardware in one view</div>
        </div>
        <button class="btn-primary" onclick="loadStorage();loadNetwork()">↺ Refresh</button>
      </div>
      <!-- ── Storage ── -->
      <div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Storage</div>
      <div id="disk-list" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;margin-bottom:18px"></div>
      <!-- ── Network ── -->
      <div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Network</div>
      <div class="panel" style="margin-bottom:14px;padding:10px 14px">
        <div class="panel-title" style="margin-bottom:8px">📈 Live Bandwidth</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
          <div>
            <div style="font-size:10px;color:var(--text3);margin-bottom:3px">↑ TX (Upload)</div>
            <div style="position:relative;height:70px"><canvas id="net-tx-chart"></canvas></div>
          </div>
          <div>
            <div style="font-size:10px;color:var(--text3);margin-bottom:3px">↓ RX (Download)</div>
            <div style="position:relative;height:70px"><canvas id="net-rx-chart"></canvas></div>
          </div>
        </div>
      </div>
      <div class="panel">
        <div class="panel-title">Interfaces</div>
        <table><thead><tr><th>Interface</th><th>IP</th><th>Sent</th><th>Recv</th><th>Rate ↑/↓</th><th>Status</th></tr></thead>
        <tbody id="net-table"></tbody></table>
      </div>
      <!-- ── Hardware (merged) — pie charts ── -->
      <div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin:18px 0 8px">Hardware</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">

        <!-- CPU pie chart -->
        <div class="panel" style="display:flex;flex-direction:column;align-items:center;padding:16px 12px;gap:10px">
          <div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;align-self:flex-start">CPU</div>
          <div style="position:relative;width:120px;height:120px">
            <canvas id="hw-cpu-chart" width="120" height="120"></canvas>
            <div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;pointer-events:none">
              <span id="hw-cpu-pct" style="font-size:20px;font-weight:700;font-family:var(--mono);color:var(--text)">—%</span>
              <span style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.04em">Usage</span>
            </div>
          </div>
          <div id="hw-cpu-detail" style="font-size:11px;color:var(--text2);text-align:center">— cores · — MHz</div>
        </div>

        <!-- Memory pie chart -->
        <div class="panel" style="display:flex;flex-direction:column;align-items:center;padding:16px 12px;gap:10px">
          <div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;align-self:flex-start">Memory</div>
          <div style="position:relative;width:120px;height:120px">
            <canvas id="hw-mem-chart" width="120" height="120"></canvas>
            <div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;pointer-events:none">
              <span id="hw-mem-pct" style="font-size:20px;font-weight:700;font-family:var(--mono);color:var(--text)">—%</span>
              <span style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.04em">Used</span>
            </div>
          </div>
          <div id="hw-mem-detail" style="font-size:11px;color:var(--text2);text-align:center">— / — GB</div>
        </div>

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
      <!-- Reddit Login -->
      <div class="panel">
        <div class="panel-title">🔐 Reddit Login</div>
        <div style="font-size:12px;color:var(--text3);margin-bottom:10px">
          Enter your Reddit username and password to access NSFW subreddits. <strong>No app creation required.</strong>
          Leave blank to use anonymous access (public subreddits only).
        </div>
        <div class="settings-grid">
          <div class="field"><label>Reddit Username</label><input type="text" id="cfg-reddit-username" placeholder="your_reddit_username"><div class="field-hint">Your Reddit account username</div></div>
          <div class="field"><label>Reddit Password</label><input type="password" id="cfg-reddit-password" placeholder="•••••••••••"><div class="field-hint">Your Reddit account password</div></div>
        </div>
        <details style="margin-top:12px">
          <summary style="font-size:11px;color:var(--text3);cursor:pointer;user-select:none">⚙️ Advanced — Reddit API App (optional, for official OAuth access)</summary>
          <div style="margin-top:10px;padding:10px;background:var(--bg3);border-radius:6px;border:1px solid var(--border)">
            <div style="font-size:11px;color:var(--text3);margin-bottom:8px">
              Only needed if username+password login stops working. Create a free <strong>script</strong>-type app at
              <a href="https://www.reddit.com/prefs/apps" target="_blank" rel="noopener" style="color:var(--blue)">reddit.com/prefs/apps</a>.
            </div>
            <div class="settings-grid">
              <div class="field"><label>Client ID</label><input type="text" id="cfg-reddit-client-id" placeholder="14-char app ID"><div class="field-hint">Found under your app name on the apps page</div></div>
              <div class="field"><label>Client Secret</label><input type="password" id="cfg-reddit-client-secret" placeholder="•••••••••••"><div class="field-hint">The "secret" field for your Reddit app</div></div>
            </div>
          </div>
        </details>
        <button class="btn-primary" style="margin-top:12px" onclick="saveRedditCredentials()">Save Reddit Credentials</button>
        <span id="reddit-creds-status" style="font-size:11px;color:var(--green);margin-left:10px"></span>
      </div>
      <!-- Weather Location -->
      <div class="panel">
        <div class="panel-title">🌤️ Weather Location</div>
        <div style="font-size:12px;color:var(--text3);margin-bottom:10px">Set your city and country for weather data. Leave blank to auto-detect from IP.</div>
        <div class="settings-grid">
          <div class="field"><label>City</label><input type="text" id="cfg-weather-city" placeholder="e.g. London"><div class="field-hint">City name for weather forecast</div></div>
          <div class="field"><label>Country</label><input type="text" id="cfg-weather-country" placeholder="e.g. United Kingdom"><div class="field-hint">Country name (optional, helps accuracy)</div></div>
        </div>
        <button class="btn-primary" style="margin-top:12px" onclick="saveWeatherLocation()">Save & Refresh Weather</button>
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
          <div class="field"><label>⚽ Football API Key</label><input type="password" id="svc-football-key" placeholder="football-data.org key"><div class="field-hint">Free at football-data.org — powers Premier League tab</div></div>
          <div style="grid-column:1/-1;margin-top:4px">
            <div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px">⬇️ Downloads</div>
            <div style="display:flex;flex-direction:column;gap:8px">
              <div>
                <label style="font-size:11px;color:var(--text2)">Client</label>
                <select id="svc-dl-type" onchange="dlTypeChanged()" style="width:100%;padding:7px 10px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);font-size:12px;box-sizing:border-box;margin-top:3px">
                  <option value="qbittorrent">qBittorrent</option>
                  <option value="transmission">Transmission</option>
                  <option value="deluge">Deluge</option>
                </select>
              </div>
              <!-- qBittorrent fields -->
              <div id="dl-fields-qbittorrent" style="display:flex;flex-direction:column;gap:8px">
                <div>
                  <label style="font-size:11px;color:var(--text2)">URL</label>
                  <input id="svc-qbit-url" type="text" placeholder="http://10.0.0.33:8080" style="width:100%;padding:7px 10px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);font-size:12px;box-sizing:border-box;margin-top:3px">
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
                  <div>
                    <label style="font-size:11px;color:var(--text2)">Username</label>
                    <input id="svc-qbit-user" type="text" placeholder="admin" style="width:100%;padding:7px 10px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);font-size:12px;box-sizing:border-box;margin-top:3px">
                  </div>
                  <div>
                    <label style="font-size:11px;color:var(--text2)">Password</label>
                    <input id="svc-qbit-pass" type="password" placeholder="adminadmin" style="width:100%;padding:7px 10px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);font-size:12px;box-sizing:border-box;margin-top:3px">
                  </div>
                </div>
              </div>
              <!-- Transmission fields -->
              <div id="dl-fields-transmission" style="display:none;flex-direction:column;gap:8px">
                <div>
                  <label style="font-size:11px;color:var(--text2)">URL</label>
                  <input id="svc-transmission-url" type="text" placeholder="http://10.0.0.33:9091" style="width:100%;padding:7px 10px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);font-size:12px;box-sizing:border-box;margin-top:3px">
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
                  <div>
                    <label style="font-size:11px;color:var(--text2)">Username (optional)</label>
                    <input id="svc-transmission-user" type="text" placeholder="" style="width:100%;padding:7px 10px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);font-size:12px;box-sizing:border-box;margin-top:3px">
                  </div>
                  <div>
                    <label style="font-size:11px;color:var(--text2)">Password</label>
                    <input id="svc-transmission-pass" type="password" placeholder="" style="width:100%;padding:7px 10px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);font-size:12px;box-sizing:border-box;margin-top:3px">
                  </div>
                </div>
              </div>
              <!-- Deluge fields -->
              <div id="dl-fields-deluge" style="display:none;flex-direction:column;gap:8px">
                <div>
                  <label style="font-size:11px;color:var(--text2)">URL</label>
                  <input id="svc-deluge-url" type="text" placeholder="http://10.0.0.33:8112" style="width:100%;padding:7px 10px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);font-size:12px;box-sizing:border-box;margin-top:3px">
                </div>
                <div>
                  <label style="font-size:11px;color:var(--text2)">Password</label>
                  <input id="svc-deluge-pass" type="password" placeholder="deluge" style="width:100%;padding:7px 10px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);font-size:12px;box-sizing:border-box;margin-top:3px">
                </div>
              </div>
            </div>
          </div>
        </div>
        <button class="btn-primary" style="margin-top:16px" onclick="saveSvcSettings()">Save Integrations</button>
      </div>

      <!-- ── Config Backup & Restore ── -->
      <div class="panel">
        <div class="panel-title">💾 Config Backup & Restore</div>
        <div style="font-size:12px;color:var(--text3);margin-bottom:14px">
          Export all your settings (API keys, URLs, credentials) to a JSON file. Import it to restore after a fresh install or container rebuild.
          <strong style="color:var(--text2)"> Keep your backup file safe — it contains credentials.</strong>
        </div>
        <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center">
          <button class="btn-primary" onclick="exportConfig()" style="padding:8px 18px">
            ⬇️ Export Config
          </button>
          <button class="btn" onclick="importConfig()" style="padding:8px 18px">
            ⬆️ Import Config
          </button>
          <span id="config-backup-status" style="font-size:11px;color:var(--green)"></span>
        </div>
        <div style="margin-top:10px;font-size:11px;color:var(--text3)">
          Export saves: service URLs, API keys, Reddit credentials, weather city, downloader settings, and all other server-side settings.<br>
          <em>Appearance/theme preferences are saved locally in your browser and are not included.</em>
        </div>
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
        <div class="ctr-row"><span>ArrHub Version</span><span>3.15.21</span></div>
        <div class="ctr-row"><span>Auth Status</span><span style="color:var(--green)">Disabled (open access)</span></div>
        <div class="ctr-row"><span>WebUI Port</span><span>9999</span></div>
      </div>
    </div>

    <!-- ── RSS FEEDS ── -->
    <!-- ═══════════════════════════════════════════════════════════
         IPTV PLAYER TAB
    ═══════════════════════════════════════════════════════════ -->
    <div id="tab-iptv" class="tab-panel">
      <div class="section-header">
        <div>
          <div class="section-title">📺 IPTV Player</div>
          <div class="section-sub">Live channels · Sports · Multiview · Schedule</div>
        </div>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
          <button class="view-btn active" id="iptv-view-channels" onclick="iptvSetView('channels')">📡 Channels</button>
          <button class="view-btn" id="iptv-view-schedule" onclick="iptvSetView('schedule')">📅 Schedule</button>
          <button class="view-btn" id="iptv-view-multiview" onclick="iptvSetView('multiview')">⊞ Multiview</button>
          <select id="iptv-source-select" onchange="iptvSetSource(this.value)" title="IPTV Source" style="font-size:11px;padding:4px 8px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);cursor:pointer">
            <option value="moviebite">MovieBite</option>
            <option value="bintv">BinTV</option>
            <option value="daddylive">DaddyLive</option>
          </select>
          <div id="iptv-dl-domain-wrap" style="display:none;align-items:center;gap:4px;flex-wrap:wrap">
            <span style="font-size:10px;color:var(--text3)">Domain:</span>
            <select id="iptv-dl-domain-preset" onchange="iptvDaddylivePreset(this.value)"
              style="font-size:10px;padding:3px 5px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r)">
              <option value="daddylive.lat">daddylive.lat ★</option>
              <option value="daddylive.eu">daddylive.eu</option>
              <option value="daddylive.me">daddylive.me</option>
              <option value="daddylive.sh">daddylive.sh</option>
              <option value="daddylive.tv">daddylive.tv</option>
              <option value="custom">Custom…</option>
            </select>
            <input id="iptv-dl-domain" type="text" placeholder="daddylive.lat"
              style="font-size:10px;padding:3px 7px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);width:110px;display:none"
              onchange="iptvSetDaddyliveDomain(this.value)"
              title="Custom DaddyLive domain"/>
            <span style="font-size:10px;color:var(--text3)">Path:</span>
            <select id="iptv-dl-pattern" onchange="iptvSetDaddylivePattern(this.value)"
              style="font-size:10px;padding:3px 5px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r)">
              <option value="stream">/stream/stream-N.php</option>
              <option value="live">/live/stream-N.php</option>
              <option value="embed">/embed/stream-N.php</option>
              <option value="premium">/premium-streams/stream-N.php</option>
            </select>
          </div>
          <button onclick="iptvBrowseChannels()" class="btn" style="padding:6px 14px;font-size:12px" title="Browse channels in a panel">🔍 Browse</button>
          <button onclick="iptvShowAddChannel()" class="btn-primary" style="padding:6px 14px;font-size:12px">＋ Channel</button>
          <button class="btn-primary" onclick="iptvReload()">↺ Refresh</button>
        </div>
      </div>

      <!-- ── CHANNELS VIEW ── -->
      <div id="iptv-channels-view">
        <div style="display:grid;grid-template-columns:260px 1fr;gap:14px;height:calc(100vh - 230px);min-height:560px">

          <!-- Left: channel browser -->
          <div style="display:flex;flex-direction:column;gap:8px;min-height:0">
            <div class="search-wrap" style="margin:0;flex:none">
              <svg class="search-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/></svg>
              <input type="text" id="iptv-search" placeholder="Search channels…" oninput="iptvFilterChannels()">
            </div>
            <div id="iptv-cat-pills" style="display:flex;gap:4px;flex-wrap:wrap"></div>
            <div id="iptv-channel-list"
                 style="flex:1;overflow-y:auto;background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);padding:4px;min-height:0">
              <div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">Loading channels…</div>
            </div>
          </div>

          <!-- Right: player -->
          <div style="display:flex;flex-direction:column;gap:8px;min-height:0">
            <!-- Now-playing bar -->
            <div style="display:flex;align-items:center;justify-content:space-between;background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:8px 14px">
              <div style="display:flex;align-items:center;gap:10px">
                <span style="font-size:18px">📺</span>
                <div>
                  <div id="iptv-now-playing" style="font-size:13px;font-weight:600;color:var(--text)">Select a channel</div>
                  <div id="iptv-now-source" style="font-size:10px;color:var(--text3)">MovieBite · Select a channel</div>
                </div>
              </div>
              <div style="display:flex;gap:6px">
                <button class="btn" style="font-size:11px;padding:3px 10px" onclick="iptvAddToMultiview()" title="Add to multiview">⊞ Multiview</button>
                <button class="btn" style="font-size:11px;padding:3px 10px" onclick="iptvFullscreen()" title="Fullscreen">⛶ Fullscreen</button>
                <button class="btn" style="font-size:11px;padding:3px 10px" id="iptv-popout-btn" onclick="iptvPopout()" title="Open in new tab">↗ Pop-out</button>
              </div>
            </div>
            <!-- Player iframe -->
            <div id="iptv-player-wrap" style="position:relative;background:#000;border-radius:var(--r);overflow:hidden;flex:1;min-height:0">
              <div id="iptv-player-placeholder" style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;color:var(--text3)">
                <div style="font-size:48px">📡</div>
                <div style="font-size:14px">Click a channel to start watching</div>
                <div style="font-size:11px;color:var(--text3)">Live channels via MovieBite — select a channel to watch</div>
              </div>
              <!--
                CSS header-crop trick: parent has overflow:hidden + position:relative.
                The iframe is pushed up by 55px (MovieBite site header height) so the
                site navigation is hidden and only the player fills the container.
              -->
              <iframe id="iptv-player-frame"
                src="about:blank"
                style="position:absolute;top:-55px;left:0;width:100%;height:calc(100% + 55px);border:none;display:none"
                allow="autoplay;fullscreen;encrypted-media;picture-in-picture"
                allowfullscreen
              ></iframe>
            </div>
            <!-- Custom HLS URL -->
            <details>
              <summary style="cursor:pointer;font-size:11px;font-weight:600;color:var(--text3);padding:4px 0;user-select:none">🔗 Custom HLS / M3U8 Stream</summary>
              <div style="padding:8px 0;display:flex;gap:8px;flex-wrap:wrap">
                <input type="text" id="iptv-hls-url" placeholder="https://example.com/stream.m3u8"
                  style="flex:1;min-width:240px;padding:7px 10px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);font-size:12px">
                <button class="btn-primary" onclick="iptvPlayHLS()">▶ Play HLS</button>
              </div>
              <video id="iptv-hls-player" controls muted playsinline
                style="width:100%;max-height:280px;background:#000;border-radius:var(--r);margin-top:6px;display:none"></video>
            </details>
          </div>
        </div>
      </div>

      <!-- ── REDDIT COMMENTS MODAL ── -->
      <div id="feeds-reddit-comments-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:960;align-items:center;justify-content:center" onclick="if(event.target===this)feedsCloseComments()">
        <div style="position:relative;width:min(780px,96vw);max-height:90vh;background:var(--bg2);border-radius:12px;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 24px 80px rgba(0,0,0,.7)">
          <div style="display:flex;align-items:flex-start;justify-content:space-between;padding:14px 16px;border-bottom:1px solid var(--border);gap:10px;flex-shrink:0">
            <div id="feeds-comments-title" style="font-size:14px;font-weight:600;color:var(--text);line-height:1.4"></div>
            <button onclick="feedsCloseComments()" style="background:none;border:none;color:var(--text3);font-size:20px;cursor:pointer;line-height:1;padding:2px 6px;flex-shrink:0">✕</button>
          </div>
          <div style="padding:14px 16px 0;flex-shrink:0">
            <div id="feeds-comments-post-body" style="font-size:13px;color:var(--text2);line-height:1.7;white-space:pre-wrap;word-break:break-word;max-height:180px;overflow-y:auto;padding-bottom:10px;border-bottom:1px solid var(--border)"></div>
            <div id="feeds-comments-meta" style="font-size:11px;color:var(--text3);padding:8px 0;display:flex;gap:14px;border-bottom:1px solid var(--border)"></div>
          </div>
          <div id="feeds-comments-list" style="padding:12px 16px;overflow-y:auto;flex:1;display:flex;flex-direction:column;gap:8px">
            <div style="color:var(--text3);font-size:12px">Loading comments…</div>
          </div>
          <div style="padding:10px 16px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;flex-shrink:0">
            <a id="feeds-comments-link" href="#" target="_blank" rel="noopener" style="font-size:11px;color:var(--blue);text-decoration:none">↗ View on Reddit</a>
          </div>
        </div>
      </div>

      <!-- ── REDDIT POST READER MODAL ── -->
      <div id="feeds-reddit-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:950;align-items:center;justify-content:center" onclick="if(event.target===this)feedsCloseRedditPost()">
        <div style="position:relative;width:min(720px,95vw);max-height:85vh;background:var(--bg2);border-radius:12px;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 24px 80px rgba(0,0,0,.7)">
          <div style="display:flex;align-items:flex-start;justify-content:space-between;padding:14px 16px;border-bottom:1px solid var(--border);gap:10px;flex-shrink:0">
            <div id="feeds-reddit-modal-title" style="font-size:14px;font-weight:600;color:var(--text);line-height:1.4"></div>
            <button onclick="feedsCloseRedditPost()" style="background:none;border:none;color:var(--text3);font-size:20px;cursor:pointer;line-height:1;padding:2px 6px;flex-shrink:0">✕</button>
          </div>
          <div id="feeds-reddit-modal-body" style="padding:16px;overflow-y:auto;flex:1;font-size:13px;color:var(--text2);line-height:1.8;white-space:pre-wrap;word-break:break-word"></div>
          <div style="padding:10px 16px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;flex-shrink:0">
            <a id="feeds-reddit-modal-link" href="#" target="_blank" rel="noopener" style="font-size:11px;color:var(--blue);text-decoration:none">↗ View on Reddit</a>
          </div>
        </div>
      </div>

      <!-- ── FEEDS MEDIA PLAYER MODAL ── -->
      <div id="feeds-media-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:950;align-items:center;justify-content:center" onclick="if(event.target===this)feedsCloseMedia()">
        <div style="position:relative;width:min(860px,95vw);background:var(--bg2);border-radius:12px;overflow:hidden;box-shadow:0 24px 80px rgba(0,0,0,.7)">
          <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border)">
            <div id="feeds-media-title" style="font-size:13px;font-weight:600;color:var(--text);line-height:1.3;max-width:calc(100% - 40px)"></div>
            <button onclick="feedsCloseMedia()" style="background:none;border:none;color:var(--text3);font-size:20px;cursor:pointer;line-height:1;padding:2px 6px" title="Close">✕</button>
          </div>
          <div id="feeds-media-body" style="background:#000">
            <iframe id="feeds-media-iframe" src="" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" allowfullscreen style="width:100%;height:480px;display:block;border:none"></iframe>
          </div>
          <div style="padding:10px 16px;display:flex;justify-content:flex-end">
            <a id="feeds-media-extlink" href="#" target="_blank" rel="noopener" style="font-size:11px;color:var(--blue);text-decoration:none">↗ Open on YouTube</a>
          </div>
        </div>
      </div>

      <!-- ── IPTV BROWSE CHANNELS PANEL (inline, not a modal overlay) ── -->
      <div id="iptv-browse-modal" style="display:none;margin-top:12px;border-radius:12px;overflow:hidden;background:var(--bg2);border:1px solid var(--border);flex-direction:column">
        <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 16px;border-bottom:1px solid var(--border);flex-shrink:0;background:var(--surface)">
          <div>
            <span id="iptv-browse-title" style="font-size:13px;font-weight:600">📺 Browse Channels</span>
            <span style="font-size:11px;color:var(--text3);margin-left:10px">Find a channel → add it with ＋ Channel</span>
          </div>
          <button onclick="iptvHideBrowse()" style="background:none;border:none;color:var(--text3);font-size:20px;cursor:pointer;padding:2px 6px" title="Close browser">✕</button>
        </div>
        <div style="height:560px;overflow:hidden;position:relative">
          <iframe id="iptv-browse-iframe" src="" frameborder="0" style="width:100%;height:100%;border:none" allow="autoplay;fullscreen"></iframe>
        </div>
        <div style="padding:10px 16px;border-top:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-shrink:0;background:var(--surface)">
          <span style="font-size:11px;color:var(--text2)">Channel URL: <code style="background:var(--bg3);padding:2px 6px;border-radius:4px;font-size:10px">live.moviebite.cc/channels/<b>SLUG</b></code></span>
          <button onclick="iptvHideBrowse();iptvShowAddChannel()" class="btn-primary" style="padding:5px 14px;font-size:12px;margin-left:auto">＋ Add Channel</button>
        </div>
      </div>

      <!-- ── ADD CHANNEL MODAL ── -->
      <div id="iptv-add-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:900;align-items:center;justify-content:center">
        <div class="panel" style="width:380px;max-width:95vw;padding:20px">
          <div class="panel-title" style="margin-bottom:14px">📺 Add Custom Channel</div>
          <div style="display:flex;flex-direction:column;gap:10px">
            <div>
              <label style="font-size:11px;color:var(--text2);display:block;margin-bottom:4px">Channel Name *</label>
              <input id="iptv-add-name" type="text" placeholder="e.g. Sky Sports F1" style="width:100%;padding:7px 10px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);font-size:12px;box-sizing:border-box">
            </div>
            <div>
              <label style="font-size:11px;color:var(--text2);display:block;margin-bottom:4px">MovieBite Slug (from URL) *</label>
              <input id="iptv-add-slug" type="text" placeholder="e.g. SKY-SPORTS-F1" style="width:100%;padding:7px 10px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);font-size:12px;box-sizing:border-box">
              <div style="font-size:10px;color:var(--text3);margin-top:4px">Visit <a href="https://live.moviebite.cc" target="_blank" style="color:var(--blue)">live.moviebite.cc</a>, find the channel, copy the last part of the URL</div>
            </div>
            <div>
              <label style="font-size:11px;color:var(--text2);display:block;margin-bottom:4px">Group</label>
              <input id="iptv-add-group" type="text" placeholder="Sports, News, Entertainment…" style="width:100%;padding:7px 10px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);font-size:12px;box-sizing:border-box">
            </div>
          </div>
          <div style="display:flex;gap:8px;margin-top:16px">
            <button onclick="iptvHideAddChannel()" class="btn" style="flex:1;padding:8px">Cancel</button>
            <button onclick="iptvSaveAddChannel()" class="btn-primary" style="flex:1;padding:8px">Add Channel</button>
          </div>
        </div>
      </div>

      <!-- ── SCHEDULE VIEW ── -->
      <div id="iptv-schedule-view" style="display:none">
        <div style="display:flex;gap:8px;margin-bottom:12px;align-items:center;flex-wrap:wrap">
          <button class="filter-pill active" id="iptv-sched-live" onclick="iptvLoadSchedule('live',this)">🔴 Live Now</button>
          <button class="filter-pill" id="iptv-sched-all" onclick="iptvLoadSchedule('all',this)">📅 All Events</button>
          <div style="display:flex;align-items:center;gap:6px;margin-left:auto">
            <label style="font-size:11px;color:var(--text3)">⏱ TZ offset:</label>
            <select id="iptv-tz-offset" onchange="iptvSetTZOffset(this.value)" style="font-size:11px;padding:3px 7px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);cursor:pointer">
              <option value="-6">-6h</option><option value="-5">-5h</option><option value="-4">-4h</option>
              <option value="-3">-3h</option><option value="-2">-2h</option><option value="-1">-1h</option>
              <option value="0" selected>+0h</option>
              <option value="1">+1h</option><option value="2">+2h</option><option value="3">+3h</option>
              <option value="4">+4h</option><option value="5">+5h</option><option value="6">+6h</option>
            </select>
          </div>
          <span id="iptv-sched-status" style="font-size:11px;color:var(--text3)"></span>
        </div>
        <div id="iptv-schedule-list" style="display:flex;flex-direction:column;gap:6px">
          <div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">Click "Live Now" or "All Events" to load schedule</div>
        </div>
      </div>

      <!-- ── MULTIVIEW ── -->
      <div id="iptv-multiview-view" style="display:none">
        <div style="display:flex;gap:8px;margin-bottom:12px;align-items:center;flex-wrap:wrap">
          <span style="font-size:12px;font-weight:600;color:var(--text)">Layout:</span>
          <button class="filter-pill active" onclick="iptvMVLayout(1,1,this)">1×1</button>
          <button class="filter-pill" onclick="iptvMVLayout(2,1,this)">2×1</button>
          <button class="filter-pill" onclick="iptvMVLayout(2,2,this)">2×2</button>
          <button class="filter-pill" onclick="iptvMVLayout(3,2,this)">3×2</button>
          <button class="btn" style="margin-left:auto;font-size:11px;padding:3px 10px" onclick="iptvMVClearAll()">✕ Clear All</button>
        </div>
        <div id="iptv-mv-grid" style="display:grid;grid-template-columns:1fr 1fr;gap:8px"></div>
      </div>
    </div>

    <!-- ── RSS / FEEDS ─────────────────────────────── -->
    <div id="tab-feeds" class="tab-panel">
      <!-- ═══════════════════════════════════════════════════════════════════
           FEEDS TAB  (RSS · Reddit · YouTube · Hacker News)
           ═══════════════════════════════════════════════════════════════════ -->

      <!-- Sub-nav + action bar -->
      <div class="section-header" style="margin-bottom:14px">
        <div style="display:flex;align-items:center;gap:8px">
          <svg width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 5c7.18 0 13 5.82 13 13M6 11a7 7 0 017 7M6 17a1 1 0 110 2 1 1 0 010-2z"/></svg>
          <span style="font-weight:600;font-size:15px">Feeds</span>
        </div>
        <div id="feeds-pills-row" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
          <!-- pills are rendered dynamically by loadFeedsTab() -->
        </div>
      </div>

      <!-- ── RSS sub-page ─────────────────────────────────────────────── -->
      <div id="feeds-page-rss">
        <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:10px">
          <div id="feeds-rss-source-tabs" style="display:flex;gap:6px;flex-wrap:wrap"></div>
          <div style="display:flex;align-items:center;gap:6px">
            <select id="feeds-rss-sort" onchange="feedsSortGrid('feeds-rss-grid',this.value)" style="font-size:11px;padding:4px 8px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);cursor:pointer">
              <option value="default">Latest</option>
              <option value="oldest">Oldest</option>
              <option value="az">A–Z</option>
            </select>
            <button id="feeds-rss-view-btn" onclick="feedsToggleView('feeds-rss-grid','feeds-rss-view-btn')" title="Toggle view" style="background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:var(--r);cursor:pointer;font-size:12px">☰</button>
          </div>
        </div>
        <div id="feeds-rss-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:12px"></div>
        <div style="text-align:center;margin-top:14px"><button id="feeds-rss-more-btn" onclick="feedsLoadMore('rss')" style="display:none;background:var(--bg3);border:1px solid var(--border);color:var(--text2);padding:6px 22px;border-radius:var(--r);cursor:pointer;font-size:12px">Load More</button></div>
      </div>

      <!-- ── Reddit sub-page ──────────────────────────────────────────── -->
      <div id="feeds-page-reddit" style="display:none">
        <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:10px">
          <div id="feeds-reddit-source-tabs" style="display:flex;gap:6px;flex-wrap:wrap"></div>
          <div style="display:flex;align-items:center;gap:6px">
            <select id="feeds-reddit-sort" onchange="feedsRedditChangeSort(this.value)" style="font-size:11px;padding:4px 8px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);cursor:pointer">
              <option value="hot">Hot</option>
              <option value="new">New</option>
              <option value="top">Top</option>
              <option value="rising">Rising</option>
            </select>
            <button id="feeds-reddit-view-btn" onclick="feedsToggleView('feeds-reddit-grid','feeds-reddit-view-btn')" title="Toggle view" style="background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:var(--r);cursor:pointer;font-size:12px">☰</button>
          </div>
        </div>
        <div id="feeds-reddit-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:12px"></div>
        <div style="text-align:center;margin-top:14px"><button id="feeds-reddit-more-btn" onclick="feedsRedditLoadMore()" style="display:none;background:var(--bg3);border:1px solid var(--border);color:var(--text2);padding:6px 22px;border-radius:var(--r);cursor:pointer;font-size:12px">Load More</button></div>
      </div>

      <!-- ── YouTube sub-page ─────────────────────────────────────────── -->
      <div id="feeds-page-youtube" style="display:none">
        <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:10px">
          <div id="feeds-yt-channel-tabs" style="display:flex;gap:6px;flex-wrap:wrap"></div>
          <div style="display:flex;align-items:center;gap:6px">
            <select id="feeds-yt-sort" onchange="feedsSortGrid('feeds-yt-grid',this.value)" style="font-size:11px;padding:4px 8px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);cursor:pointer">
              <option value="default">Latest</option>
              <option value="oldest">Oldest</option>
              <option value="az">A–Z</option>
            </select>
            <button id="feeds-yt-view-btn" onclick="feedsToggleView('feeds-yt-grid','feeds-yt-view-btn')" title="Toggle view" style="background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:var(--r);cursor:pointer;font-size:12px">☰</button>
          </div>
        </div>
        <div id="feeds-yt-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px"></div>
        <div style="text-align:center;margin-top:14px"><button id="feeds-yt-more-btn" onclick="feedsLoadMore('youtube')" style="display:none;background:var(--bg3);border:1px solid var(--border);color:var(--text2);padding:6px 22px;border-radius:var(--r);cursor:pointer;font-size:12px">Load More</button></div>
      </div>

      <!-- ── Hacker News sub-page ─────────────────────────────────────── -->
      <div id="feeds-page-hn" style="display:none">
        <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:10px">
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            <button class="filter-pill active" id="hn-sort-frontpage" onclick="feedsHNChangeSort('frontpage',this)">🔥 Front Page</button>
            <button class="filter-pill" id="hn-sort-newest" onclick="feedsHNChangeSort('newest',this)">New</button>
            <button class="filter-pill" id="hn-sort-ask" onclick="feedsHNChangeSort('ask',this)">Ask HN</button>
            <button class="filter-pill" id="hn-sort-show" onclick="feedsHNChangeSort('show',this)">Show HN</button>
          </div>
          <button id="feeds-hn-view-btn" onclick="feedsToggleView('feeds-hn-grid','feeds-hn-view-btn')" title="Toggle view" style="background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:var(--r);cursor:pointer;font-size:12px">☰</button>
        </div>
        <div id="feeds-hn-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px"></div>
        <div style="text-align:center;margin-top:14px"><button id="feeds-hn-more-btn" onclick="feedsHNLoadMore()" style="display:none;background:var(--bg3);border:1px solid var(--border);color:var(--text2);padding:6px 22px;border-radius:var(--r);cursor:pointer;font-size:12px">Load More</button></div>
      </div>

      <!-- ── Twitter/X sub-page ─────────────────────────────────────── -->
      <div id="feeds-page-twitter" style="display:none">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px">
          <div id="feeds-twitter-handle-tabs" style="display:flex;gap:6px;flex-wrap:wrap;flex:1"></div>
          <div style="display:flex;gap:6px;align-items:center">
            <button id="tw-mode-cards-btn" onclick="twitterSetMode('cards')" style="background:var(--accent,#2563eb);color:#fff;border:none;padding:4px 12px;border-radius:var(--r);font-size:11px;cursor:pointer">📋 Cards</button>
            <button id="tw-mode-viewer-btn" onclick="twitterSetMode('viewer')" style="background:var(--bg3);color:var(--text2);border:1px solid var(--border);padding:4px 12px;border-radius:var(--r);font-size:11px;cursor:pointer">🌐 Web Viewer</button>
            <a id="feeds-twitter-open-link" href="#" target="_blank" rel="noopener"
               style="font-size:11px;color:var(--blue);text-decoration:none;white-space:nowrap">↗ Open on X</a>
          </div>
        </div>
        <!-- Cards mode (nitter RSS) -->
        <div id="feeds-twitter-cards-mode">
          <div id="feeds-twitter-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px"></div>
          <div style="text-align:center;margin-top:14px"><button id="feeds-twitter-more-btn" onclick="feedsLoadMore('twitter')" style="display:none;background:var(--bg3);border:1px solid var(--border);color:var(--text2);padding:6px 22px;border-radius:var(--r);cursor:pointer;font-size:12px">Load More</button></div>
          <div id="feeds-twitter-empty" style="display:none;padding:40px 20px;text-align:center;color:var(--text3);font-size:12px">
            <div style="font-size:32px;margin-bottom:8px">𝕏</div>
            <div>No Twitter/X handles added yet.</div>
            <div style="margin-top:6px">Click <strong>Manage</strong> → add a handle like <code>@username</code></div>
          </div>
        </div>
        <!-- Web viewer mode (twitterwebviewer.com via proxy) -->
        <div id="feeds-twitter-viewer-mode" style="display:none">
          <div id="feeds-twitter-webviewer-wrap" style="position:relative;width:100%;height:620px">
            <iframe id="feeds-twitter-webviewer-iframe" src="" style="width:100%;height:100%;border:none;border-radius:var(--r);background:var(--bg2)" allowfullscreen
              onload="feedsTwitterViewerLoaded(this)"
              onerror="feedsTwitterViewerError(this)"></iframe>
            <div id="feeds-twitter-webviewer-fallback" style="display:none;position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;background:var(--bg2);border-radius:var(--r)">
              <div style="font-size:40px">𝕏</div>
              <div style="font-size:13px;color:var(--text2);text-align:center">Twitter/X cannot be embedded in a web frame.<br>Open it directly in a new tab.</div>
              <a id="feeds-twitter-webviewer-extlink" href="#" target="_blank" rel="noopener" class="btn blue" style="padding:8px 18px;font-size:13px">↗ Open on X</a>
            </div>
          </div>
        </div>
      </div>

      <!-- Custom category pages are injected here by JS -->
      <div id="feeds-custom-pages"></div>

      <!-- ── Manage sub-page ──────────────────────────────────────────── -->
      <div id="feeds-page-manage" style="display:none">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
          <span style="font-size:13px;font-weight:600;color:var(--text)">Manage Subscriptions</span>
          <button class="btn-primary" style="font-size:11px;padding:4px 12px" onclick="feedsShowNewCatModal()">＋ New Category</button>
        </div>
        <div id="feeds-manage-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px">
          <!-- rendered dynamically by _feedsRenderManage() -->
        </div>
      </div>

    </div><!-- /tab-feeds -->

    <!-- ═══════════════════════════════════════════════════════════
         FOOTBALL HUB TAB
    ═══════════════════════════════════════════════════════════ -->
    <div id="tab-epl" class="tab-panel">
      <!-- Header -->
      <div class="section-header" style="margin-bottom:10px">
        <div style="display:flex;align-items:center;gap:8px">
          <span style="font-size:22px">⚽</span>
          <div>
            <span style="font-weight:600;font-size:15px">Football Hub</span>
            <div style="font-size:11px;color:var(--text3)">Tables · Fixtures · Results · Highlights · News</div>
          </div>
        </div>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
          <button class="view-btn active" id="football-view-table" onclick="footballSetView('table')">📊 Table</button>
          <button class="view-btn" id="football-view-fixtures" onclick="footballSetView('fixtures')">📅 Fixtures</button>
          <button class="view-btn" id="football-view-results" onclick="footballSetView('results')">✅ Results</button>
          <button class="view-btn" id="football-view-highlights" onclick="footballSetView('highlights')">🎬 Highlights</button>
          <button class="view-btn" id="football-view-news" onclick="footballSetView('news')">📰 News</button>
          <button class="btn-primary" onclick="footballRefresh()">↺ Refresh</button>
        </div>
      </div>

      <!-- League Selector -->
      <div id="football-league-selector" style="margin-bottom:14px;padding-bottom:12px;border-bottom:1px solid var(--border)">
        <div style="font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">🏆 Domestic Leagues</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px">
          <button class="league-pill active" onclick="footballSelectLeague('eng.1')">🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League</button>
          <button class="league-pill" onclick="footballSelectLeague('esp.1')">🇪🇸 La Liga</button>
          <button class="league-pill" onclick="footballSelectLeague('ger.1')">🇩🇪 Bundesliga</button>
          <button class="league-pill" onclick="footballSelectLeague('ita.1')">🇮🇹 Serie A</button>
          <button class="league-pill" onclick="footballSelectLeague('fra.1')">🇫🇷 Ligue 1</button>
          <button class="league-pill" onclick="footballSelectLeague('usa.1')">🇺🇸 MLS</button>
        </div>
        <div style="font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">🌍 International &amp; Club</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">
          <button class="league-pill" onclick="footballSelectLeague('uefa.champions')">🏆 Champions League</button>
          <button class="league-pill" onclick="footballSelectLeague('uefa.europa')">🥈 Europa League</button>
          <button class="league-pill" onclick="footballSelectLeague('fifa.world')">🌍 World Cup</button>
          <button class="league-pill" onclick="footballSelectLeague('uefa.euro')">🇪🇺 Euros</button>
          <button class="league-pill" onclick="footballSelectLeague('conmebol.america')">🌎 Copa America</button>
          <button class="league-pill" onclick="footballSelectLeague('caf.nations')">🌍 AFCON</button>
        </div>
      </div>

      <!-- League title bar -->
      <div id="football-league-title" style="font-size:13px;font-weight:600;margin-bottom:10px;color:var(--text)">🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League</div>

      <!-- Content views -->
      <div id="football-table-view">
        <div id="football-standings" style="overflow-x:auto">
          <div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">Loading standings…</div>
        </div>
      </div>
      <div id="football-fixtures-view" style="display:none">
        <div id="football-fixtures-list" style="display:flex;flex-direction:column;gap:8px;max-width:860px"></div>
      </div>
      <div id="football-results-view" style="display:none">
        <div id="football-results-list" style="display:flex;flex-direction:column;gap:8px;max-width:860px"></div>
      </div>
      <div id="football-highlights-view" style="display:none">
        <div id="football-highlights-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px"></div>
      </div>
      <div id="football-news-view" style="display:none">
        <div id="football-news-list" style="display:flex;flex-direction:column;gap:8px;max-width:860px"></div>
      </div>
    </div><!-- /tab-epl -->

    <!-- Team Detail Modal -->
    <div id="football-team-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:920;align-items:flex-start;justify-content:flex-end;padding:12px">
      <div class="panel" style="width:460px;max-width:95vw;max-height:calc(100vh - 24px);overflow-y:auto;position:relative;padding:0">
        <div style="padding:14px 16px 10px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;position:sticky;top:0;background:var(--bg2);z-index:1">
          <img id="football-team-crest" src="" style="width:28px;height:28px;object-fit:contain" onerror="this.style.display='none'">
          <span id="football-team-name" style="font-weight:600;font-size:14px;flex:1"></span>
          <button onclick="footballCloseTeamModal()" style="background:none;border:none;color:var(--text3);font-size:20px;cursor:pointer;padding:0 4px;line-height:1">✕</button>
        </div>
        <div style="display:flex;border-bottom:1px solid var(--border)">
          <button id="team-tab-btn-fixtures" onclick="footballTeamTab('fixtures')" style="flex:1;padding:9px;background:none;border:none;border-bottom:2px solid var(--accent,#2563eb);color:var(--text);font-size:12px;font-weight:500;cursor:pointer">📅 Fixtures &amp; Results</button>
          <button id="team-tab-btn-news" onclick="footballTeamTab('news')" style="flex:1;padding:9px;background:none;border:none;border-bottom:2px solid transparent;color:var(--text2);font-size:12px;cursor:pointer">📰 News</button>
        </div>
        <div id="football-team-content" style="padding:12px">
          <div style="color:var(--text3);font-size:12px;text-align:center;padding:20px">Loading…</div>
        </div>
      </div>
    </div>

    <!-- New Category modal -->
    <div id="feeds-newcat-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:900;align-items:center;justify-content:center">
      <div class="panel" style="width:340px;max-width:95vw;padding:20px">
        <div class="panel-title" style="margin-bottom:14px">➕ New Feed Category</div>
        <div style="display:flex;flex-direction:column;gap:10px">
          <div>
            <label style="font-size:11px;color:var(--text2);display:block;margin-bottom:4px">Category Name *</label>
            <input id="newcat-name" type="text" placeholder="e.g. Podcasts" style="width:100%;padding:7px 10px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);font-size:12px;box-sizing:border-box">
          </div>
          <div>
            <label style="font-size:11px;color:var(--text2);display:block;margin-bottom:4px">Icon (emoji) *</label>
            <input id="newcat-icon" type="text" placeholder="🎙" maxlength="4" style="width:100%;padding:7px 10px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:var(--r);font-size:16px;box-sizing:border-box">
          </div>
          <div style="font-size:10px;color:var(--text3)">Custom categories use standard RSS/Atom feeds — just like the RSS tab.</div>
        </div>
        <div style="display:flex;gap:8px;margin-top:16px">
          <button onclick="feedsHideNewCatModal()" class="btn" style="flex:1;padding:8px">Cancel</button>
          <button onclick="feedsSaveNewCat()" class="btn-primary" style="flex:1;padding:8px">Create Category</button>
        </div>
      </div>
    </div>

    <!-- Add Feed Modal -->
    <div id="feeds-add-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:900;display:none;align-items:center;justify-content:center">
      <div class="panel" style="margin:0;width:420px;max-width:95vw;position:relative">
        <button onclick="feedsHideAddModal()" style="position:absolute;top:10px;right:10px;background:none;border:none;color:var(--text3);font-size:16px;cursor:pointer">✕</button>
        <div class="panel-title" id="feeds-add-modal-title">Add Feed</div>
        <div style="display:flex;flex-direction:column;gap:10px;padding:4px 0">
          <div>
            <label style="font-size:11px;color:var(--text3);display:block;margin-bottom:4px">NAME</label>
            <input id="feeds-add-name" type="text" placeholder="e.g. TechCrunch"
              style="width:100%;padding:8px 10px;background:var(--bg3);border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-size:13px">
          </div>
          <div id="feeds-add-url-row">
            <label style="font-size:11px;color:var(--text3);display:block;margin-bottom:4px" id="feeds-add-url-label">RSS URL</label>
            <input id="feeds-add-url" type="text" placeholder="https://..."
              style="width:100%;padding:8px 10px;background:var(--bg3);border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-size:13px">
          </div>
          <div id="feeds-add-id-row" style="display:none">
            <label style="font-size:11px;color:var(--text3);display:block;margin-bottom:4px" id="feeds-add-id-label">Channel ID</label>
            <input id="feeds-add-id" type="text" placeholder="UCsBjURrPoezykLs9EqgamOA"
              style="width:100%;padding:8px 10px;background:var(--bg3);border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-size:13px">
          </div>
        </div>
        <div style="display:flex;gap:8px;margin-top:14px">
          <button onclick="feedsHideAddModal()" class="btn" style="flex:1;padding:8px">Cancel</button>
          <button onclick="feedsSaveAdd()" class="btn blue" style="flex:1;padding:8px">Save</button>
        </div>
      </div>
    </div>

    <!-- Old IPTV/Live stuff - keeping for backward compat
         (commented out to avoid duplicate modals)
          <!--
          <a href="https://pluto.tv" target="_blank" rel="noopener noreferrer" class="panel" style="text-decoration:none;display:flex;flex-direction:column;align-items:center;gap:5px;padding:14px;cursor:pointer;margin:0" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor=''">
            <div style="font-size:24px">📡</div><div style="font-weight:600;color:var(--text);font-size:12px">Pluto TV</div>
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
<nav id="bottom-nav" style="overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none">
  <button class="bn-item active" onclick="showTab('overview',this);closeSidebar()">
    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 5a1 1 0 011-1h4a1 1 0 011 1v5a1 1 0 01-1 1H5a1 1 0 01-1-1V5zm10 0a1 1 0 011-1h4a1 1 0 011 1v2a1 1 0 01-1 1h-4a1 1 0 01-1-1V5zM4 15a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1H5a1 1 0 01-1-1v-4zm10-3a1 1 0 011-1h4a1 1 0 011 1v7a1 1 0 01-1 1h-4a1 1 0 01-1-1v-7z"/></svg>
    <span>Home</span>
  </button>
  <button class="bn-item" onclick="showTab('containers',this);closeSidebar()">
    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/></svg>
    <span>Docker</span>
  </button>
  <button class="bn-item" onclick="showTab('deploy',this);closeSidebar()">
    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6m0 0v6m0-6h6m-6 0H6"/></svg>
    <span>Deploy</span>
  </button>
  <button class="bn-item" onclick="showTab('feeds',this);closeSidebar()">
    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 5c7.18 0 13 5.82 13 13M6 11a7 7 0 017 7M6 17a1 1 0 110 2 1 1 0 010-2z"/></svg>
    <span>Feeds</span>
  </button>
  <button class="bn-item" onclick="showTab('iptv',this);closeSidebar()">
    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.069A1 1 0 0121 8.82v6.36a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
    <span>IPTV</span>
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
let catFilter = 'All';

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
    // Auto-close sidebar on mobile after tab selection
    if (window.innerWidth <= 900) closeSidebar();

    // Lazy-load on first show
    if (name === 'overview') { updateGreeting(); loadServiceLauncher(); }
    else if (name === 'containers') loadContainers();
    else if (name === 'stornet') { loadStorage(); loadNetwork(); loadHardware(); }
    else if (name === 'ports') loadPortMap();
    else if (name === 'logs') loadLogs();
    else if (name === 'deploy') loadCatalog();
    else if (name === 'stack') { loadStackManager(); loadDeployHistory(); }
    else if (name === 'backup') loadBackups();
    else if (name === 'updates') checkUpdates();
    else if (name === 'settings') loadSettings();
    else if (name === 'feeds') loadFeedsTab();
    else if (name === 'epl') footballInit();
    else if (name === 'iptv') iptvInit();
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
    if (!ctx || typeof Chart === 'undefined') return null;
    return new Chart(ctx.getContext('2d'), {
        type: 'doughnut',
        data: {
            datasets: [{
                data: [0, 100],
                backgroundColor: [color, 'rgba(48,54,61,0.6)'],
                borderWidth: 0,
                borderRadius: 6,
                hoverOffset: 0
            }]
        },
        options: {
            responsive: false,
            cutout: '72%',
            circumference: 360,
            rotation: -90,
            animation: { duration: 500, easing: 'easeOutQuart' },
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
            // Load average bars — cap at cpuCount cores (fallback 8)
            const _cores = window._cpuCount || 8;
            const _loadPct = (v) => Math.min(parseFloat(v) / _cores * 100, 100).toFixed(1);
            const lb1 = document.getElementById('load-1m-bar');
            const lb5 = document.getElementById('load-5m-bar');
            const lb15 = document.getElementById('load-15m-bar');
            if (lb1) { const p=_loadPct(d.load_1m); lb1.style.width=p+'%'; lb1.className='pbar '+(p<50?'blue':p<80?'':''); lb1.style.background=p>=80?'var(--red)':p>=50?'var(--yellow)':''; }
            if (lb5) { const p=_loadPct(d.load_5m); lb5.style.width=p+'%'; lb5.className='pbar '+(p<50?'blue':''); lb5.style.background=p>=80?'var(--red)':p>=50?'var(--yellow)':''; }
            if (lb15) { const p=_loadPct(d.load_15m); lb15.style.width=p+'%'; lb15.className='pbar '+(p<50?'blue':''); lb15.style.background=p>=80?'var(--red)':p>=50?'var(--yellow)':''; }
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
        setEl('si-hostname', d.hostname || '—');
        setEl('si-uptime', d.uptime_display || '—');
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
        const lines = (d.logs || '').split('\n');
        if (!lines.length || (lines.length === 1 && !lines[0])) {
            el.textContent = '(empty)';
        } else {
            el.innerHTML = lines.map(_colorLogLine).join('\n');
        }
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
        const _seenDevices = new Set();
        d.filesystems.forEach(fs => {
            if (fs.device && _seenDevices.has(fs.device)) return;
            if (fs.device) _seenDevices.add(fs.device);
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
                  <div style="position:relative;width:130px;height:130px">
                    <canvas id="c-${safeId}" width="130" height="130"></canvas>
                    <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none">
                      <span id="pct-${safeId}" style="font-size:22px;font-weight:700;font-family:var(--mono);color:${clr}">${pct}%</span>
                    </div>
                  </div>
                  <div style="font-size:12px;font-weight:600;color:var(--text);text-align:center;word-break:break-all">${mount}</div>
                  <div style="font-size:11px;color:var(--text3)">${fmtBytes(fs.used)} / ${fmtBytes(fs.total)}</div>`;
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

// ── Dashboard Storage widget (compact bar-style, reuses /api/storage) ─
async function loadDashStorage() {
    const el = document.getElementById('dash-storage-list');
    if (!el) return;
    try {
        const r = await fetch(API + '/api/storage');
        const d = await r.json();
        if (!d.filesystems || !d.filesystems.length) {
            el.innerHTML = '<div style="color:var(--text3);font-size:12px;text-align:center;padding:16px">No filesystem data</div>';
            return;
        }
        const fsList = d.filesystems.filter(fs =>
            !['tmpfs','devtmpfs','squashfs','overlay','none'].includes(fs.fstype || '') &&
            !((fs.device || '').startsWith('/dev/loop'))
        );
        el.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fill,minmax(80px,1fr));gap:8px;padding:4px 2px;overflow-y:auto;max-height:300px';
        // Destroy existing charts before rebuild
        if (!window._dashDiskCharts) window._dashDiskCharts = {};
        Object.values(window._dashDiskCharts).forEach(c => { try { c.destroy(); } catch(e){} });
        window._dashDiskCharts = {};
        el.innerHTML = fsList.map(fs => {
            const pct  = Math.round(fs.percent || 0);
            const mount = (fs.mountpoint || fs.device || '?');
            const label = mount.length > 12 ? mount.split('/').pop() || mount.slice(-10) : mount;
            const safeId = 'ds-' + mount.replace(/[^a-zA-Z0-9]/g,'_');
            const clr = pct < 70 ? '#3fb950' : pct < 90 ? '#e3b341' : '#f85149';
            return `<div style="display:flex;flex-direction:column;align-items:center;gap:4px;padding:8px 4px;background:var(--bg3);border:1px solid var(--border);border-radius:8px" title="${mount}&#10;${fmtBytes(fs.used)} / ${fmtBytes(fs.total)}">
              <div style="position:relative;width:64px;height:64px">
                <canvas id="${safeId}" width="64" height="64"></canvas>
                <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none">
                  <span style="font-size:13px;font-weight:700;font-family:var(--mono);color:${clr}">${pct}%</span>
                </div>
              </div>
              <div style="font-size:9px;color:var(--text2);text-align:center;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:76px" title="${mount}">${label}</div>
            </div>`;
        }).join('');
        // Draw donuts
        fsList.forEach(fs => {
            const pct  = Math.round(fs.percent || 0);
            const mount = fs.mountpoint || fs.device || '?';
            const safeId = 'ds-' + mount.replace(/[^a-zA-Z0-9]/g,'_');
            const canvas = document.getElementById(safeId);
            if (!canvas || typeof Chart === 'undefined') return;
            const clr = pct < 70 ? '#3fb950' : pct < 90 ? '#e3b341' : '#f85149';
            window._dashDiskCharts[safeId] = new Chart(canvas.getContext('2d'), {
                type: 'doughnut',
                data: { datasets: [{ data: [pct, 100-pct], backgroundColor: [clr, 'rgba(48,54,61,0.6)'], borderWidth: 0, borderRadius: 4, hoverOffset: 0 }] },
                options: { cutout:'70%', circumference:360, rotation:-90, responsive:false, animation:{duration:600}, plugins:{legend:{display:false},tooltip:{enabled:false}} }
            });
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

        // Init charts on first call (force resize to handle zero-dimension canvas on tab-open)
        if (!_netTxChart) { _netTxChart = _netChartInit('net-tx-chart', 'TX', 'rgb(56,139,253)'); if (_netTxChart) setTimeout(() => _netTxChart.resize(), 50); }
        if (!_netRxChart) { _netRxChart = _netChartInit('net-rx-chart', 'RX', 'rgb(63,185,80)'); if (_netRxChart) setTimeout(() => _netRxChart.resize(), 50); }

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
let _hwCpuChart = null, _hwMemChart = null;

async function loadHardware() {
    try {
        const r = await fetch(API + '/api/hardware');
        const d = await r.json();

        // Store core count globally for load-bar scaling
        if (d.cpu && d.cpu.count) window._cpuCount = d.cpu.count;

        // ── CPU pie chart ──
        if (d.cpu) {
            const pct   = d.cpu.percent ?? 0;
            const color = pct < 50 ? '#3fb950' : pct < 80 ? '#e3b341' : '#f85149';
            const cpuCtx = document.getElementById('hw-cpu-chart');
            if (cpuCtx) {
                if (_hwCpuChart) { _hwCpuChart.destroy(); _hwCpuChart = null; }
                _hwCpuChart = new Chart(cpuCtx, {
                    type: 'doughnut',
                    data: { datasets: [{ data: [pct, 100-pct],
                        backgroundColor: [color, 'rgba(48,54,61,0.6)'], borderWidth: 0, hoverOffset: 0 }] },
                    options: { responsive: false, cutout: '70%',
                        animation: { duration: 600 },
                        plugins: { legend: { display: false }, tooltip: { enabled: false } } }
                });
            }
            document.getElementById('hw-cpu-pct').textContent  = Math.round(pct) + '%';
            document.getElementById('hw-cpu-detail').textContent =
                `${d.cpu.count||'—'} cores · ${d.cpu.freq?.current ? Math.round(d.cpu.freq.current)+'MHz' : '—'}`;
        }

        // ── Memory pie chart ──
        if (d.memory) {
            const m    = d.memory;
            const pct  = m.percent ?? 0;
            const color = pct < 50 ? '#3fb950' : pct < 80 ? '#e3b341' : '#f85149';
            const memCtx = document.getElementById('hw-mem-chart');
            if (memCtx) {
                if (_hwMemChart) { _hwMemChart.destroy(); _hwMemChart = null; }
                _hwMemChart = new Chart(memCtx, {
                    type: 'doughnut',
                    data: { datasets: [{ data: [pct, 100-pct],
                        backgroundColor: [color, 'rgba(48,54,61,0.6)'], borderWidth: 0, hoverOffset: 0 }] },
                    options: { responsive: false, cutout: '70%',
                        animation: { duration: 600 },
                        plugins: { legend: { display: false }, tooltip: { enabled: false } } }
                });
            }
            document.getElementById('hw-mem-pct').textContent    = Math.round(pct) + '%';
            document.getElementById('hw-mem-detail').textContent  =
                `${fmtBytes(m.used)} / ${fmtBytes(m.total)}`;
        }
    } catch(e) {}
}

// ── Logs ─────────────────────────────────────────────────────────────
function _colorLogLine(line) {
    const esc = line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    const lo = line.toLowerCase();
    let color = '';
    if (/\b(emerg|emergency|crit|critical|alert|panic)\b/.test(lo))
        color = 'color:#ff4444;font-weight:700';
    else if (/\b(err|error|failed|failure|exception|traceback)\b/.test(lo))
        color = 'color:var(--red,#f85149)';
    else if (/\b(warn|warning|deprecated)\b/.test(lo))
        color = 'color:var(--orange,#d29922)';
    else if (/\b(info|started|starting|loaded|ready|success|ok)\b/.test(lo))
        color = 'color:var(--green,#3fb950)';
    else if (/\b(debug|trace|verbose)\b/.test(lo))
        color = 'color:var(--text3,#6e7681)';
    return color ? `<span style="${color}">${esc}</span>` : `<span>${esc}</span>`;
}

async function loadLogs() {
    const el = document.getElementById('log-output');
    el.textContent = 'Loading...';
    try {
        const r = await fetch(API + '/api/logs');
        const d = await r.json();
        const lines = d.lines || [];
        if (!lines.length) { el.textContent = '(empty)'; return; }
        el.innerHTML = lines.map(_colorLogLine).join('\n');
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
        setInput('svc-football-key', s.football_api_key);
        setInput('cfg-weather-city',    s.weather_city);
        setInput('cfg-weather-country', s.weather_country);
        setInput('cfg-reddit-client-id',     s.reddit_client_id     || '');
        setInput('cfg-reddit-client-secret', s.reddit_client_secret || '');
        setInput('cfg-reddit-username',      s.reddit_username      || '');
        setInput('cfg-reddit-password',      s.reddit_password      || '');
        setInput('svc-qbit-url',    s.qbittorrent_url);
        setInput('svc-qbit-user',   s.qbittorrent_user);
        setInput('svc-qbit-pass',   s.qbittorrent_pass);
        // Downloader settings
        if (document.getElementById('svc-dl-type')) { document.getElementById('svc-dl-type').value = s.downloader_type || 'qbittorrent'; dlTypeChanged(); }
        if (document.getElementById('svc-transmission-url')) document.getElementById('svc-transmission-url').value = s.transmission_url || '';
        if (document.getElementById('svc-transmission-user')) document.getElementById('svc-transmission-user').value = s.transmission_user || '';
        if (document.getElementById('svc-transmission-pass')) document.getElementById('svc-transmission-pass').value = s.transmission_pass || '';
        if (document.getElementById('svc-deluge-url')) document.getElementById('svc-deluge-url').value = s.deluge_url || '';
        if (document.getElementById('svc-deluge-pass')) document.getElementById('svc-deluge-pass').value = s.deluge_pass || '';
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

        // Current conditions
        const temp = d.current_temp != null ? Math.round(d.current_temp) : (w.temp_max != null ? Math.round(w.temp_max) : '—');
        setEl('weather-temp', temp + '°C');
        setEl('weather-desc', w.desc || 'Clear');
        setEl('weather-location', d.location || '');
        setEl('weather-humidity', d.humidity != null ? d.humidity + '%' : '—');
        setEl('weather-wind', d.wind_mph != null ? Math.round(d.wind_mph) + ' mph' : '—');
        setEl('weather-feels', d.feels_like != null ? Math.round(d.feels_like) + '°C' : '—');
        const iconEl = document.getElementById('weather-icon');
        if (iconEl && w.icon) iconEl.textContent = w.icon;

        // Glance-style 7-day forecast grid
        const forecastEl = document.getElementById('weather-forecast');
        if (forecastEl && d.daily) {
            const days = d.daily.slice(0, 7);
            forecastEl.style.gridTemplateColumns = `repeat(${days.length}, 1fr)`;
            forecastEl.innerHTML = days.map((day, i) => {
                const dt = new Date(day.date + 'T12:00');
                const dayLabel = i === 0 ? 'Today' : dt.toLocaleDateString(undefined, {weekday:'short'});
                const dateNum  = dt.getDate();
                const hi = day.temp_max != null ? Math.round(day.temp_max) : '—';
                const lo = day.temp_min != null ? Math.round(day.temp_min) : '—';
                const isToday = i === 0;
                return `<div style="display:flex;flex-direction:column;align-items:center;gap:2px;padding:7px 2px;background:${isToday?'var(--bg3)':'transparent'};border-radius:var(--r);text-align:center">
                  <div style="font-size:10px;font-weight:${isToday?'700':'400'};color:${isToday?'var(--blue)':'var(--text3)'}">${dayLabel}</div>
                  <div style="font-size:10px;color:var(--text3)">${dateNum}</div>
                  <div style="font-size:22px;line-height:1.3;margin:2px 0">${day.icon || '🌤️'}</div>
                  <div style="font-size:12px;font-weight:600;color:var(--text)">${hi}°</div>
                  <div style="font-size:10px;color:var(--text3)">${lo}°</div>
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

// ══════════════════════════════════════════════════════════════════════
// FEEDS TAB — RSS · Reddit · YouTube · Hacker News
// ══════════════════════════════════════════════════════════════════════
let _feedsSubs     = {rss:[], reddit:[], youtube:[]};
let _feedsPage     = 'rss';
let _feedsRssActive = null;   // currently selected RSS source id
let _feedsRedditActive = null;
let _feedsYTActive = null;
let _feedsAddType  = 'rss';
// Feed view/sort/pagination state
let _feedsViewMode  = {};   // gridId → 'grid'|'list'
let _feedsAllItems  = {};   // gridId → full item array
let _feedsOffset    = {};   // gridId → current render offset
const _FEEDS_PAGE_SIZE = 12;
// Reddit pagination / sort state
let _feedsRedditSort  = 'hot';
let _feedsRedditAfter = null;
// HN sort state
let _hnSort = 'frontpage';
let _hnAllItems = [];
let _hnOffset = 0;
const _hnFeedUrls = {
    frontpage: 'https://hnrss.org/frontpage',
    newest:    'https://hnrss.org/newest',
    ask:       'https://hnrss.org/ask',
    show:      'https://hnrss.org/show'
};

// All known category types (built-in + custom from _type_meta)
let _feedsAllTypes = ['rss','reddit','youtube','hn','twitter']; // built-in order; custom appended
let _feedsTwitterActive = null;

function _feedsBuildNavPills() {
    const row = document.getElementById('feeds-pills-row');
    if (!row) return;
    const meta = _feedsSubs._type_meta || {};
    // Built-in types always shown first
    const builtIn = ['rss','reddit','youtube','hn'];
    const custom   = Object.keys(meta).filter(k => !builtIn.includes(k) && k !== '_type_meta');
    _feedsAllTypes = [...builtIn, ...custom];

    row.innerHTML = [
        ..._feedsAllTypes.map(p => {
            const icon = p === 'rss' ? '📰' : p === 'reddit' ? '🤖' : p === 'youtube' ? '▶' : p === 'hn' ? '🔶' : (meta[p]?.icon || '📡');
            const name = p === 'rss' ? 'RSS' : p === 'reddit' ? 'Reddit' : p === 'youtube' ? 'YouTube' : p === 'hn' ? 'HN' : (meta[p]?.name || p);
            return `<button class="filter-pill${p===_feedsPage?' active':''}" id="feeds-pill-${p}" onclick="feedsNav('${p}',this)">${icon} ${name}</button>`;
        }),
        `<button class="filter-pill${_feedsPage==='manage'?' active':''}" id="feeds-pill-manage" onclick="feedsNav('manage',this)" style="margin-left:auto">⚙ Manage</button>`
    ].join('');
}

function feedsNav(page, el) {
    _feedsPage = page;
    // Hide all known pages
    [..._feedsAllTypes, 'manage'].forEach(p => {
        const pg = document.getElementById('feeds-page-' + p);
        if (pg) pg.style.display = (p === page) ? '' : 'none';
        const pill = document.getElementById('feeds-pill-' + p);
        if (pill) pill.classList.toggle('active', p === page);
    });
    if (page === 'rss')     _feedsLoadRssPage();
    else if (page === 'reddit')  _feedsLoadRedditPage();
    else if (page === 'youtube') _feedsLoadYTPage();
    else if (page === 'hn')      _feedsLoadHN(false);
    else if (page === 'twitter') _feedsLoadTwitterPage();
    else if (page === 'manage')  _feedsRenderManage();
    else                         _feedsLoadCustomPage(page);
}

function feedsRefreshCurrent() { feedsNav(_feedsPage, null); }

// ── Bootstrap (called when Feeds tab is shown) ───────────────────────
async function loadFeedsTab() {
    try {
        const r = await fetch(API + '/api/feeds/subscriptions');
        _feedsSubs = await r.json();
    } catch(e) { _feedsSubs = {rss:[], reddit:[], youtube:[], _type_meta:{}}; }
    // Ensure _type_meta exists
    if (!_feedsSubs._type_meta) _feedsSubs._type_meta = {};
    _feedsBuildNavPills();
    // Inject custom page divs
    _feedsInjectCustomPageDivs();
    feedsNav('rss', document.getElementById('feeds-pill-rss'));
}

function _feedsInjectCustomPageDivs() {
    const container = document.getElementById('feeds-custom-pages');
    if (!container) return;
    const meta = _feedsSubs._type_meta || {};
    const builtIn = ['rss','reddit','youtube','hn'];
    const custom = Object.keys(meta).filter(k => !builtIn.includes(k));
    container.innerHTML = custom.map(k => `
      <div id="feeds-page-${k}" style="display:none">
        <div id="feeds-${k}-source-tabs" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px"></div>
        <div id="feeds-${k}-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px"></div>
        <div style="text-align:center;margin-top:14px"><button id="feeds-${k}-more-btn" onclick="feedsLoadMore('${k}')" style="display:none;background:var(--bg3);border:1px solid var(--border);color:var(--text2);padding:6px 22px;border-radius:var(--r);cursor:pointer;font-size:12px">Load More</button></div>
      </div>`).join('');
}

// ── RSS sub-page ─────────────────────────────────────────────────────
function _feedsLoadRssPage() {
    const tabs = document.getElementById('feeds-rss-source-tabs');
    const grid = document.getElementById('feeds-rss-grid');
    if (!tabs || !grid) return;
    const sources = _feedsSubs.rss || [];
    if (!sources.length) {
        tabs.innerHTML = '';
        grid.innerHTML = '<div class="empty"><div class="empty-icon">📰</div><div class="empty-text">No RSS feeds — add some in Manage</div></div>';
        return;
    }
    // Source pills
    tabs.innerHTML = sources.map(s =>
        `<button class="filter-pill${s.id === _feedsRssActive ? ' active' : ''}"
            onclick="_feedsSelectRss('${s.id}',this)">${s.name}</button>`
    ).join('');
    // Auto-select first if none active
    if (!_feedsRssActive || !sources.find(s => s.id === _feedsRssActive))
        _feedsRssActive = sources[0].id;
    // Update active pill
    tabs.querySelectorAll('.filter-pill').forEach(p => p.classList.remove('active'));
    const activePill = [...tabs.querySelectorAll('.filter-pill')].find(p => p.textContent === (sources.find(s=>s.id===_feedsRssActive)?.name));
    if (activePill) activePill.classList.add('active');
    _feedsFetchAndRenderCards(sources.find(s => s.id === _feedsRssActive)?.url, grid, 'rss');
}

function _feedsSelectRss(id, el) {
    _feedsRssActive = id;
    document.querySelectorAll('#feeds-rss-source-tabs .filter-pill').forEach(p => p.classList.remove('active'));
    if (el) el.classList.add('active');
    const src = (_feedsSubs.rss || []).find(s => s.id === id);
    if (src) _feedsFetchAndRenderCards(src.url, document.getElementById('feeds-rss-grid'), 'rss');
}

// ── Reddit sub-page ──────────────────────────────────────────────────
function _feedsLoadRedditPage() {
    const tabs = document.getElementById('feeds-reddit-source-tabs');
    const grid = document.getElementById('feeds-reddit-grid');
    if (!tabs || !grid) return;
    const sources = _feedsSubs.reddit || [];
    if (!sources.length) {
        tabs.innerHTML = '';
        grid.innerHTML = '<div class="empty"><div class="empty-icon">🤖</div><div class="empty-text">No subreddits — add some in Manage</div></div>';
        return;
    }
    tabs.innerHTML = sources.map(s =>
        `<button class="filter-pill${s.id === _feedsRedditActive ? ' active' : ''}"
            onclick="_feedsSelectReddit('${s.id}',this)">${s.name}</button>`
    ).join('');
    if (!_feedsRedditActive || !sources.find(s => s.id === _feedsRedditActive))
        _feedsRedditActive = sources[0].id;
    tabs.querySelectorAll('.filter-pill').forEach(p => p.classList.remove('active'));
    const activePill = [...tabs.querySelectorAll('.filter-pill')].find(p => p.textContent === (sources.find(s=>s.id===_feedsRedditActive)?.name));
    if (activePill) activePill.classList.add('active');
    _feedsFetchRedditDirect(sources.find(s => s.id === _feedsRedditActive)?.url, grid, false);
}

function _feedsSelectReddit(id, el) {
    _feedsRedditActive = id;
    document.querySelectorAll('#feeds-reddit-source-tabs .filter-pill').forEach(p => p.classList.remove('active'));
    if (el) el.classList.add('active');
    const src = (_feedsSubs.reddit || []).find(s => s.id === id);
    if (src) _feedsFetchRedditDirect(src.url, document.getElementById('feeds-reddit-grid'), false);
}

// ── Reddit: fetch directly from browser (bypasses server IP blocks) ──
async function _feedsFetchRedditDirect(url, grid, appendMode) {
    if (!url || !grid) return;
    const moreBtn = document.getElementById('feeds-reddit-more-btn');
    if (!appendMode) {
        grid.innerHTML = '<div class="skeleton" style="height:200px;border-radius:var(--r)"></div>'.repeat(6);
        _feedsRedditAfter = null;
    }
    const m = (url||'').match(/reddit\.com\/r\/([A-Za-z0-9_]+)/);
    if (!m) { grid.innerHTML = '<div class="empty" style="grid-column:1/-1"><div class="empty-icon">📭</div><div class="empty-text">Invalid Reddit URL</div></div>'; return; }
    const sub = m[1];
    const safe = t => (t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    const sort = _feedsRedditSort || 'hot';
    try {
        // Always use server-side proxy — handles NSFW age-gate and CORS reliably
        const r2 = await fetch(API + `/api/reddit/feed?sub=${encodeURIComponent(sub)}&sort=${sort}&limit=25${_feedsRedditAfter?'&after='+encodeURIComponent(_feedsRedditAfter):''}`);
        if (!r2.ok) throw new Error(`Server proxy error: HTTP ${r2.status}`);
        const resp2 = await r2.json();
        if (!resp2.ok) throw new Error(resp2.error || 'Reddit fetch failed');
        var data = {data: resp2.data};
        _feedsRedditAfter = data?.data?.after || null;
        const posts = (data?.data?.children || []).filter(p=>p.kind==='t3');
        if (!posts.length && !appendMode) {
            grid.innerHTML = '<div class="empty" style="grid-column:1/-1"><div class="empty-icon">📭</div><div class="empty-text">No posts found</div></div>';
            if (moreBtn) moreBtn.style.display = 'none';
            return;
        }
        const html = posts.map(post => {
            const pd = post.data || {};
            const title = pd.title || 'Untitled';
            const permalink = 'https://www.reddit.com' + (pd.permalink || '#');
            const created = pd.created_utc;
            const dateStr = created ? new Date(created*1000).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}) : '';
            const postHint = pd.post_hint || '';
            const isVideo = pd.is_video || false;
            const isGallery = pd.is_gallery || false;
            const domain = pd.domain || '';
            const postUrl = pd.url || permalink;
            const isVideoLink = isVideo || postHint==='rich:video' || domain.includes('v.redd.it') || domain.includes('youtube.com') || domain.includes('youtu.be');
            const isGif = /\.(gif|gifv)$/i.test(postUrl) || domain.includes('i.imgur.com');
            const isImage = postHint==='image' || /\.(jpg|jpeg|png|webp)$/i.test(postUrl);
            const ptype = isVideoLink ? 'video' : isGif ? 'gif' : isGallery ? 'gallery' : isImage ? 'image' : 'text';
            let thumb = null;
            try { const imgs = pd.preview?.images; if (imgs?.length) thumb = imgs[0].source.url.replace(/&amp;/g,'&'); } catch(e){}
            if (!thumb && isGallery) { try { const k=Object.keys(pd.media_metadata)[0]; thumb=pd.media_metadata[k].s.u.replace(/&amp;/g,'&'); } catch(e){} }
            if (!thumb) { const tn=pd.thumbnail||''; if(tn.startsWith('http')&&!['self','default','spoiler'].includes(tn)) thumb=tn; }
            const flair = pd.link_flair_text || '';
            const score = pd.score || 0;
            const numC  = pd.num_comments || 0;
            const typeBadge = ptype==='video'   ? `<div style="position:absolute;top:6px;left:6px;background:rgba(0,0,0,.75);color:#fff;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:600">▶ Video</div>`
                            : ptype==='gif'     ? `<div style="position:absolute;top:6px;left:6px;background:rgba(0,0,0,.75);color:#ff6b6b;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:600">GIF</div>`
                            : ptype==='gallery' ? `<div style="position:absolute;top:6px;left:6px;background:rgba(0,0,0,.75);color:#fff;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:600">🖼 Gallery</div>` : '';
            const icon = ptype==='video'?'▶':ptype==='gif'?'🎞':ptype==='gallery'?'🖼':ptype==='image'?'🖼':'🤖';
            const encodedTitle = encodeURIComponent(title.slice(0,200));
            const encodedPermalink = encodeURIComponent(pd.permalink || permalink);
            return `<a href="${permalink}" onclick="feedsOpenComments(decodeURIComponent('${encodedPermalink}'),decodeURIComponent('${encodedTitle}'));return false;" style="display:flex;flex-direction:column;text-decoration:none;background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;transition:border-color .15s,transform .1s;cursor:pointer" onmouseover="this.style.borderColor='var(--blue)';this.style.transform='translateY(-2px)'" onmouseout="this.style.borderColor='var(--border)';this.style.transform=''">
              ${thumb
                ? `<div style="position:relative;width:100%;padding-top:52%;background:var(--surface2);overflow:hidden"><img src="${thumb}" loading="lazy" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover" onerror="this.parentElement.innerHTML='<div style=\\'display:flex;align-items:center;justify-content:center;height:100%;font-size:36px\\'>${icon}</div>'">${typeBadge}</div>`
                : ptype === 'text' && pd.selftext
                ? `<div style="width:100%;padding-top:52%;position:relative;background:var(--surface2)"><div style="position:absolute;inset:6px;overflow:hidden;font-size:11px;color:var(--text2);line-height:1.5;padding:4px">${safe((pd.selftext||'').slice(0,300))}</div></div>`
                : `<div style="width:100%;padding-top:52%;position:relative;background:var(--surface2)"><div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:36px">${icon}</div>${typeBadge}</div>`}
              <div style="padding:9px 12px 11px;flex:1;display:flex;flex-direction:column;gap:3px">
                <div style="font-size:12px;font-weight:600;color:var(--text);line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${safe(title)}</div>
                <div style="display:flex;align-items:center;gap:10px;font-size:10px;color:var(--text3);margin-top:3px">
                  ${flair?`<span style="background:var(--surface2);padding:1px 5px;border-radius:3px;color:var(--text2);font-size:9px">${safe(flair)}</span>`:''}
                  <span>▲ ${score.toLocaleString()}</span><span>💬 ${numC.toLocaleString()}</span>
                </div>
                <div style="font-size:10px;color:var(--text3);margin-top:auto;padding-top:4px">${safe(dateStr)}</div>
              </div>
            </a>`;
        }).join('');
        if (appendMode) grid.insertAdjacentHTML('beforeend', html);
        else grid.innerHTML = html;
        if (moreBtn) moreBtn.style.display = _feedsRedditAfter ? '' : 'none';
    } catch(e) {
        if (!appendMode) {
            const needsSettings = e.message.includes('Settings') || e.message.includes('username') || e.message.includes('login');
            grid.innerHTML = `<div class="empty" style="grid-column:1/-1">
              <div class="empty-icon">⚠️</div>
              <div class="empty-text" style="color:var(--red);max-width:460px;line-height:1.6">${e.message}</div>
              ${needsSettings ? `<button class="btn blue" style="margin-top:12px" onclick="showTab('settings',null)">⚙️ Open Settings</button>` : ''}
            </div>`;
        }
    }
}

// ── YouTube sub-page ─────────────────────────────────────────────────
function _feedsLoadYTPage() {
    const tabs = document.getElementById('feeds-yt-channel-tabs');
    const grid = document.getElementById('feeds-yt-grid');
    if (!tabs || !grid) return;
    const channels = _feedsSubs.youtube || [];
    if (!channels.length) {
        tabs.innerHTML = '';
        grid.innerHTML = '<div class="empty"><div class="empty-icon">▶</div><div class="empty-text">No YouTube channels — add some in Manage</div></div>';
        return;
    }
    tabs.innerHTML = channels.map(c =>
        `<button class="filter-pill${c.id === _feedsYTActive ? ' active' : ''}"
            onclick="_feedsSelectYT('${c.id}',this)">${c.name}</button>`
    ).join('');
    if (!_feedsYTActive || !channels.find(c => c.id === _feedsYTActive))
        _feedsYTActive = channels[0].id;
    tabs.querySelectorAll('.filter-pill').forEach(p => p.classList.remove('active'));
    const ac = [...tabs.querySelectorAll('.filter-pill')].find(p => p.textContent === (channels.find(c=>c.id===_feedsYTActive)?.name));
    if (ac) ac.classList.add('active');
    _feedsFetchAndRenderCards(channels.find(c => c.id === _feedsYTActive)?.url, grid, 'youtube');
}

function _feedsSelectYT(id, el) {
    _feedsYTActive = id;
    document.querySelectorAll('#feeds-yt-channel-tabs .filter-pill').forEach(p => p.classList.remove('active'));
    if (el) el.classList.add('active');
    const ch = (_feedsSubs.youtube || []).find(c => c.id === id);
    if (ch) _feedsFetchAndRenderCards(ch.url, document.getElementById('feeds-yt-grid'), 'youtube');
}

// ── Hacker News ──────────────────────────────────────────────────────
async function _feedsLoadHN(appendMode) {
    const grid = document.getElementById('feeds-hn-grid');
    const moreBtn = document.getElementById('feeds-hn-more-btn');
    if (!grid) return;
    const feedUrl = _hnFeedUrls[_hnSort] || _hnFeedUrls.frontpage;
    if (!appendMode) {
        grid.innerHTML = '<div class="skeleton" style="height:200px;border-radius:var(--r)"></div>'.repeat(6);
        _hnAllItems = [];
        _hnOffset = 0;
    }
    const safe = t => (t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    try {
        const r = await fetch(API + `/api/rss/fetch?url=${encodeURIComponent(feedUrl)}`);
        const d = await r.json();
        const newItems = d.items || [];
        if (!newItems.length && !appendMode) {
            grid.innerHTML = '<div class="empty" style="grid-column:1/-1"><div class="empty-text">No HN stories</div></div>';
            if (moreBtn) moreBtn.style.display = 'none';
            return;
        }
        _hnAllItems = appendMode ? _hnAllItems.concat(newItems) : newItems;
        const from = appendMode ? _hnOffset : 0;
        const to   = from + _FEEDS_PAGE_SIZE;
        _hnOffset  = to;
        const slice = _hnAllItems.slice(from, to);
        const html = slice.map((item, idx) => {
            const domain = (() => { try { return new URL(item.link||'').hostname.replace('www.',''); } catch(e) { return ''; } })();
            const thumb = item.thumb;
            return `<a href="${item.link||'#'}" target="_blank" rel="noopener"
              style="display:flex;flex-direction:column;text-decoration:none;background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;transition:border-color .15s,transform .1s"
              onmouseover="this.style.borderColor='var(--yellow,#e3b341)';this.style.transform='translateY(-2px)'"
              onmouseout="this.style.borderColor='var(--border)';this.style.transform=''">
              ${thumb
                ? `<div style="position:relative;width:100%;padding-top:52%;background:var(--surface2);overflow:hidden"><img src="${thumb}" loading="lazy" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover" onerror="this.parentElement.innerHTML='<div style=\\'display:flex;align-items:center;justify-content:center;height:100%;font-size:30px\\'>🟠</div>'"></div>`
                : `<div style="width:100%;padding-top:52%;position:relative;background:var(--surface2)" ${item.link&&item.link.startsWith('http')?`data-og-url="${item.link}"`:''}>
                     <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:30px">🟠</div>
                   </div>`}
              <div style="padding:9px 12px 11px;flex:1;display:flex;flex-direction:column;gap:3px">
                <div style="font-size:12px;font-weight:600;color:var(--text);line-height:1.4;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden">${safe(item.title)}</div>
                <div style="font-size:10px;color:var(--text3);margin-top:auto;padding-top:4px;display:flex;align-items:center;gap:6px">
                  ${domain?`<span style="background:var(--surface2);padding:1px 5px;border-radius:3px">${safe(domain)}</span>`:''}
                  <span>${item.date||''}</span>
                </div>
              </div>
            </a>`;
        }).join('');
        if (appendMode) grid.insertAdjacentHTML('beforeend', html);
        else grid.innerHTML = html;
        if (moreBtn) moreBtn.style.display = _hnOffset < _hnAllItems.length ? '' : (_hnAllItems.length >= _FEEDS_PAGE_SIZE ? '' : 'none');
        // Lazy-load og:image
        setTimeout(() => _feedsLazyLoadThumbs(grid), 100);
    } catch(e) {
        if (!appendMode) grid.innerHTML = `<div style="color:var(--red);font-size:12px;padding:8px;grid-column:1/-1">Failed to load Hacker News: ${e.message}</div>`;
    }
}

// ── Generic card fetcher + renderer ─────────────────────────────────
async function _feedsFetchAndRenderCards(url, grid, mode) {
    if (!url || !grid) return;
    const gridId = grid.id;
    const moreBtnId = gridId.endsWith('-grid') ? gridId.replace(/-grid$/, '-more-btn') : null;
    grid.innerHTML = '<div class="skeleton" style="height:200px;border-radius:var(--r)"></div>'.repeat(6);
    try {
        const r = await fetch(API + `/api/rss/fetch?url=${encodeURIComponent(url)}&_t=${Date.now()}`);
        const d = await r.json();
        const items = d.items || [];
        if (!items.length) {
            const errMsg = d.error ? `Error: ${d.error}` : 'No items found';
            grid.innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="empty-icon">📭</div><div class="empty-text">${errMsg}</div></div>`;
            if (moreBtnId) { const b=document.getElementById(moreBtnId); if(b) b.style.display='none'; }
            return;
        }
        const safe = t => (t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

        // Store full item list with pre-rendered HTML for card mode
        const enriched = items.map(item => {
            const isYT   = mode === 'youtube' || (item.link||'').includes('youtube.com');
            const isReddit = mode === 'reddit';
            const ptype  = item.post_type || (isYT ? 'youtube' : 'article');
            const thumb  = item.thumb;
            const typeBadge = (() => {
                if (ptype === 'video')   return `<div style="position:absolute;top:6px;left:6px;background:rgba(0,0,0,.75);color:#fff;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:600">▶ Video</div>`;
                if (ptype === 'gif')     return `<div style="position:absolute;top:6px;left:6px;background:rgba(0,0,0,.75);color:#ff6b6b;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:600">GIF</div>`;
                if (ptype === 'gallery') return `<div style="position:absolute;top:6px;left:6px;background:rgba(0,0,0,.75);color:#fff;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:600">🖼 Gallery</div>`;
                if (isYT)                return `<div style="position:absolute;bottom:6px;right:6px;background:rgba(0,0,0,.8);color:red;padding:2px 6px;border-radius:4px;font-size:11px;font-weight:600">▶ YT</div>`;
                return '';
            })();
            const placeholderIcon = ptype === 'video' ? '▶' : ptype === 'gif' ? '🎞' : ptype === 'gallery' ? '🖼' : ptype === 'image' ? '🖼' : isYT ? '▶' : '📰';
            const redditMeta = isReddit ? `
                <div style="display:flex;align-items:center;gap:10px;font-size:10px;color:var(--text3);margin-top:3px">
                  ${item.flair ? `<span style="background:var(--surface2);padding:1px 5px;border-radius:3px;color:var(--text2);font-size:9px">${safe(item.flair)}</span>` : ''}
                  <span>▲ ${(item.score||0).toLocaleString()}</span>
                  <span>💬 ${(item.num_comments||0).toLocaleString()}</span>
                </div>` : '';
            const aspectRatio = isYT || ptype === 'video' ? '56.25%' : '52%';
            const ytIdMatch = isYT ? (item.link||'').match(/(?:v=|youtu\.be\/)([A-Za-z0-9_-]{11})/) : null;
            const ytId = ytIdMatch ? ytIdMatch[1] : null;
            const cardStyle = `display:flex;flex-direction:column;text-decoration:none;background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;transition:border-color .15s,transform .1s;cursor:pointer`;
            item._html = `
            <a href="${item.link}" ${ytId ? `data-yt-id="${ytId}" onclick="feedsOpenYT(this.dataset.ytId,this.dataset.ytTitle||'');return false;"` : 'target="_blank" rel="noopener"'} data-yt-title="${safe(item.title)}" style="${cardStyle}"
              onmouseover="this.style.borderColor='var(--blue)';this.style.transform='translateY(-2px)'"
              onmouseout="this.style.borderColor='var(--border)';this.style.transform=''">
              ${thumb
                ? `<div style="position:relative;width:100%;padding-top:${aspectRatio};background:var(--surface2);overflow:hidden">
                     <img src="${thumb}" loading="lazy" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover"
                          onerror="this.parentElement.innerHTML='<div style=\\'display:flex;align-items:center;justify-content:center;height:100%;font-size:36px\\'>${placeholderIcon}</div>'">
                     ${typeBadge}
                   </div>`
                : `<div style="width:100%;padding-top:${aspectRatio};position:relative;background:var(--surface2)" ${!isYT && item.link && item.link.startsWith('http') ? `data-og-url="${item.link}"` : ''}>
                     <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:36px">${placeholderIcon}</div>
                     ${typeBadge}
                   </div>`}
              <div style="padding:9px 12px 11px;flex:1;display:flex;flex-direction:column;gap:3px">
                <div style="font-size:12px;font-weight:600;color:var(--text);line-height:1.4;display:-webkit-box;-webkit-line-clamp:${isReddit?2:3};-webkit-box-orient:vertical;overflow:hidden">${safe(item.title)}</div>
                ${(item.excerpt && !isYT) ? `<div style="font-size:11px;color:var(--text2);line-height:1.4;margin-top:2px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${safe(item.excerpt)}</div>` : ''}
                ${redditMeta}
                <div style="font-size:10px;color:var(--text3);margin-top:auto;padding-top:4px;display:flex;align-items:center;gap:4px">
                  <svg width="10" height="10" fill="none" stroke="currentColor" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" stroke-width="2"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6l4 2"/></svg>
                  ${safe(item.date || '')}
                </div>
              </div>
            </a>`;
            return item;
        });

        // Store for sort/pagination
        _feedsAllItems[gridId] = enriched;
        _feedsOffset[gridId] = _FEEDS_PAGE_SIZE;
        const viewMode = _feedsViewMode[gridId] || 'grid';
        _feedsRenderItems(grid, enriched, 0, _FEEDS_PAGE_SIZE, viewMode, false);

        // Show/hide Load More
        if (moreBtnId) {
            const b = document.getElementById(moreBtnId);
            if (b) b.style.display = enriched.length > _FEEDS_PAGE_SIZE ? '' : 'none';
        }
        // Lazy-load og:image for cards without thumbnails
        setTimeout(() => _feedsLazyLoadThumbs(grid), 100);
    } catch(e) {
        grid.innerHTML = `<div style="color:var(--red);font-size:12px;padding:8px;grid-column:1/-1">Failed to load feed: ${e.message}</div>`;
    }
}

// ── og:image lazy thumbnail loader ───────────────────────────────────
async function _feedsLazyLoadThumbs(grid) {
    if (!grid) return;
    const placeholders = Array.from(grid.querySelectorAll('[data-og-url]'));
    if (!placeholders.length) return;
    // Fetch all og:images in parallel (max 8 at a time to avoid overwhelming server)
    const BATCH = 8;
    for (let i = 0; i < placeholders.length; i += BATCH) {
        const batch = placeholders.slice(i, i + BATCH);
        await Promise.all(batch.map(async ph => {
            const artUrl = ph.dataset.ogUrl;
            if (!artUrl) return;
            try {
                const r = await fetch(`/api/feeds/og?url=${encodeURIComponent(artUrl)}`);
                const d = await r.json();
                if (d.img) {
                    ph.innerHTML = `<img src="${d.img}" loading="lazy" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover" onerror="this.style.display='none'">`;
                    ph.removeAttribute('data-og-url');
                }
            } catch(e) { /* silently skip */ }
        }));
    }
}

// ── Reddit post reader modal ──────────────────────────────────────────
function feedsOpenRedditPost(title, text, url) {
    const modal = document.getElementById('feeds-reddit-modal');
    const titleEl = document.getElementById('feeds-reddit-modal-title');
    const body = document.getElementById('feeds-reddit-modal-body');
    const link = document.getElementById('feeds-reddit-modal-link');
    if (!modal) return;
    if (titleEl) titleEl.textContent = title || '';
    if (body) body.textContent = text || '';
    if (link) link.href = url || '#';
    // Escape any hidden parent by moving modal to body level
    if (modal.parentElement !== document.body) document.body.appendChild(modal);
    modal.style.display = 'flex';
    document.body.style.overflow = 'hidden';
}
function feedsCloseRedditPost() {
    const modal = document.getElementById('feeds-reddit-modal');
    if (modal) modal.style.display = 'none';
    document.body.style.overflow = '';
}

// ── Reddit Comments Modal ─────────────────────────────────────────────
async function feedsOpenComments(permalink, title) {
    const modal   = document.getElementById('feeds-reddit-comments-modal');
    const titleEl = document.getElementById('feeds-comments-title');
    const bodyEl  = document.getElementById('feeds-comments-post-body');
    const metaEl  = document.getElementById('feeds-comments-meta');
    const listEl  = document.getElementById('feeds-comments-list');
    const linkEl  = document.getElementById('feeds-comments-link');
    if (!modal) return;
    const fullLink = permalink.startsWith('http') ? permalink : 'https://www.reddit.com' + permalink;
    if (titleEl) titleEl.textContent = title || 'Post';
    if (bodyEl)  bodyEl.textContent  = '';
    if (metaEl)  metaEl.innerHTML    = '';
    if (listEl)  listEl.innerHTML    = '<div style="color:var(--text3);font-size:12px">Loading comments…</div>';
    if (linkEl)  linkEl.href         = fullLink;
    // Escape any hidden parent by moving modal to body level
    if (modal.parentElement !== document.body) document.body.appendChild(modal);
    modal.style.display = 'flex';
    document.body.style.overflow = 'hidden';
    const safe = t => (t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    function renderComment(c, depth) {
        const d = c.data || {};
        if (!d.author || d.author === '[deleted]' && !d.body) return '';
        const indent = depth * 14;
        const body = (d.body || '').replace(/\n\n/g,'<br><br>').replace(/\n/g,'<br>');
        const replies = (d.replies?.data?.children||[]).filter(r=>r.kind==='t1');
        return `<div style="margin-left:${indent}px;border-left:${depth>0?'2px solid var(--border)':'none'};padding-left:${depth>0?'10px':'0'};margin-top:6px">
          <div style="background:var(--surface);border-radius:6px;padding:8px 10px">
            <div style="font-size:10px;color:var(--blue);font-weight:600;margin-bottom:4px">${safe(d.author||'?')} <span style="color:var(--text3);font-weight:400">· ▲ ${d.score||0}</span></div>
            <div style="font-size:12px;color:var(--text2);line-height:1.6">${body}</div>
          </div>
          ${replies.slice(0,5).map(r=>renderComment(r,depth+1)).join('')}
        </div>`;
    }
    try {
        // Direct browser fetch for comments — www.reddit.com JSON API
        const commentUrl = (fullLink.startsWith('http') ? fullLink : 'https://www.reddit.com'+fullLink).replace(/\/?$/, '.json') + '?limit=50&raw_json=1&include_over_18=1';
        let data;
        try {
            const r = await fetch(commentUrl);
            if (!r.ok) throw new Error('browser fetch failed');
            data = await r.json();
        } catch(e2) {
            // Fallback to server proxy
            const r2 = await fetch(API + `/api/reddit/comments?url=${encodeURIComponent(commentUrl)}`);
            data = await r2.json();
        }
        const post = data[0]?.data?.children?.[0]?.data || {};
        const comments = data[1]?.data?.children || [];
        if (bodyEl) bodyEl.textContent = post.selftext || (post.url && post.url !== fullLink ? post.url : '');
        if (metaEl) metaEl.innerHTML = `
          <span>▲ ${(post.score||0).toLocaleString()} points</span>
          <span>💬 ${(post.num_comments||0).toLocaleString()} comments</span>
          <span>📌 r/${safe(post.subreddit||'')}</span>
          <span>👤 u/${safe(post.author||'')}</span>`;
        if (listEl) {
            const rendered = comments.filter(c=>c.kind==='t1').slice(0,30).map(c=>renderComment(c,0)).join('');
            listEl.innerHTML = rendered || '<div style="color:var(--text3);font-size:12px;padding:8px">No comments yet</div>';
        }
    } catch(e) {
        if (listEl) listEl.innerHTML = `<div style="color:var(--red);font-size:12px">Failed to load comments: ${e.message}</div>`;
    }
}
function feedsCloseComments() {
    const modal = document.getElementById('feeds-reddit-comments-modal');
    if (modal) modal.style.display = 'none';
    document.body.style.overflow = '';
}

// ── Feed view / sort / pagination helpers ────────────────────────────
function feedsToggleView(gridId, btnId) {
    const grid = document.getElementById(gridId);
    const btn  = document.getElementById(btnId);
    if (!grid) return;
    const current = _feedsViewMode[gridId] || 'grid';
    const next = current === 'grid' ? 'list' : 'grid';
    _feedsViewMode[gridId] = next;
    if (btn) btn.textContent = next === 'grid' ? '☰' : '⊞';
    const items = _feedsAllItems[gridId];
    if (items && items.length) {
        const offset = _feedsOffset[gridId] || _FEEDS_PAGE_SIZE;
        _feedsRenderItems(grid, items, 0, offset, next, false);
    }
    // Apply list mode styles
    if (next === 'list') {
        grid.style.gridTemplateColumns = '1fr';
        grid.style.gap = '6px';
    } else {
        grid.style.gridTemplateColumns = '';
        grid.style.gap = '12px';
    }
}

function feedsSortGrid(gridId, sortVal) {
    const grid  = document.getElementById(gridId);
    if (!grid) return;
    let items = (_feedsAllItems[gridId] || []).slice();
    if (sortVal === 'oldest') items.reverse();
    else if (sortVal === 'az') items.sort((a,b) => (a.title||'').localeCompare(b.title||''));
    _feedsAllItems[gridId] = items;
    _feedsOffset[gridId] = _FEEDS_PAGE_SIZE;
    const mode = _feedsViewMode[gridId] || 'grid';
    _feedsRenderItems(grid, items, 0, _FEEDS_PAGE_SIZE, mode, false);
    // Update Load More button
    const moreId = gridId.endsWith('-grid') ? gridId.replace(/-grid$/, '-more-btn') : null;
    if (moreId) { const b = document.getElementById(moreId); if (b) b.style.display = items.length > _FEEDS_PAGE_SIZE ? '' : 'none'; }
}

function _feedsRenderItems(grid, items, from, to, mode, append) {
    const safe = t => (t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    const slice = items.slice(from, to);
    const html = slice.map(item => {
        const isYT = (item.link||'').includes('youtube.com') || (item.link||'').includes('youtu.be');
        const thumb = item.thumb;
        const ytId = isYT ? (()=>{ try { const u=new URL(item.link); return u.searchParams.get('v')||u.pathname.split('/').pop(); } catch(e){return null;} })() : null;
        const domainStr = (() => { try { return new URL(item.link||'').hostname.replace('www.',''); } catch(e){return '';} })();
        if (mode === 'list') {
            return `<a href="${item.link||'#'}" ${ytId?`data-yt-id="${ytId}" onclick="feedsOpenYT(this.dataset.ytId,this.dataset.ytTitle||'');return false;" data-yt-title="${safe(item.title)}"`:' target="_blank" rel="noopener"'} style="display:flex;align-items:center;gap:10px;text-decoration:none;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 12px;transition:border-color .15s" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor='var(--border)'">
              ${thumb ? `<img src="${thumb}" loading="lazy" style="width:56px;height:40px;object-fit:cover;border-radius:4px;flex-shrink:0" onerror="this.style.display='none'">` : `<div style="width:56px;height:40px;background:var(--bg3);border-radius:4px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:16px">${isYT?'▶':'📰'}</div>`}
              <div style="flex:1;min-width:0">
                <div style="font-size:12px;font-weight:600;color:var(--text);line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${safe(item.title)}</div>
                <div style="font-size:10px;color:var(--text3);margin-top:2px">${domainStr ? `${domainStr} · ` : ''}${item.date||''}</div>
              </div>
            </a>`;
        }
        // Card mode — delegate to the card HTML the item already produced (passed as raw)
        return item._html || `<a href="${item.link||'#'}" ${ytId?`data-yt-id="${ytId}" onclick="feedsOpenYT(this.dataset.ytId,this.dataset.ytTitle||'');return false;" data-yt-title="${safe(item.title)}"`:' target="_blank" rel="noopener"'} style="display:flex;flex-direction:column;text-decoration:none;background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;transition:border-color .15s" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor='var(--border)'">
          ${thumb ? `<div style="position:relative;width:100%;padding-top:52%;background:var(--surface2)"><img src="${thumb}" loading="lazy" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover" onerror="this.parentElement.innerHTML='<div style=\\'display:flex;align-items:center;justify-content:center;height:100%;font-size:36px\\'>${isYT?'▶':'📰'}</div>'"></div>` : `<div style="width:100%;padding-top:52%;background:var(--surface2);position:relative"><div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:36px">${isYT?'▶':'📰'}</div></div>`}
          <div style="padding:9px 12px 11px;flex:1;display:flex;flex-direction:column;gap:3px">
            <div style="font-size:12px;font-weight:600;color:var(--text);line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${safe(item.title)}</div>
            <div style="font-size:10px;color:var(--text3);margin-top:auto;padding-top:4px">${domainStr ? `${domainStr} · ` : ''}${item.date||''}</div>
          </div>
        </a>`;
    }).join('');
    if (append) grid.insertAdjacentHTML('beforeend', html);
    else grid.innerHTML = html;
    // Trigger og:image lazy loading for any new placeholders
    setTimeout(() => _feedsLazyLoadThumbs(grid), 120);
}

function feedsLoadMore(type) {
    const gridId = type === 'youtube' ? 'feeds-yt-grid' : `feeds-${type}-grid`;
    const moreBtnId = type === 'youtube' ? 'feeds-yt-more-btn' : `feeds-${type}-more-btn`;
    const grid = document.getElementById(gridId);
    const moreBtn = document.getElementById(moreBtnId);
    if (!grid) return;
    const items = _feedsAllItems[gridId] || [];
    const from = _feedsOffset[gridId] || _FEEDS_PAGE_SIZE;
    const to = from + _FEEDS_PAGE_SIZE;
    _feedsOffset[gridId] = to;
    const mode = _feedsViewMode[gridId] || 'grid';
    _feedsRenderItems(grid, items, from, to, mode, true);
    if (moreBtn) moreBtn.style.display = to >= items.length ? 'none' : '';
}

function feedsRedditChangeSort(val) {
    _feedsRedditSort = val;
    _feedsRedditAfter = null;
    const moreBtn = document.getElementById('feeds-reddit-more-btn');
    if (moreBtn) moreBtn.style.display = 'none';
    const grid = document.getElementById('feeds-reddit-grid');
    const src = (_feedsSubs.reddit||[]).find(s => s.id === _feedsRedditActive);
    if (src && grid) _feedsFetchRedditDirect(src.url, grid, false);
}

function feedsRedditLoadMore() {
    const grid = document.getElementById('feeds-reddit-grid');
    const src = (_feedsSubs.reddit||[]).find(s => s.id === _feedsRedditActive);
    if (src && grid) _feedsFetchRedditDirect(src.url, grid, true);
}

function feedsHNChangeSort(sort, btnEl) {
    _hnSort = sort;
    _hnAllItems = [];
    _hnOffset = 0;
    document.querySelectorAll('[id^="hn-sort-"]').forEach(b=>b.classList.remove('active'));
    if (btnEl) btnEl.classList.add('active');
    _feedsLoadHN(false);
}

let _twitterViewMode = 'cards';
function twitterSetMode(mode) {
    _twitterViewMode = mode;
    const cardsEl  = document.getElementById('feeds-twitter-cards-mode');
    const viewerEl = document.getElementById('feeds-twitter-viewer-mode');
    const cardBtn  = document.getElementById('tw-mode-cards-btn');
    const viewBtn  = document.getElementById('tw-mode-viewer-btn');
    if (cardsEl)  cardsEl.style.display  = mode === 'cards'  ? '' : 'none';
    if (viewerEl) viewerEl.style.display = mode === 'viewer' ? '' : 'none';
    if (cardBtn)  { cardBtn.style.background = mode==='cards'  ? 'var(--accent,#2563eb)' : 'var(--bg3)'; cardBtn.style.color = mode==='cards'  ? '#fff' : 'var(--text2)'; cardBtn.style.border = mode==='cards'  ? 'none' : '1px solid var(--border)'; }
    if (viewBtn)  { viewBtn.style.background  = mode==='viewer' ? 'var(--accent,#2563eb)' : 'var(--bg3)'; viewBtn.style.color  = mode==='viewer' ? '#fff' : 'var(--text2)'; viewBtn.style.border  = mode==='viewer' ? 'none' : '1px solid var(--border)'; }
    if (mode === 'viewer') {
        // Load the active handle in the web viewer iframe
        const activePill = document.querySelector('#feeds-twitter-handle-tabs .filter-pill.active');
        if (activePill) {
            const handle = (activePill.dataset.handle || activePill.textContent || '').replace(/^[\s\S]*?@?(\w+)\s*$/, '$1').replace(/^@/, '').trim();
            const iframe = document.getElementById('feeds-twitter-webviewer-iframe');
            const fallback = document.getElementById('feeds-twitter-webviewer-fallback');
            const extLink  = document.getElementById('feeds-twitter-webviewer-extlink');
            if (fallback) fallback.style.display = 'none';
            if (iframe && handle) {
                iframe.style.display = '';
                iframe.src = API + '/api/twitter/webviewer?handle=' + encodeURIComponent(handle);
            }
            if (extLink) extLink.href = `https://x.com/${handle}`;
        }
    }
}

function _feedsLoadTwitterPage() {
    const tabs    = document.getElementById('feeds-twitter-handle-tabs');
    const grid    = document.getElementById('feeds-twitter-grid');
    const emptyEl = document.getElementById('feeds-twitter-empty');
    const moreBtn = document.getElementById('feeds-twitter-more-btn');
    if (!tabs) return;
    const handles = _feedsSubs.twitter || [];
    if (!handles.length) {
        if (grid) grid.innerHTML = '';
        if (emptyEl) emptyEl.style.display = '';
        if (moreBtn) moreBtn.style.display = 'none';
        tabs.innerHTML = '';
        return;
    }
    if (emptyEl) emptyEl.style.display = 'none';
    if (!_feedsTwitterActive || !handles.find(h => h.id === _feedsTwitterActive))
        _feedsTwitterActive = handles[0].id;
    tabs.innerHTML = handles.map(h =>
        `<button class="filter-pill${h.id===_feedsTwitterActive?' active':''}" data-handle="${(h.url||'').replace(/^@/,'')}" onclick="_feedsSelectTwitter('${h.id}',this)">𝕏 ${h.name}</button>`
    ).join('');
    const active = handles.find(h => h.id === _feedsTwitterActive);
    if (active && grid) _feedsFetchTwitterCards(active.url, grid, false);
}

function _feedsSelectTwitter(id, el) {
    _feedsTwitterActive = id;
    document.querySelectorAll('#feeds-twitter-handle-tabs .filter-pill').forEach(p => p.classList.remove('active'));
    if (el) el.classList.add('active');
    const h = (_feedsSubs.twitter || []).find(s => s.id === id);
    const grid = document.getElementById('feeds-twitter-grid');
    const openLink = document.getElementById('feeds-twitter-open-link');
    if (openLink) openLink.href = `https://x.com/${(h && h.url ? h.url : '').replace(/^@/,'')}`;
    if (h && grid) _feedsFetchTwitterCards(h.url, grid, false);
    // Also update web viewer iframe if in viewer mode
    if (_twitterViewMode === 'viewer' && h) {
        const handle = (h.url || '').replace(/^@/, '');
        const iframe = document.getElementById('feeds-twitter-webviewer-iframe');
        const fallback = document.getElementById('feeds-twitter-webviewer-fallback');
        const extLink  = document.getElementById('feeds-twitter-webviewer-extlink');
        if (fallback) fallback.style.display = 'none';
        if (iframe && handle) {
            iframe.style.display = '';
            iframe.src = API + '/api/twitter/webviewer?handle=' + encodeURIComponent(handle);
        }
        if (extLink) extLink.href = `https://x.com/${handle}`;
    }
}

async function _feedsFetchTwitterCards(handle, grid, appendMode) {
    if (!grid) return;
    const moreBtn = document.getElementById('feeds-twitter-more-btn');
    const cleanHandle = (handle || '').replace(/^@/, '');
    if (!cleanHandle) return;
    if (!appendMode) {
        grid.innerHTML = '<div class="skeleton" style="height:200px;border-radius:var(--r)"></div>'.repeat(6);
        if (moreBtn) moreBtn.style.display = 'none';
    }
    try {
        const r = await fetch(API + `/api/twitter/feed?handle=${encodeURIComponent(cleanHandle)}&_t=${Date.now()}`);
        const d = await r.json();
        const items = d.items || [];
        if (!items.length) {
            const msg = d.error ? `Could not load tweets: ${d.error}` : 'No tweets found';
            grid.innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="empty-icon">𝕏</div><div class="empty-text" style="color:var(--text3)">${msg}<br><br><a href="https://x.com/${cleanHandle}" target="_blank" rel="noopener" style="color:var(--blue)">↗ Open @${cleanHandle} on X</a></div></div>`;
            return;
        }
        const safe = t => (t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        const gridId = grid.id;
        const enriched = items.map(item => {
            item._html = `
            <a href="${item.link||'#'}" target="_blank" rel="noopener"
              style="display:flex;flex-direction:column;text-decoration:none;background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;transition:border-color .15s,transform .1s;cursor:pointer"
              onmouseover="this.style.borderColor='#1d9bf0';this.style.transform='translateY(-2px)'"
              onmouseout="this.style.borderColor='var(--border)';this.style.transform=''">
              <div style="padding:14px;flex:1;display:flex;flex-direction:column;gap:6px">
                <div style="display:flex;align-items:center;gap:8px;font-size:11px;font-weight:700;color:#1d9bf0">
                  <span style="font-size:15px">𝕏</span> @${safe(cleanHandle)}
                </div>
                <div style="font-size:13px;color:var(--text);line-height:1.5;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden">${safe(item.title)}</div>
                ${item.excerpt && item.excerpt !== item.title ? `<div style="font-size:11px;color:var(--text2);line-height:1.4">${safe(item.excerpt.slice(0,120))}</div>` : ''}
                <div style="font-size:10px;color:var(--text3);margin-top:auto;padding-top:4px">${safe(item.date||'')}</div>
              </div>
            </a>`;
            return item;
        });
        _feedsAllItems[gridId] = enriched;
        _feedsOffset[gridId] = _FEEDS_PAGE_SIZE;
        _feedsRenderItems(grid, enriched, 0, _FEEDS_PAGE_SIZE, 'grid', false);
        if (moreBtn) moreBtn.style.display = enriched.length > _FEEDS_PAGE_SIZE ? '' : 'none';
    } catch(e) {
        grid.innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="empty-icon">𝕏</div><div class="empty-text" style="color:var(--red)">Failed: ${e.message}<br><br><a href="https://x.com/${cleanHandle}" target="_blank" rel="noopener" style="color:var(--blue)">↗ Open @${cleanHandle} on X</a></div></div>`;
    }
}

function feedsHNLoadMore() {
    _feedsLoadHN(true);
}

// ── Media player modal (YouTube embed) ───────────────────────────────
function feedsOpenYT(videoId, title) {
    const modal = document.getElementById('feeds-media-modal');
    const iframe = document.getElementById('feeds-media-iframe');
    const titleEl = document.getElementById('feeds-media-title');
    const extLink = document.getElementById('feeds-media-extlink');
    if (!modal || !iframe) return;
    iframe.src = `https://www.youtube.com/embed/${videoId}?autoplay=1&rel=0`;
    if (titleEl) titleEl.textContent = title || '';
    if (extLink) extLink.href = `https://www.youtube.com/watch?v=${videoId}`;
    modal.style.display = 'flex';
    document.body.style.overflow = 'hidden';
}
function feedsTwitterViewerLoaded(iframe) {
    // Check if the page content indicates an error (Next.js app error)
    try {
        const doc = iframe.contentDocument || iframe.contentWindow?.document;
        if (doc && doc.body && doc.body.innerText.includes('Application error')) {
            feedsTwitterViewerError(iframe);
        }
    } catch(e) {
        // Cross-origin: can't inspect — assume OK
    }
}
function feedsTwitterViewerError(iframe) {
    if (iframe) iframe.style.display = 'none';
    const fallback = document.getElementById('feeds-twitter-webviewer-fallback');
    if (fallback) { fallback.style.display = 'flex'; }
}
function feedsCloseMedia() {
    const modal = document.getElementById('feeds-media-modal');
    const iframe = document.getElementById('feeds-media-iframe');
    if (iframe) iframe.src = '';
    if (modal) modal.style.display = 'none';
    document.body.style.overflow = '';
}
// close modals on Escape key
document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
        feedsCloseMedia();
        feedsCloseRedditPost();
        iptvHideBrowse();
    }
});

// ── Manage sub-page ──────────────────────────────────────────────────
// ── Custom type page loader ───────────────────────────────────────────
function _feedsLoadCustomPage(type) {
    const tabs = document.getElementById(`feeds-${type}-source-tabs`);
    const grid = document.getElementById(`feeds-${type}-grid`);
    const moreBtn = document.getElementById(`feeds-${type}-more-btn`);
    if (!tabs || !grid) return;
    const meta = _feedsSubs._type_meta || {};
    const icon = meta[type]?.icon || '📡';
    const sources = _feedsSubs[type] || [];
    if (!sources.length) {
        tabs.innerHTML = '';
        grid.innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="empty-icon">${icon}</div><div class="empty-text">No feeds yet — add some in Manage</div></div>`;
        if (moreBtn) moreBtn.style.display = 'none';
        return;
    }
    let activeId = sources[0].id;
    tabs.innerHTML = sources.map(s =>
        `<button class="filter-pill${s.id===activeId?' active':''}" onclick="_feedsSelectCustom('${type}','${s.id}',this)">${s.name}</button>`
    ).join('');
    _feedsFetchAndRenderCards(sources[0].url, grid, 'rss');
}

function _feedsSelectCustom(type, id, el) {
    document.querySelectorAll(`#feeds-${type}-source-tabs .filter-pill`).forEach(p => p.classList.remove('active'));
    if (el) el.classList.add('active');
    const src = (_feedsSubs[type] || []).find(s => s.id === id);
    if (src) _feedsFetchAndRenderCards(src.url, document.getElementById(`feeds-${type}-grid`), 'rss');
}

// ── Manage sub-page ──────────────────────────────────────────────────
function _feedsRenderManage() {
    const grid = document.getElementById('feeds-manage-grid');
    if (!grid) return;
    const meta = _feedsSubs._type_meta || {};
    const builtIn = ['rss','reddit','youtube','twitter'];
    const allTypes = [...builtIn, ...Object.keys(meta).filter(k => !builtIn.includes(k))];
    const icons = {rss:'📰', reddit:'🤖', youtube:'▶', twitter:'𝕏'};
    const names = {rss:'RSS Feeds', reddit:'Reddit', youtube:'YouTube', twitter:'Twitter / X'};

    grid.innerHTML = allTypes.map(type => {
        const icon = icons[type] || meta[type]?.icon || '📡';
        const name = names[type] || meta[type]?.name || type;
        const list = _feedsSubs[type] || [];
        const isCustom = !builtIn.includes(type);
        return `
          <div class="panel">
            <div class="panel-title" style="display:flex;align-items:center;justify-content:space-between">
              <span>${icon} ${name}</span>
              <div style="display:flex;gap:4px">
                <button class="btn blue" style="padding:3px 8px;font-size:11px" onclick="feedsShowAddModal('${type}')">+ Add</button>
                ${isCustom ? `<button class="btn" style="padding:3px 8px;font-size:11px;color:var(--red)" onclick="_feedsDeleteCategory('${type}')" title="Delete category">🗑</button>` : ''}
              </div>
            </div>
            <div id="feeds-manage-${type}" style="display:flex;flex-direction:column;gap:6px;max-height:220px;overflow-y:auto">
              ${list.length ? list.map(s => `
                <div style="display:flex;align-items:center;gap:8px;padding:6px 8px;background:var(--surface);border:1px solid var(--border);border-radius:6px">
                  <div style="flex:1;min-width:0">
                    <div style="font-size:12px;font-weight:600;color:var(--text)">${s.name}</div>
                    <div style="font-size:10px;color:var(--text3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${s.url || s.id}</div>
                  </div>
                  <button onclick="_feedsDeleteSub('${type}','${s.id}')" style="background:none;border:none;color:var(--red);cursor:pointer;font-size:14px;padding:2px 4px;flex-shrink:0" title="Remove">✕</button>
                </div>`).join('') : `<div style="font-size:12px;color:var(--text3);padding:6px">No sources yet</div>`}
            </div>
          </div>`;
    }).join('');
}

async function _feedsDeleteSub(type, id) {
    if (!confirm('Remove this subscription?')) return;
    try {
        await fetch(API + `/api/feeds/subscriptions/${type}/${encodeURIComponent(id)}`, {method:'DELETE'});
        const subs = _feedsSubs[type] || [];
        _feedsSubs[type] = subs.filter(s => s.id !== id);
        _feedsRenderManage();
        showToast('Subscription removed', 'success', 2000);
    } catch(e) { showToast('Failed to remove', 'error'); }
}

// ── Add modal ────────────────────────────────────────────────────────
function feedsShowAddModal(type) {
    _feedsAddType = type;
    const modal = document.getElementById('feeds-add-modal');
    const title = document.getElementById('feeds-add-modal-title');
    const urlRow = document.getElementById('feeds-add-url-row');
    const idRow  = document.getElementById('feeds-add-id-row');
    const urlLbl = document.getElementById('feeds-add-url-label');
    const idLbl  = document.getElementById('feeds-add-id-label');
    if (!modal) return;
    document.getElementById('feeds-add-name').value = '';
    document.getElementById('feeds-add-url').value  = '';
    document.getElementById('feeds-add-id').value   = '';
    const meta = _feedsSubs._type_meta || {};
    if (type === 'rss') {
        title.textContent = '📰 Add RSS Feed';
        urlRow.style.display = '';
        urlLbl.textContent = 'RSS Feed URL';
        idRow.style.display = 'none';
        document.getElementById('feeds-add-url').placeholder = 'https://techcrunch.com/feed/';
    } else if (type === 'reddit') {
        title.textContent = '🤖 Add Subreddit';
        urlRow.style.display = 'none';
        idRow.style.display = '';
        idLbl.textContent = 'Subreddit (e.g. homelab or selfhosted)';
        document.getElementById('feeds-add-id').placeholder = 'homelab';
    } else if (type === 'youtube') {
        title.textContent = '▶ Add YouTube Channel';
        urlRow.style.display = '';
        urlLbl.textContent = 'Channel ID (from youtube.com/channel/UC...)';
        idRow.style.display = 'none';
        document.getElementById('feeds-add-url').placeholder = 'UCsBjURrPoezykLs9EqgamOA';
    } else {
        // Custom category — always RSS-style URL
        const icon = meta[type]?.icon || '📡';
        const name = meta[type]?.name || type;
        title.textContent = `${icon} Add Feed to ${name}`;
        urlRow.style.display = '';
        urlLbl.textContent = 'RSS / Atom Feed URL';
        idRow.style.display = 'none';
        document.getElementById('feeds-add-url').placeholder = 'https://example.com/feed.xml';
    }
    modal.style.display = 'flex';
}

// ── New Category modal ────────────────────────────────────────────────
function feedsShowNewCatModal() {
    const m = document.getElementById('feeds-newcat-modal');
    if (m) { document.getElementById('newcat-name').value=''; document.getElementById('newcat-icon').value=''; m.style.display='flex'; }
}
function feedsHideNewCatModal() {
    const m = document.getElementById('feeds-newcat-modal');
    if (m) m.style.display='none';
}
async function feedsSaveNewCat() {
    const name = document.getElementById('newcat-name').value.trim();
    const icon = document.getElementById('newcat-icon').value.trim() || '📡';
    if (!name) { showToast('Category name is required','error',2000); return; }
    const id = name.toLowerCase().replace(/[^a-z0-9_]/g,'_').replace(/_+/g,'_');
    try {
        const r = await fetch(API+'/api/feeds/categories', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,name,icon})});
        const d = await r.json();
        if (d.error) { showToast('Error: '+d.error,'error'); return; }
        feedsHideNewCatModal();
        // Reload subs and rebuild nav
        const sr = await fetch(API+'/api/feeds/subscriptions');
        _feedsSubs = await sr.json();
        if (!_feedsSubs._type_meta) _feedsSubs._type_meta = {};
        _feedsBuildNavPills();
        _feedsInjectCustomPageDivs();
        _feedsRenderManage();
        showToast(`Category "${name}" created`,'success');
    } catch(e) { showToast('Failed to create category','error'); }
}

async function _feedsDeleteCategory(type) {
    if (!confirm(`Delete the "${type}" category and all its subscriptions?`)) return;
    try {
        await fetch(API+`/api/feeds/categories/${encodeURIComponent(type)}`, {method:'DELETE'});
        delete _feedsSubs[type];
        if (_feedsSubs._type_meta) delete _feedsSubs._type_meta[type];
        _feedsBuildNavPills();
        _feedsInjectCustomPageDivs();
        _feedsRenderManage();
        if (_feedsPage === type) feedsNav('rss', document.getElementById('feeds-pill-rss'));
        showToast(`Category removed`,'success');
    } catch(e) { showToast('Failed to delete category','error'); }
}

function feedsHideAddModal() {
    const modal = document.getElementById('feeds-add-modal');
    if (modal) modal.style.display = 'none';
}

async function feedsSaveAdd() {
    const type = _feedsAddType;
    const name = document.getElementById('feeds-add-name').value.trim();
    const urlVal = document.getElementById('feeds-add-url').value.trim();
    const idVal  = document.getElementById('feeds-add-id').value.trim();
    if (!name) { showToast('Name is required', 'error', 2000); return; }

    let id, url;
    if (type === 'rss') {
        url = urlVal;
        id  = name.toLowerCase().replace(/[^a-z0-9]/g, '-');
        if (!url) { showToast('URL is required', 'error', 2000); return; }
    } else if (type === 'reddit') {
        const sub = idVal.replace(/^r\//,'').replace(/\s/g,'');
        if (!sub) { showToast('Subreddit name required', 'error', 2000); return; }
        id  = sub.toLowerCase();
        url = `https://old.reddit.com/r/${sub}/.rss`;
    } else if (type === 'youtube') {
        const chId = urlVal.replace(/\s/g,'');
        if (!chId) { showToast('Channel ID required', 'error', 2000); return; }
        id  = chId;
        url = `https://www.youtube.com/feeds/videos.xml?channel_id=${chId}`;
    } else {
        // Custom category: generic RSS URL
        url = urlVal;
        id  = urlVal.replace(/[^a-z0-9]/gi, '_').slice(0,32) || ('feed_' + Date.now());
    }

    try {
        const r = await fetch(API + '/api/feeds/subscriptions', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({type, id, name, url})
        });
        const d = await r.json();
        if (d.error) { showToast(d.error, 'error'); return; }
        if (!_feedsSubs[type]) _feedsSubs[type] = [];
        if (!_feedsSubs[type].find(s => s.id === id))
            _feedsSubs[type].push({id, name, url});
        feedsHideAddModal();
        _feedsRenderManage();
        showToast(`${name} added!`, 'success', 2000);
    } catch(e) { showToast('Save failed', 'error'); }
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

        // Reddit gets bigger cards; everything else gets compact row layout
        const isReddit = catId === 'Reddit' || url.includes('reddit.com');
        if (isReddit) {
            // Big Reddit-style cards with resizable layout
            const cardSize = parseInt(itemsEl.closest('.rss-col')?.dataset.cardSize || '1');
            itemsEl.innerHTML = `
              <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
                <span style="font-size:11px;color:var(--text3)">Card size:</span>
                <input type="range" min="0" max="2" value="${cardSize}" style="width:80px;accent-color:var(--blue)"
                  oninput="rssSetCardSize('${catId}',+this.value)">
              </div>
              <div id="reddit-cards-${catId}" style="display:grid;gap:12px;${['grid-template-columns:1fr','grid-template-columns:repeat(2,1fr)','grid-template-columns:repeat(3,1fr)'][cardSize]}">
              ${items.slice(0,20).map(item => `
                <a href="${item.link}" target="_blank" rel="noopener"
                   style="display:flex;flex-direction:column;gap:8px;text-decoration:none;background:var(--surface);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;transition:border-color .15s"
                   onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor='var(--border)'">
                  ${item.thumb
                    ? `<img src="${item.thumb}" loading="lazy" style="width:100%;height:140px;object-fit:cover;background:var(--surface2)" onerror="this.style.display='none'">`
                    : `<div style="width:100%;height:80px;background:var(--surface2);display:flex;align-items:center;justify-content:center;font-size:32px">🤖</div>`}
                  <div style="padding:10px 12px 12px;flex:1;display:flex;flex-direction:column;gap:4px">
                    <div style="font-size:13px;font-weight:600;color:var(--text);line-height:1.4;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden">${item.title}</div>
                    ${item.excerpt ? `<div style="font-size:11px;color:var(--text2);line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin-top:2px">${item.excerpt}</div>` : ''}
                    <div style="font-size:10px;color:var(--text3);margin-top:auto;padding-top:6px">${item.date || ''}</div>
                  </div>
                </a>`).join('')}
              </div>`;
        } else {
        // Rich card layout for non-Reddit feeds — bigger tiles
        itemsEl.innerHTML = `<div style="display:grid;gap:8px">` + items.slice(0, 15).map(item => `
            <a href="${item.link}" target="_blank" rel="noopener"
               style="display:flex;gap:12px;align-items:flex-start;text-decoration:none;
                      background:var(--surface);border:1px solid var(--border);border-radius:8px;
                      padding:10px 12px;transition:border-color .15s,transform .1s;"
               onmouseover="this.style.borderColor='var(--blue)';this.style.transform='translateY(-1px)'"
               onmouseout="this.style.borderColor='var(--border)';this.style.transform=''">
              ${item.thumb
                ? `<img src="${item.thumb}" loading="lazy"
                        style="width:88px;height:66px;object-fit:cover;border-radius:6px;flex-shrink:0;background:var(--surface2)"
                        onerror="this.outerHTML='<div style=\\'width:88px;height:66px;flex-shrink:0;background:var(--surface2);border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:26px\\'>📰</div>'">`
                : `<div style="width:88px;height:66px;flex-shrink:0;background:var(--surface2);border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:26px;color:var(--text3)">📰</div>`}
              <div style="flex:1;min-width:0">
                <div style="font-size:13px;font-weight:600;color:var(--text);line-height:1.4;margin-bottom:4px;
                     display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${item.title}</div>
                ${item.excerpt
                    ? `<div style="font-size:11px;color:var(--text2);line-height:1.45;margin-bottom:5px;
                            display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${item.excerpt}</div>`
                    : ''}
                <div style="font-size:10px;color:var(--text3);display:flex;align-items:center;gap:4px">
                  <svg width="10" height="10" fill="none" stroke="currentColor" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" stroke-width="2"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6l4 2"/></svg>
                  ${item.date || ''}
                </div>
              </div>
            </a>`).join('') + `</div>`;
        }

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

// Reddit card-size slider (0=1col, 1=2col, 2=3col)
function rssSetCardSize(catId, size) {
    const col = document.getElementById('rss-col-' + catId);
    if (col) col.dataset.cardSize = size;
    const grid = document.getElementById('reddit-cards-' + catId);
    if (!grid) return;
    const cols = ['1fr','repeat(2,1fr)','repeat(3,1fr)'][size] || '1fr';
    grid.style.gridTemplateColumns = cols;
}

// ── Add Feed Modal ─────────────────────────────────────────────
let _addFeedType = 'rss';

function openAddFeedModal() {
  const modal = document.getElementById('add-feed-modal');
  if (modal) { modal.style.display = 'flex'; }
}
function closeAddFeedModal() {
  const modal = document.getElementById('add-feed-modal');
  if (modal) modal.style.display = 'none';
  // Clear inputs
  ['add-feed-name-rss','add-feed-url-rss','add-feed-sub'].forEach(id => {
    const el = document.getElementById(id); if (el) el.value = '';
  });
  const st = document.getElementById('add-feed-status');
  if (st) st.textContent = '';
}
function setAddFeedType(t) {
  _addFeedType = t;
  document.getElementById('add-feed-form-rss').style.display = t === 'rss' ? '' : 'none';
  document.getElementById('add-feed-form-reddit').style.display = t === 'reddit' ? '' : 'none';
  document.getElementById('add-feed-type-rss').classList.toggle('active', t === 'rss');
  document.getElementById('add-feed-type-reddit').classList.toggle('active', t === 'reddit');
}
async function submitAddFeed() {
  const status = document.getElementById('add-feed-status');
  let payload;
  if (_addFeedType === 'rss') {
    const name = (document.getElementById('add-feed-name-rss')?.value || '').trim();
    const url  = (document.getElementById('add-feed-url-rss')?.value || '').trim();
    if (!name || !url) { if (status) status.textContent = 'Name and URL are required.'; return; }
    payload = { name, url, icon: '📰', type: 'rss' };
  } else {
    const sub  = (document.getElementById('add-feed-sub')?.value || '').trim().replace(/^r\//, '');
    if (!sub) { if (status) status.textContent = 'Subreddit name is required.'; return; }
    payload = { name: 'r/' + sub, url: `https://www.reddit.com/r/${sub}/.rss`, icon: '🤖', type: 'reddit' };
  }
  if (status) status.textContent = 'Saving…';
  try {
    const r = await fetch(API + '/api/rss/custom', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    const d = await r.json();
    if (d.error) { if (status) status.textContent = 'Error: ' + d.error; return; }
    if (status) status.textContent = '✓ Feed added!';
    setTimeout(() => { closeAddFeedModal(); loadRSSFeeds(); }, 800);
  } catch(e) {
    if (status) status.textContent = 'Failed to save feed.';
  }
}

// ══════════════════════════════════════════════════════════════════════
// 10a. FOOTBALL HUB (multi-league)
// ══════════════════════════════════════════════════════════════════════
// Utility: show loading indicator without wiping existing content (no flicker)
function _fbSetLoading(el, msg) {
    if (!el) return;
    const isEmpty = !el.innerHTML.trim() || !!el.querySelector('.fb-loading-placeholder');
    if (isEmpty) {
        el.innerHTML = '<div class="fb-loading-placeholder" style="color:var(--text3);font-size:12px;padding:20px;text-align:center">'+msg+'</div>';
    } else {
        // Keep old content visible, just dim it
        el.style.opacity = '0.45';
        el.style.pointerEvents = 'none';
        el.style.transition = 'opacity .15s';
    }
}
function _fbClearLoading(el) {
    if (!el) return;
    el.style.opacity = '';
    el.style.pointerEvents = '';
    el.style.transition = '';
}
const FOOTBALL_LEAGUES = {
    'eng.1':            { name:'Premier League',    flag:'🏴󠁧󠁢󠁥󠁮󠁧󠁿', highlights:['premier league','english premier','epl'] },
    'esp.1':            { name:'La Liga',            flag:'🇪🇸', highlights:['la liga','laliga','spanish primera'] },
    'ger.1':            { name:'Bundesliga',         flag:'🇩🇪', highlights:['bundesliga','german bundesliga'] },
    'ita.1':            { name:'Serie A',            flag:'🇮🇹', highlights:['serie a','italian serie'] },
    'fra.1':            { name:'Ligue 1',            flag:'🇫🇷', highlights:['ligue 1','french ligue'] },
    'usa.1':            { name:'MLS',                flag:'🇺🇸', highlights:['mls','major league soccer'] },
    'uefa.champions':   { name:'Champions League',  flag:'🏆', highlights:['champions league','ucl'] },
    'uefa.europa':      { name:'Europa League',      flag:'🥈', highlights:['europa league','uel'] },
    'fifa.world':       { name:'World Cup',          flag:'🌍', highlights:['world cup','fifa world'] },
    'uefa.euro':        { name:'Euros',              flag:'🇪🇺', highlights:['euro 2024','euros','european championship'] },
    'conmebol.america': { name:'Copa America',       flag:'🌎', highlights:['copa america'] },
    'caf.nations':      { name:'AFCON',              flag:'🌍', highlights:['africa cup','afcon'] },
};
let _footballInited = false;
let _footballCurrentView = 'table';
let _footballLeague = 'eng.1';
let _footballTeamId = null;
let _footballTeamLeague = null;
let _footballTeamTabActive = 'fixtures';

function footballInit() {
    if (!_footballInited) {
        _footballInited = true;
        footballLoadStandings();
    }
}

function footballSelectLeague(code) {
    if (!FOOTBALL_LEAGUES[code]) return;
    _footballLeague = code;
    // Update pill buttons
    document.querySelectorAll('.league-pill').forEach(b => {
        const oc = b.getAttribute('onclick') || '';
        b.classList.toggle('active', oc.includes("'"+code+"'"));
    });
    // Update league title bar
    const info = FOOTBALL_LEAGUES[code];
    const titleEl = document.getElementById('football-league-title');
    if (titleEl) titleEl.textContent = info.flag + ' ' + info.name;
    // Reload current view
    _footballDispatch(_footballCurrentView);
}

function footballSetView(view) {
    _footballCurrentView = view;
    ['table','fixtures','results','highlights','news'].forEach(v => {
        const el = document.getElementById('football-'+v+'-view');
        if (el) el.style.display = v === view ? '' : 'none';
        const btn = document.getElementById('football-view-'+v);
        if (btn) btn.classList.toggle('active', v === view);
    });
    _footballDispatch(view);
}

function _footballDispatch(view) {
    if (view === 'table')      footballLoadStandings();
    else if (view === 'fixtures')   footballLoadFixtures();
    else if (view === 'results')    footballLoadResults();
    else if (view === 'highlights') footballLoadHighlights();
    else if (view === 'news')       footballLoadNews();
}

function footballRefresh() {
    _footballInited = false;
    footballSetView(_footballCurrentView);
}

async function footballLoadStandings() {
    const el = document.getElementById('football-standings');
    if (!el) return;
    const league = _footballLeague;
    const info = FOOTBALL_LEAGUES[league] || {};
    _fbSetLoading(el, 'Loading standings…');
    try {
        const r = await fetch('https://site.api.espn.com/apis/v2/sports/soccer/'+league+'/standings');
        const raw = await r.json();
        let entries = [];
        for (const child of (raw.children || [])) entries.push(...(child.standings?.entries || []));
        if (!entries.length) entries = raw.standings?.entries || [];
        if (!entries.length) {
            el.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">No standings for '+info.name+' right now.<br><span style="font-size:11px;color:var(--text3)">Competition may be between seasons or in knockout stage.</span></div>';
            return;
        }
        const rows = entries.map(e => {
            const team = e.team || {};
            const s = {};
            (e.stats || []).forEach(st => s[st.name] = st.value != null ? st.value : st.displayValue);
            return {
                id: team.id || '',
                pos: parseInt(s.rank || 0), team: team.shortDisplayName || team.displayName || '?',
                crest: (team.logos || [{}])[0]?.href || '',
                played: parseInt(s.gamesPlayed || 0),
                won: parseInt(s.wins || 0), drawn: parseInt(s.ties || 0), lost: parseInt(s.losses || 0),
                gf: parseInt(s.pointsFor || 0), ga: parseInt(s.pointsAgainst || 0),
                gd: parseInt(s.pointDifferential || 0), pts: parseInt(s.points || 0)
            };
        }).sort((a,b) => a.pos - b.pos);
        const relegZone = rows.length - 3;
        let html = '<table style="width:100%;border-collapse:collapse;font-size:12px">';
        html += '<thead><tr style="border-bottom:2px solid var(--border);color:var(--text2);text-align:left">';
        html += '<th style="padding:8px 6px;width:30px">#</th>';
        html += '<th style="padding:8px 6px">Team</th>';
        html += '<th style="padding:8px 6px;text-align:center;width:36px">P</th>';
        html += '<th style="padding:8px 6px;text-align:center;width:36px">W</th>';
        html += '<th style="padding:8px 6px;text-align:center;width:36px">D</th>';
        html += '<th style="padding:8px 6px;text-align:center;width:36px">L</th>';
        html += '<th style="padding:8px 6px;text-align:center;width:44px">GF</th>';
        html += '<th style="padding:8px 6px;text-align:center;width:44px">GA</th>';
        html += '<th style="padding:8px 6px;text-align:center;width:44px">GD</th>';
        html += '<th style="padding:8px 6px;text-align:center;width:44px;font-weight:700">Pts</th>';
        html += '</tr></thead><tbody>';
        rows.forEach((t, i) => {
            const bg = i < 4 ? 'rgba(56,142,60,.08)' : i >= relegZone ? 'rgba(211,47,47,.08)' : 'transparent';
            const border = i < 4 ? '3px solid #388e3c' : i >= relegZone ? '3px solid #d32f2f' : '3px solid transparent';
            const safeTeam = t.team.replace(/"/g,'&quot;').replace(/'/g,'&#39;');
            const safeCrest = t.crest.replace(/"/g,'&quot;');
            html += '<tr style="border-bottom:1px solid var(--border);background:'+bg+'">';
            html += '<td style="padding:8px 6px;border-left:'+border+';font-weight:600">'+t.pos+'</td>';
            html += '<td style="padding:6px 6px">';
            if (t.id) {
                html += '<div style="display:flex;align-items:center;gap:8px;cursor:pointer;padding:2px 0" onclick="footballShowTeam(\''+t.id+'\',\''+safeTeam+'\',\''+safeCrest+'\')" title="View '+safeTeam+' fixtures &amp; news">';
            } else {
                html += '<div style="display:flex;align-items:center;gap:8px">';
            }
            if (t.crest) html += '<img src="'+t.crest+'" style="width:20px;height:20px;object-fit:contain" onerror="this.style.display=\'none\'">';
            html += '<span style="font-weight:500">'+t.team+'</span>';
            if (t.id) html += '<svg style="width:10px;height:10px;opacity:.35;flex-shrink:0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>';
            html += '</div></td>';
            html += '<td style="padding:8px 6px;text-align:center">'+t.played+'</td>';
            html += '<td style="padding:8px 6px;text-align:center">'+t.won+'</td>';
            html += '<td style="padding:8px 6px;text-align:center">'+t.drawn+'</td>';
            html += '<td style="padding:8px 6px;text-align:center">'+t.lost+'</td>';
            html += '<td style="padding:8px 6px;text-align:center">'+t.gf+'</td>';
            html += '<td style="padding:8px 6px;text-align:center">'+t.ga+'</td>';
            const gdColor = t.gd > 0 ? '#4caf50' : t.gd < 0 ? '#f44336' : 'var(--text2)';
            html += '<td style="padding:8px 6px;text-align:center;color:'+gdColor+'">'+(t.gd > 0 ? '+' : '')+t.gd+'</td>';
            html += '<td style="padding:8px 6px;text-align:center;font-weight:700;font-size:13px">'+t.pts+'</td>';
            html += '</tr>';
        });
        html += '</tbody></table>';
        html += '<div style="display:flex;flex-wrap:wrap;gap:12px;margin-top:10px;font-size:10px;color:var(--text3)">';
        html += '<span style="display:flex;align-items:center;gap:4px"><span style="width:10px;height:10px;background:#388e3c;border-radius:2px;display:inline-block"></span> Top positions</span>';
        html += '<span style="display:flex;align-items:center;gap:4px"><span style="width:10px;height:10px;background:#d32f2f;border-radius:2px;display:inline-block"></span> Relegation zone</span>';
        html += '<span style="display:flex;align-items:center;gap:4px"><svg style="width:10px;opacity:.4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg> Click team for fixtures &amp; news</span>';
        html += '</div>';
        _fbClearLoading(el); el.innerHTML = html;
    } catch(e) {
        _fbClearLoading(el); el.innerHTML = '<div style="color:#f66;font-size:12px;padding:20px;text-align:center">Failed to load standings: '+e.message+'</div>';
    }
}

async function _footballFetchMatches(league, type) {
    // Use server-side proxy which does a 60-day lookahead for upcoming / 21-day for results
    const r = await fetch(API + '/api/epl/matches?type=' + encodeURIComponent(type) + '&league=' + encodeURIComponent(league));
    const raw = await r.json();
    return raw.matches || [];
}

function _footballMatchRow(m) {
    const dt = m.date ? new Date(m.date) : null;
    const dateStr = dt ? dt.toLocaleDateString('en-GB',{weekday:'short',day:'numeric',month:'short'})+' · '+dt.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'}) : '';
    const isFinal = m.status === 'FINISHED';
    const score = m.scoreH != null ? m.scoreH+' - '+m.scoreA : 'vs';
    const homeW = isFinal && m.scoreH > m.scoreA, awayW = isFinal && m.scoreA > m.scoreH;
    const centerEl = m.status === 'IN_PLAY'
        ? '<span style="background:#f44336;color:#fff;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:600">LIVE</span>'
        : isFinal
            ? '<div style="font-weight:700;font-size:15px;letter-spacing:1px">'+score+'</div><div style="font-size:9px;color:var(--text3)">'+dateStr+'</div>'
            : '<span style="font-size:10px;color:var(--text3)">'+dateStr+'</span>';
    let row = '<div style="display:flex;align-items:center;padding:10px 12px;background:var(--bg2);border-radius:var(--r);border:1px solid var(--border);gap:10px">';
    row += '<div style="flex:1;display:flex;align-items:center;justify-content:flex-end;gap:6px;text-align:right">';
    row += '<span style="font-weight:'+(homeW?'700':'500')+';font-size:13px;color:'+(homeW?'var(--text)':'var(--text2)')+'">'+m.home+'</span>';
    if (m.homeCrest) row += '<img src="'+m.homeCrest+'" style="width:22px;height:22px;object-fit:contain" onerror="this.style.display=\'none\'">';
    row += '</div><div style="min-width:88px;text-align:center">'+centerEl+'</div>';
    row += '<div style="flex:1;display:flex;align-items:center;gap:6px">';
    if (m.awayCrest) row += '<img src="'+m.awayCrest+'" style="width:22px;height:22px;object-fit:contain" onerror="this.style.display=\'none\'">';
    row += '<span style="font-weight:'+(awayW?'700':'500')+';font-size:13px;color:'+(awayW?'var(--text)':'var(--text2)')+'">'+m.away+'</span>';
    row += '</div></div>';
    return row;
}

async function footballLoadFixtures() {
    const el = document.getElementById('football-fixtures-list');
    if (!el) return;
    _fbSetLoading(el, 'Loading fixtures…');
    try {
        const matches = await _footballFetchMatches(_footballLeague, 'upcoming');
        if (!matches.length) { el.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">No upcoming fixtures found.</div>'; return; }
        const grouped = {};
        matches.forEach(m => {
            const dt = m.date ? new Date(m.date) : null;
            const key = dt ? dt.toLocaleDateString('en-GB',{weekday:'long',day:'numeric',month:'short'}) : 'Upcoming';
            if (!grouped[key]) grouped[key] = [];
            grouped[key].push(m);
        });
        let html = '';
        Object.entries(grouped).forEach(([label, list]) => {
            html += '<div style="font-size:12px;font-weight:600;color:var(--text2);margin:12px 0 6px;padding-bottom:4px;border-bottom:1px solid var(--border)">'+label+'</div>';
            list.forEach(m => { html += _footballMatchRow(m); });
        });
        _fbClearLoading(el); el.innerHTML = html;
    } catch(e) {
        _fbClearLoading(el); el.innerHTML = '<div style="color:#f66;font-size:12px;padding:20px;text-align:center">Failed to load fixtures: '+e.message+'</div>';
    }
}

async function footballLoadResults() {
    const el = document.getElementById('football-results-list');
    if (!el) return;
    _fbSetLoading(el, 'Loading results…');
    try {
        const matches = (await _footballFetchMatches(_footballLeague, 'results')).reverse();
        if (!matches.length) { el.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">No recent results.</div>'; return; }
        let html = '';
        matches.forEach(m => { html += _footballMatchRow(m); });
        _fbClearLoading(el); el.innerHTML = html;
    } catch(e) {
        _fbClearLoading(el); el.innerHTML = '<div style="color:#f66;font-size:12px;padding:20px;text-align:center">Failed to load results: '+e.message+'</div>';
    }
}

async function footballLoadHighlights() {
    const el = document.getElementById('football-highlights-grid');
    if (!el) return;
    _fbSetLoading(el, 'Loading highlights…');
    const info = FOOTBALL_LEAGUES[_footballLeague] || {};
    const keys = info.highlights || [];
    try {
        let highlights = [];
        try {
            const r = await fetch('https://www.scorebat.com/video-api/v3/feed/?token=free');
            const allVids = await r.json();
            const vids = Array.isArray(allVids) ? allVids : (allVids.response || []);
            for (const v of vids) {
                const comp = (v.competition || v.competitionName || '').toLowerCase();
                if (!keys.some(k => comp.includes(k))) continue;
                let embed = '';
                for (const ev of (v.videos || [])) { if (ev.embed) { embed = ev.embed; break; } }
                highlights.push({ title: v.title||'', thumb: v.thumbnail||'', embed, date: v.date||'' });
                if (highlights.length >= 20) break;
            }
        } catch(e2) {
            const r2 = await fetch('/api/epl/highlights');
            const d2 = await r2.json();
            highlights = (d2.highlights || []).filter(h => {
                const comp = (h.competition || '').toLowerCase();
                return keys.some(k => comp.includes(k));
            });
        }
        if (!highlights.length) { el.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:20px;text-align:center;grid-column:1/-1">No highlights for '+info.name+' right now.</div>'; return; }
        let html = '';
        highlights.forEach(h => {
            const dt = h.date ? new Date(h.date) : null;
            const dateStr = dt ? dt.toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric'}) : '';
            html += '<div style="background:var(--bg2);border-radius:var(--r);border:1px solid var(--border);overflow:hidden;cursor:pointer" onclick="footballPlayHighlight(this)">';
            if (h.thumb) {
                html += '<div style="position:relative;padding-top:56.25%;background:#111">';
                html += '<img src="'+h.thumb+'" style="position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover" onerror="this.style.display=&quot;none&quot;">';
                html += '<div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:44px;height:44px;background:rgba(0,0,0,.7);border-radius:50%;display:flex;align-items:center;justify-content:center">';
                html += '<svg width="18" height="18" fill="#fff" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></div></div>';
            } else {
                html += '<div style="position:relative;padding-top:56.25%;background:#111"><div style="display:flex;align-items:center;justify-content:center;position:absolute;top:0;left:0;width:100%;height:100%;font-size:40px">⚽</div></div>';
            }
            html += '<div style="padding:10px 12px"><div style="font-weight:600;font-size:12px;line-height:1.3;margin-bottom:4px">'+h.title+'</div><div style="font-size:10px;color:var(--text3)">'+dateStr+'</div></div>';
            if (h.embed) html += '<div class="football-embed-data" style="display:none">'+h.embed.replace(/</g,'\\x3c').replace(/>/g,'\\x3e')+'</div>';
            html += '</div>';
        });
        _fbClearLoading(el); el.innerHTML = html;
    } catch(e) {
        _fbClearLoading(el); el.innerHTML = '<div style="color:#f66;font-size:12px;padding:20px;text-align:center;grid-column:1/-1">Failed to load highlights: '+e.message+'</div>';
    }
}

async function footballLoadNews() {
    const el = document.getElementById('football-news-list');
    if (!el) return;
    const info = FOOTBALL_LEAGUES[_footballLeague] || {};
    _fbSetLoading(el, 'Loading news…');
    try {
        const r = await fetch('https://site.api.espn.com/apis/site/v2/sports/soccer/'+_footballLeague+'/news?limit=20');
        const raw = await r.json();
        const articles = raw.articles || [];
        if (!articles.length) { el.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">No news for '+info.name+' right now.</div>'; return; }
        let html = '';
        articles.forEach(a => {
            const dt = a.published ? new Date(a.published) : null;
            const dateStr = dt ? dt.toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric'})+' · '+dt.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'}) : '';
            const imgUrl = (a.images || [])[0]?.url || '';
            const link = a.links?.web?.href || a.links?.mobile?.href || '';
            html += '<div style="display:flex;gap:12px;padding:12px;background:var(--bg2);border-radius:var(--r);border:1px solid var(--border);cursor:pointer" onclick="if(\''+link+'\')window.open(\''+link+'\',\'_blank\')">';
            if (imgUrl) html += '<img src="'+imgUrl+'" style="width:88px;height:62px;object-fit:cover;border-radius:6px;flex-shrink:0" onerror="this.style.display=\'none\'">';
            html += '<div style="flex:1;min-width:0"><div style="font-weight:600;font-size:12px;line-height:1.4;margin-bottom:4px">'+a.headline+'</div>';
            if (a.description) html += '<div style="font-size:11px;color:var(--text2);line-height:1.4;margin-bottom:4px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical">'+a.description+'</div>';
            html += '<div style="font-size:10px;color:var(--text3)">'+dateStr+'</div></div></div>';
        });
        _fbClearLoading(el); el.innerHTML = html;
    } catch(e) {
        _fbClearLoading(el); el.innerHTML = '<div style="color:#f66;font-size:12px;padding:20px;text-align:center">Failed to load news: '+e.message+'</div>';
    }
}

function footballPlayHighlight(card) {
    const embedEl = card.querySelector('.football-embed-data');
    if (!embedEl) return;
    const embedHtml = embedEl.textContent.replace(/\\x3c/g,'<').replace(/\\x3e/g,'>');
    const srcMatch = embedHtml.match(/src=["']([^"']+)["']/);
    if (srcMatch) {
        const overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:950;display:flex;align-items:center;justify-content:center;cursor:pointer';
        overlay.onclick = () => overlay.remove();
        overlay.innerHTML = '<div style="width:90%;max-width:900px;aspect-ratio:16/9;position:relative" onclick="event.stopPropagation()">'
            +'<iframe src="'+srcMatch[1]+'" style="width:100%;height:100%;border:none;border-radius:8px" allow="autoplay;fullscreen;encrypted-media" allowfullscreen></iframe>'
            +'<button onclick="this.parentNode.parentNode.remove()" style="position:absolute;top:-36px;right:0;background:none;border:none;color:#fff;font-size:24px;cursor:pointer">✕</button>'
            +'</div>';
        document.body.appendChild(overlay);
    }
}

// ── Team Detail Modal ────────────────────────────────────────────────
function footballShowTeam(teamId, teamName, teamCrest) {
    if (!teamId) return;
    _footballTeamId = teamId;
    _footballTeamLeague = _footballLeague;
    _footballTeamTabActive = 'fixtures';
    document.getElementById('football-team-name').textContent = teamName;
    const crestEl = document.getElementById('football-team-crest');
    if (teamCrest) { crestEl.src = teamCrest; crestEl.style.display = ''; }
    else { crestEl.style.display = 'none'; }
    const modal = document.getElementById('football-team-modal');
    modal.style.display = 'flex';
    footballTeamTab('fixtures');
}

function footballCloseTeamModal() {
    document.getElementById('football-team-modal').style.display = 'none';
}

function footballTeamTab(tab) {
    _footballTeamTabActive = tab;
    ['fixtures','news'].forEach(t => {
        const btn = document.getElementById('team-tab-btn-'+t);
        if (!btn) return;
        const isActive = t === tab;
        btn.style.color = isActive ? 'var(--text)' : 'var(--text2)';
        btn.style.fontWeight = isActive ? '600' : 'normal';
        btn.style.borderBottomColor = isActive ? 'var(--accent,#2563eb)' : 'transparent';
    });
    if (tab === 'fixtures') _footballTeamLoadFixtures();
    else if (tab === 'news') _footballTeamLoadNews();
}

async function _footballTeamLoadFixtures() {
    const el = document.getElementById('football-team-content');
    if (!el) return;
    _fbSetLoading(el, 'Loading schedule…');
    try {
        // Use server-side proxy: correct season year + multi-competition (regular + cups)
        const r = await fetch(API + '/api/football/team_fixtures?team_id='+encodeURIComponent(_footballTeamId)+'&league='+encodeURIComponent(_footballTeamLeague));
        const raw = await r.json();
        if (raw.error) throw new Error(raw.error);
        const events = raw.events || [];
        if (!events.length) { el.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">No schedule found for this team.</div>'; return; }
        const nowMs = Date.now();
        const upcoming = [], past = [];
        for (const event of events) {
            const comp = (event.competitions || [])[0] || {};
            const statusName = comp.status?.type?.name || '';
            const isFinal = statusName === 'STATUS_FINAL';
            const isLive = statusName === 'STATUS_IN_PROGRESS' || statusName === 'STATUS_HALFTIME';
            const competitors = comp.competitors || [];
            const home = competitors.find(c => c.homeAway === 'home') || {};
            const away = competitors.find(c => c.homeAway === 'away') || {};
            const ht = home.team || {}, at = away.team || {};
            const dt = event.date ? new Date(event.date) : null;
            const eventMs = dt ? dt.getTime() : 0;
            // Competition label (e.g. FA Cup, Champions League)
            const compName = event.season?.slug || event.name || '';
            const compLabel = comp.tournament?.displayName || comp.notes?.[0]?.headline || '';
            const dateStr = dt ? dt.toLocaleDateString('en-GB',{weekday:'short',day:'numeric',month:'short'})+' · '+dt.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'}) : '';
            const scoreH = (isFinal||isLive) ? parseInt(home.score||0) : null;
            const scoreA = (isFinal||isLive) ? parseInt(away.score||0) : null;
            const homeW = isFinal && scoreH > scoreA, awayW = isFinal && scoreA > scoreH;
            const compBadge = compLabel ? '<div style="font-size:9px;color:var(--text3);text-align:center;margin-bottom:2px">'+compLabel+'</div>' : '';
            const centerEl = isLive
                ? '<span style="background:#f44336;color:#fff;padding:2px 5px;border-radius:3px;font-size:9px;font-weight:600">LIVE</span>'
                : isFinal
                    ? '<div style="font-weight:700;font-size:14px;letter-spacing:1px">'+scoreH+' - '+scoreA+'</div><div style="font-size:9px;color:var(--text3)">'+dateStr+'</div>'
                    : compBadge+'<div style="font-size:10px;color:var(--text3);text-align:center;line-height:1.3">'+dateStr+'</div>';
            const row = '<div style="display:flex;align-items:center;padding:8px 10px;background:var(--bg3);border-radius:6px;border:1px solid var(--border);gap:8px;margin-bottom:6px">'
                +'<div style="flex:1;display:flex;align-items:center;justify-content:flex-end;gap:5px;text-align:right">'
                +(ht.logo?'<img src="'+ht.logo+'" style="width:18px;height:18px;object-fit:contain" onerror="this.style.display=\'none\'">':'')
                +'<span style="font-weight:'+(homeW?'700':'500')+';font-size:12px;color:'+(homeW?'var(--text)':'var(--text2)')+'">'+( ht.shortDisplayName||ht.displayName||'?')+'</span></div>'
                +'<div style="min-width:76px;text-align:center">'+centerEl+'</div>'
                +'<div style="flex:1;display:flex;align-items:center;gap:5px">'
                +(at.logo?'<img src="'+at.logo+'" style="width:18px;height:18px;object-fit:contain" onerror="this.style.display=\'none\'">':'')
                +'<span style="font-weight:'+(awayW?'700':'500')+';font-size:12px;color:'+(awayW?'var(--text)':'var(--text2)')+'">'+( at.shortDisplayName||at.displayName||'?')+'</span></div>'
                +'</div>';
            const entry = {date: eventMs, row};
            // Classify: final → past; live or date >= now-2hr → upcoming/live; date < now-2hr and not final → treat as past
            if (isFinal || (!isLive && eventMs < nowMs - 7200000)) past.push(entry);
            else upcoming.push(entry);
        }
        // Sort: upcoming ascending (soonest first), past descending (most recent first)
        upcoming.sort((a,b) => a.date - b.date);
        past.sort((a,b) => b.date - a.date);
        // Limit past results shown to keep it tidy
        const pastShown = past.slice(0, 10);
        let html = '';
        if (upcoming.length) {
            html += '<div style="font-size:11px;font-weight:600;color:var(--blue);margin:0 0 8px;padding:4px 0;border-bottom:1px solid var(--border)">📅 Upcoming Fixtures ('+upcoming.length+')</div>';
            html += upcoming.map(e => e.row).join('');
        }
        if (pastShown.length) {
            html += '<div style="font-size:11px;font-weight:600;color:var(--text2);margin:'+(upcoming.length?'14px':'0')+' 0 8px;padding:4px 0;border-bottom:1px solid var(--border)">✅ Recent Results ('+pastShown.length+(past.length>10?' of '+past.length:'')+')</div>';
            html += pastShown.map(e => e.row).join('');
        }
        if (!html) html = '<div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">No events found.</div>';
        _fbClearLoading(el); el.innerHTML = html;
    } catch(e) {
        _fbClearLoading(el); el.innerHTML = '<div style="color:#f66;font-size:12px;padding:20px;text-align:center">Failed to load fixtures: '+e.message+'</div>';
    }
}

async function _footballTeamLoadNews() {
    const el = document.getElementById('football-team-content');
    if (!el) return;
    _fbSetLoading(el, 'Loading news…');
    try {
        // Use server-side proxy to avoid browser CORS/rate-limit issues with ESPN
        const r = await fetch(API + '/api/football/team_news?team_id='+encodeURIComponent(_footballTeamId)+'&league='+encodeURIComponent(_footballTeamLeague));
        const raw = await r.json();
        if (raw.error && !raw.articles) throw new Error(raw.error);
        const articles = raw.articles || [];
        if (!articles.length) { el.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">No news available for this team.</div>'; return; }
        let html = '';
        articles.forEach(a => {
            const dt = a.published ? new Date(a.published) : null;
            const dateStr = dt ? dt.toLocaleDateString('en-GB',{weekday:'short',day:'numeric',month:'short'}) : '';
            const imgUrl = (a.images || [])[0]?.url || '';
            const link = a.links?.web?.href || a.links?.mobile?.href || '';
            const safeLink = (link||'').replace(/'/g,"\\'");
            html += '<div style="display:flex;gap:10px;padding:10px;background:var(--bg3);border-radius:6px;border:1px solid var(--border);margin-bottom:6px;cursor:pointer" onclick="if(\''+safeLink+'\')window.open(\''+safeLink+'\',\'_blank\')">';
            if (imgUrl) html += '<img src="'+imgUrl+'" style="width:72px;height:54px;object-fit:cover;border-radius:4px;flex-shrink:0" onerror="this.style.display=\'none\'">';
            html += '<div style="flex:1;min-width:0"><div style="font-weight:600;font-size:12px;line-height:1.4;margin-bottom:4px">'+a.headline+'</div>';
            if (a.description) html += '<div style="font-size:10px;color:var(--text2);line-height:1.4;margin-bottom:3px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">'+a.description+'</div>';
            html += '<div style="font-size:10px;color:var(--text3)">'+dateStr+'</div></div></div>';
        });
        _fbClearLoading(el); el.innerHTML = html;
    } catch(e) {
        _fbClearLoading(el); el.innerHTML = '<div style="color:#f66;font-size:12px;padding:20px;text-align:center">Failed to load news: '+e.message+'</div>';
    }
}

// 10. IPTV PLAYER
// ══════════════════════════════════════════════════════════════════════
let _iptvChannels    = [];
let _iptvFiltered    = [];
let _iptvCatFilter   = 'All';
let _iptvCurrentId   = null;
let _iptvCurrentName = '';
let _iptvView        = 'channels';
let _iptvMVCells     = [];   // multiview channel IDs per cell
let _iptvMVCols      = 2, _iptvMVRows = 2;
let _iptvInited      = false;
let _iptvSource      = localStorage.getItem('iptv_source') || 'moviebite';
let _iptvTZOffset    = parseInt(localStorage.getItem('iptv_tz_offset') || '0', 10);

// ── DaddyLive domain — changes frequently; user can override in Settings ──
// Stream URL: <domain>/stream/stream-<id>.php
let _daddyliveDomain   = localStorage.getItem('iptv_daddylive_domain')   || 'daddylive.lat';
let _daddylivePattern  = localStorage.getItem('iptv_daddylive_pattern')  || 'stream';
function iptvSetDaddyliveDomain(d) {
    _daddyliveDomain = (d || 'daddylive.me').replace(/^https?:\/\//, '').replace(/\/$/, '');
    localStorage.setItem('iptv_daddylive_domain', _daddyliveDomain);
}
function iptvSetDaddylivePattern(p) {
    _daddylivePattern = p || 'stream';
    localStorage.setItem('iptv_daddylive_pattern', _daddylivePattern);
}

function iptvDaddylivePreset(val) {
    const customInput = document.getElementById('iptv-dl-domain');
    if (val === 'custom') {
        if (customInput) customInput.style.display = '';
    } else {
        if (customInput) customInput.style.display = 'none';
        iptvSetDaddyliveDomain(val);
        // Sync preset dropdown to selected value
        const preset = document.getElementById('iptv-dl-domain-preset');
        if (preset) preset.value = val;
    }
}

function _daddyliveStreamUrl(id) {
    const patterns = {
        'stream':  `/stream/stream-${id}.php`,
        'live':    `/live/stream-${id}.php`,
        'embed':   `/embed/stream-${id}.php`,
        'premium': `/premium-streams/stream-${id}.php`,
    };
    return `https://${_daddyliveDomain}${patterns[_daddylivePattern] || patterns.stream}`;
}

// ── BinTV hard-coded channel list ────────────────────────────────────
// Slugs from https://www.bintv.net/  (format: /channel/<slug>)
const _BINTV_CHANNELS = [
    { id: 'sky-sports-main-event',      name: 'Sky Sports Main Event',   group: 'Sports'      },
    { id: 'sky-sports-premier-league',  name: 'Sky Sports Premier League',group: 'Sports'     },
    { id: 'sky-sports-football',        name: 'Sky Sports Football',      group: 'Sports'      },
    { id: 'sky-sports-cricket',         name: 'Sky Sports Cricket',       group: 'Sports'      },
    { id: 'sky-sports-f1',             name: 'Sky Sports F1',            group: 'Sports'      },
    { id: 'sky-sports-golf',           name: 'Sky Sports Golf',          group: 'Sports'      },
    { id: 'sky-sports-action',         name: 'Sky Sports Action',        group: 'Sports'      },
    { id: 'sky-sports-arena',          name: 'Sky Sports Arena',         group: 'Sports'      },
    { id: 'sky-sports-mix',            name: 'Sky Sports Mix',           group: 'Sports'      },
    { id: 'sky-sports-news',           name: 'Sky Sports News',          group: 'Sports'      },
    { id: 'bt-sport-1',               name: 'TNT Sports 1',             group: 'Sports'      },
    { id: 'bt-sport-2',               name: 'TNT Sports 2',             group: 'Sports'      },
    { id: 'bt-sport-3',               name: 'TNT Sports 3',             group: 'Sports'      },
    { id: 'bt-sport-4',               name: 'TNT Sports 4',             group: 'Sports'      },
    { id: 'espn-uk',                  name: 'ESPN UK',                  group: 'Sports'      },
    { id: 'eurosport-1',              name: 'Eurosport 1',              group: 'Sports'      },
    { id: 'eurosport-2',              name: 'Eurosport 2',              group: 'Sports'      },
    { id: 'bbc-one',                  name: 'BBC One',                  group: 'Entertainment'},
    { id: 'bbc-two',                  name: 'BBC Two',                  group: 'Entertainment'},
    { id: 'itv',                      name: 'ITV',                      group: 'Entertainment'},
    { id: 'channel-4',               name: 'Channel 4',                group: 'Entertainment'},
    { id: 'channel-5',               name: 'Channel 5',                group: 'Entertainment'},
    { id: 'sky-one',                  name: 'Sky One',                  group: 'Entertainment'},
    { id: 'sky-news',                 name: 'Sky News',                 group: 'News'         },
    { id: 'bbc-news',                 name: 'BBC News',                 group: 'News'         },
    { id: 'cnn-international',        name: 'CNN International',        group: 'News'         },
    { id: 'al-jazeera',              name: 'Al Jazeera',               group: 'News'         },
];

// ── DaddyLive hard-coded channel list ────────────────────────────────
// Stream IDs from daddylive.me/stream/stream-<id>.php
const _DADDYLIVE_CHANNELS = [
    { id: '1',  name: 'Sky Sports Main Event',    group: 'Sports'  },
    { id: '2',  name: 'Sky Sports Premier League', group: 'Sports' },
    { id: '3',  name: 'Sky Sports Football',       group: 'Sports'  },
    { id: '4',  name: 'Sky Sports Cricket',        group: 'Sports'  },
    { id: '5',  name: 'Sky Sports F1',            group: 'Sports'  },
    { id: '6',  name: 'Sky Sports Golf',          group: 'Sports'  },
    { id: '7',  name: 'Sky Sports Action',        group: 'Sports'  },
    { id: '8',  name: 'Sky Sports Arena',         group: 'Sports'  },
    { id: '9',  name: 'Sky Sports News',          group: 'Sports'  },
    { id: '10', name: 'Sky Sports Mix',           group: 'Sports'  },
    { id: '11', name: 'TNT Sports 1',            group: 'Sports'  },
    { id: '12', name: 'TNT Sports 2',            group: 'Sports'  },
    { id: '13', name: 'TNT Sports 3',            group: 'Sports'  },
    { id: '14', name: 'TNT Sports 4',            group: 'Sports'  },
    { id: '15', name: 'ESPN US',                 group: 'Sports'  },
    { id: '16', name: 'ESPN2 US',                group: 'Sports'  },
    { id: '17', name: 'ESPN3 US',                group: 'Sports'  },
    { id: '18', name: 'Eurosport 1',             group: 'Sports'  },
    { id: '19', name: 'Eurosport 2',             group: 'Sports'  },
    { id: '20', name: 'DAZN 1',                  group: 'Sports'  },
    { id: '21', name: 'DAZN 2',                  group: 'Sports'  },
    { id: '22', name: 'Fox Sports 1',            group: 'Sports'  },
    { id: '23', name: 'Fox Sports 2',            group: 'Sports'  },
    { id: '24', name: 'NBC Sports',              group: 'Sports'  },
    { id: '25', name: 'beIN Sports 1',           group: 'Sports'  },
    { id: '26', name: 'beIN Sports 2',           group: 'Sports'  },
    { id: '27', name: 'beIN Sports 3',           group: 'Sports'  },
    { id: '28', name: 'Movistar LaLiga',         group: 'Sports'  },
    { id: '29', name: 'Movistar Champions',      group: 'Sports'  },
    { id: '30', name: 'Canal+ Sport',            group: 'Sports'  },
    { id: '31', name: 'SuperSport 1',            group: 'Sports'  },
    { id: '32', name: 'SuperSport 2',            group: 'Sports'  },
    { id: '33', name: 'SuperSport Football',     group: 'Sports'  },
    { id: '34', name: 'Sport TV1',               group: 'Sports'  },
    { id: '35', name: 'Sport TV2',               group: 'Sports'  },
    { id: '36', name: 'MUTV',                    group: 'Sports'  },
    { id: '37', name: 'Chelsea TV',              group: 'Sports'  },
    { id: '38', name: 'Liverpool TV',            group: 'Sports'  },
    { id: '39', name: 'Eleven Sports 1',         group: 'Sports'  },
    { id: '40', name: 'Eleven Sports 2',         group: 'Sports'  },
    { id: '41', name: 'CNN International',       group: 'News'    },
    { id: '42', name: 'BBC News',                group: 'News'    },
    { id: '43', name: 'Sky News',                group: 'News'    },
    { id: '44', name: 'Al Jazeera English',      group: 'News'    },
    { id: '45', name: 'Fox News',                group: 'News'    },
    { id: '46', name: 'MSNBC',                   group: 'News'    },
    { id: '47', name: 'Euronews',                group: 'News'    },
    { id: '48', name: 'BBC One',                 group: 'UK TV'   },
    { id: '49', name: 'BBC Two',                 group: 'UK TV'   },
    { id: '50', name: 'ITV',                     group: 'UK TV'   },
];

function iptvSetSource(src) {
    _iptvSource = src;
    localStorage.setItem('iptv_source', src);
    const sel = document.getElementById('iptv-source-select');
    if (sel) sel.value = src;
    _iptvChannels = [];
    _iptvInited = false;   // force re-init so new source channels load
    _iptvInitUI();         // show/hide domain input
    iptvInit();
}

function iptvSetTZOffset(val) {
    _iptvTZOffset = parseInt(val, 10);
    localStorage.setItem('iptv_tz_offset', val);
}

function _iptvInitUI() {
    // Restore saved source/TZ
    const srcSel = document.getElementById('iptv-source-select');
    if (srcSel) srcSel.value = _iptvSource;
    const tzSel = document.getElementById('iptv-tz-offset');
    if (tzSel) tzSel.value = String(_iptvTZOffset);
    // Show domain input only for DaddyLive
    const dlWrap = document.getElementById('iptv-dl-domain-wrap');
    if (dlWrap) dlWrap.style.display = _iptvSource === 'daddylive' ? 'flex' : 'none';
    const dlInput = document.getElementById('iptv-dl-domain');
    if (dlInput) dlInput.value = _daddyliveDomain;
    const dlPattern = document.getElementById('iptv-dl-pattern');
    if (dlPattern) dlPattern.value = _daddylivePattern;
    const dlPreset = document.getElementById('iptv-dl-domain-preset');
    if (dlPreset) {
        // Select matching preset or 'custom'
        const opts = Array.from(dlPreset.options).map(o => o.value);
        dlPreset.value = opts.includes(_daddyliveDomain) ? _daddyliveDomain : 'custom';
        const customInput = document.getElementById('iptv-dl-domain');
        if (customInput) customInput.style.display = dlPreset.value === 'custom' ? '' : 'none';
        if (customInput && dlPreset.value === 'custom') customInput.value = _daddyliveDomain;
    }
}

function iptvSetView(v) {
    _iptvView = v;
    document.getElementById('iptv-channels-view').style.display  = v==='channels'  ? '' : 'none';
    document.getElementById('iptv-schedule-view').style.display  = v==='schedule'  ? '' : 'none';
    document.getElementById('iptv-multiview-view').style.display = v==='multiview' ? '' : 'none';
    ['channels','schedule','multiview'].forEach(n => {
        const b = document.getElementById('iptv-view-'+n);
        if (b) b.classList.toggle('active', n===v);
    });
    if (v==='schedule') iptvLoadSchedule('live', document.getElementById('iptv-sched-live'));
    if (v==='multiview') iptvRenderMV();
}

async function iptvInit() {
    if (_iptvInited && _iptvChannels.length) return;
    _iptvInited = true;
    _iptvInitUI();
    await iptvLoadSourceChannels();
}

async function iptvLoadSourceChannels() {
    if (_iptvSource === 'bintv') {
        _iptvChannels = _BINTV_CHANNELS.map(c => ({...c, logo:''}));
        iptvBuildCatPills();
        iptvFilterChannels();
    } else if (_iptvSource === 'daddylive') {
        _iptvChannels = _DADDYLIVE_CHANNELS.map(c => ({...c, logo:''}));
        iptvBuildCatPills();
        iptvFilterChannels();
    } else {
        await iptvFetchChannels();
    }
}

async function iptvFetchChannels() {
    const listEl = document.getElementById('iptv-channel-list');
    if (listEl) listEl.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">Loading channels…</div>';
    try {
        const r = await fetch(API + '/api/iptv/channels');
        const d = await r.json();
        _iptvChannels = d.channels || [];
        if (d.fallback) {
            // Add a small note
            const note = document.createElement('div');
            note.style = 'font-size:10px;color:var(--text3);padding:4px 8px;text-align:center';
            note.textContent = '⚠️ Showing cached list — live API unreachable';
            listEl?.prepend(note);
        }
        iptvBuildCatPills();
        iptvFilterChannels();
    } catch(e) {
        if (listEl) listEl.innerHTML = '<div style="color:var(--red);font-size:12px;padding:20px;text-align:center">Failed to load channels</div>';
    }
}

function iptvBuildCatPills() {
    const cats = ['All', ...new Set(_iptvChannels.map(c => c.group||'General').filter(Boolean))].sort((a,b)=>{
        if(a==='All') return -1; if(b==='All') return 1; return a.localeCompare(b);
    });
    const el = document.getElementById('iptv-cat-pills');
    if (!el) return;
    el.innerHTML = cats.map(c => `<div class="filter-pill${c===_iptvCatFilter?' active':''}" onclick="iptvSetCat('${c}')">${c}</div>`).join('');
}

function iptvSetCat(cat) {
    _iptvCatFilter = cat;
    document.querySelectorAll('#iptv-cat-pills .filter-pill').forEach(p =>
        p.classList.toggle('active', p.textContent===cat));
    iptvFilterChannels();
}

function iptvFilterChannels() {
    const q    = (document.getElementById('iptv-search')?.value||'').toLowerCase();
    const cat  = _iptvCatFilter;
    _iptvFiltered = _iptvChannels.filter(c => {
        const matchCat  = cat==='All' || c.group===cat;
        const matchName = !q || c.name.toLowerCase().includes(q);
        return matchCat && matchName;
    });
    iptvRenderList();
}

function iptvRenderList() {
    const el = document.getElementById('iptv-channel-list');
    if (!el) return;
    if (!_iptvFiltered.length) {
        el.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:16px;text-align:center">No channels found</div>';
        return;
    }
    el.innerHTML = _iptvFiltered.map(ch => `
        <div style="display:flex;align-items:center;border-radius:5px;transition:background .1s;${_iptvCurrentId===ch.id?'background:var(--blue2);color:var(--blue);':'color:var(--text2);'}">
          <div onclick="iptvPlayChannel('${ch.id}','${ch.name.replace(/'/g,"\\'")}',this.parentElement)"
               style="display:flex;align-items:center;gap:8px;padding:7px 8px;flex:1;min-width:0;cursor:pointer"
               onmouseover="if('${ch.id}'!=='${_iptvCurrentId}')this.parentElement.style.background='var(--surface)'"
               onmouseout="if('${ch.id}'!=='${_iptvCurrentId}')this.parentElement.style.background=''">
            <span style="font-size:14px;width:22px;text-align:center;flex-shrink:0">${ch.logo ? `<img src="${ch.logo}" style="width:18px;height:18px;object-fit:contain" onerror="this.replaceWith('📺')">` : '📺'}</span>
            <span style="font-size:12px;font-weight:${_iptvCurrentId===ch.id?600:400};white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1">${ch.name}</span>
            ${ch.group?`<span style="font-size:9px;color:var(--text3);flex-shrink:0;white-space:nowrap;margin-left:4px">${ch.group}</span>`:''}
          </div>
          ${ch.custom ? `<button onclick="iptvDeleteCustomChannel('${ch.id}',event)" style="background:none;border:none;color:var(--text3);cursor:pointer;padding:4px 8px;flex-shrink:0;font-size:11px;line-height:1" title="Remove">✕</button>` : ''}
        </div>`).join('');
}

function iptvPlayChannel(id, name, rowEl) {
    _iptvCurrentId   = id;
    _iptvCurrentName = name;
    // Highlight selected row
    document.querySelectorAll('#iptv-channel-list > div').forEach(el => {
        el.style.background = '';
        el.style.color      = 'var(--text2)';
    });
    if (rowEl) { rowEl.style.background='var(--blue2)'; rowEl.style.color='var(--blue)'; }

    const frame = document.getElementById('iptv-player-frame');
    const placeholder = document.getElementById('iptv-player-placeholder');
    const nowPlaying  = document.getElementById('iptv-now-playing');
    const nowSource   = document.getElementById('iptv-now-source');

    if (frame) {
        const streamUrls = {
            moviebite: `https://live.moviebite.cc/channels/${id}`,
            bintv:     `https://www.bintv.net/channel/${id}`,
            daddylive: _daddyliveStreamUrl(id)
        };
        frame.src = streamUrls[_iptvSource] || streamUrls.moviebite;
        frame.style.display = '';
    }
    if (placeholder) placeholder.style.display = 'none';
    if (nowPlaying)  nowPlaying.textContent = name;
    const sourceNames = { moviebite: 'MovieBite', bintv: 'BinTV', daddylive: `DaddyLive (${_daddyliveDomain}/${_daddylivePattern})` };
    if (nowSource)   nowSource.textContent  = `${sourceNames[_iptvSource]||'Stream'} · ${name}`;

    const popoutBtn = document.getElementById('iptv-popout-btn');
    const popUrls = {
        moviebite: `https://live.moviebite.cc/channels/${id}`,
        bintv:     `https://www.bintv.net/channel/${id}`,
        daddylive: _daddyliveStreamUrl(id)
    };
    if (popoutBtn) popoutBtn.dataset.url = popUrls[_iptvSource] || popUrls.moviebite;
}

function iptvPopout() {
    const url = document.getElementById('iptv-popout-btn')?.dataset.url;
    if (url) window.open(url, '_blank');
}

function iptvFullscreen() {
    const wrap  = document.getElementById('iptv-player-wrap');
    const frame = document.getElementById('iptv-player-frame');
    // Prefer to fullscreen the wrap div; iframe fullscreen works on some browsers
    const el = (wrap && wrap.requestFullscreen) ? wrap : frame;
    if (!el) return;
    if      (el.requestFullscreen)       el.requestFullscreen();
    else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
    else if (el.mozRequestFullScreen)    el.mozRequestFullScreen();
    else if (el.msRequestFullscreen)     el.msRequestFullscreen();
    else if (frame && frame.requestFullscreen) frame.requestFullscreen();
}

function iptvPlayHLS() {
    const url = document.getElementById('iptv-hls-url')?.value.trim();
    if (!url) return;
    const vid = document.getElementById('iptv-hls-player');
    if (!vid) return;
    vid.style.display = '';
    if (window.Hls && Hls.isSupported()) {
        const hls = new Hls();
        hls.loadSource(url);
        hls.attachMedia(vid);
        hls.on(Hls.Events.MANIFEST_PARSED, () => vid.play().catch(()=>{}));
    } else if (vid.canPlayType('application/vnd.apple.mpegurl')) {
        vid.src = url;
        vid.play().catch(()=>{});
    } else {
        showToast('HLS not supported in this browser', 'error');
    }
}

// ── Schedule ─────────────────────────────────────────────────────────
async function iptvLoadSchedule(type, btnEl) {
    const listEl  = document.getElementById('iptv-schedule-list');
    const statusEl= document.getElementById('iptv-sched-status');
    document.querySelectorAll('#iptv-schedule-view .filter-pill').forEach(b=>b.classList.remove('active'));
    if (btnEl) btnEl.classList.add('active');
    if (listEl) listEl.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">Loading events…</div>';
    if (statusEl) statusEl.textContent = '';
    try {
        const r = await fetch(API + `/api/iptv/schedule?type=${type}`);
        const matches = await r.json();
        const list = Array.isArray(matches) ? matches : (matches.matches || []);
        if (!list.length) {
            if (listEl) listEl.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">No events found</div>';
            return;
        }
        if (statusEl) statusEl.textContent = `${list.length} event${list.length!==1?'s':''}`;
        listEl.innerHTML = list.slice(0,60).map(m => {
            const ts      = m.time || m.date || 0;
            const adjTs   = ts ? ts + (_iptvTZOffset * 3600) : 0;
            const dt      = adjTs ? new Date(adjTs*1000).toLocaleString(undefined,{weekday:'short',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : 'TBD';
            const nowSecs = Date.now()/1000;
            const isLive  = ts && (nowSecs - ts) < 7200 && ts < nowSecs + 300;
            const srcs  = (m.sources||[]).map(s=>`<span style="font-size:9px;background:var(--surface2);padding:1px 5px;border-radius:3px">${s.source||s.id||'stream'}</span>`).join(' ');
            return `
            <div style="display:flex;align-items:center;gap:12px;padding:9px 12px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r);transition:border-color .15s;cursor:default"
                 onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor='var(--border)'">
              <div style="flex:1;min-width:0">
                <div style="font-size:12px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${m.title||m.name||'Event'}</div>
                <div style="font-size:11px;color:var(--text3);margin-top:2px">${dt} ${srcs}</div>
              </div>
              ${isLive ? '<span style="font-size:10px;background:var(--red2,rgba(248,81,73,0.15));color:var(--red);padding:2px 6px;border-radius:4px;white-space:nowrap">● LIVE</span>' : ''}
              ${(m.sources||[]).length ? `<button class="btn" style="font-size:11px;padding:3px 10px;flex-shrink:0" onclick="window.open('https://streamed.su/watch/${m.id}','_blank')">▶ Watch</button>` : ''}
            </div>`;
        }).join('');
    } catch(e) {
        if (listEl) listEl.innerHTML = `<div style="color:var(--red);font-size:12px;padding:20px;text-align:center">Schedule unavailable — ${e.message}</div>`;
    }
}

// ── Multiview ─────────────────────────────────────────────────────────
function iptvMVLayout(cols, rows, btnEl) {
    _iptvMVCols = cols; _iptvMVRows = rows;
    document.querySelectorAll('#iptv-multiview-view .filter-pill').forEach(b=>b.classList.remove('active'));
    if (btnEl) btnEl.classList.add('active');
    iptvRenderMV();
}

function iptvRenderMV() {
    const grid = document.getElementById('iptv-mv-grid');
    if (!grid) return;
    const total = _iptvMVCols * _iptvMVRows;
    grid.style.gridTemplateColumns = `repeat(${_iptvMVCols},1fr)`;
    // Ensure _iptvMVCells has enough slots
    while (_iptvMVCells.length < total) _iptvMVCells.push(null);
    grid.innerHTML = Array.from({length: total}, (_, i) => {
        const ch = _iptvMVCells[i] ? _iptvChannels.find(c=>c.id===_iptvMVCells[i]) : null;
        return `
        <div style="background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;position:relative;aspect-ratio:16/9">
          ${ch
            ? `<iframe src="${_iptvSource==='daddylive'?'https://'+_daddyliveDomain+'/stream/stream-'+ch.id+'.php':_iptvSource==='bintv'?'https://www.bintv.net/channel/'+ch.id:'https://live.moviebite.cc/channels/'+ch.id}" style="position:absolute;top:-60px;left:0;width:100%;height:calc(100% + 60px);border:none" allow="autoplay;fullscreen;picture-in-picture" allowfullscreen></iframe>`
            : `<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;gap:6px;color:var(--text3);cursor:pointer" onclick="iptvMVPickChannel(${i})">
                 <div style="font-size:28px">📺</div>
                 <div style="font-size:11px">Click to assign channel</div>
               </div>`}
          <div style="position:absolute;top:4px;right:4px;display:flex;gap:4px">
            ${ch?`<button style="background:rgba(0,0,0,.6);color:#fff;border:none;border-radius:4px;font-size:10px;padding:2px 6px;cursor:pointer" onclick="iptvMVPickChannel(${i})">⇄</button>`:''}
            ${ch?`<button style="background:rgba(0,0,0,.6);color:#fff;border:none;border-radius:4px;font-size:10px;padding:2px 6px;cursor:pointer" onclick="iptvMVClearCell(${i})">✕</button>`:''}
          </div>
          ${ch?`<div style="position:absolute;bottom:4px;left:6px;font-size:10px;background:rgba(0,0,0,.6);color:#fff;padding:1px 6px;border-radius:3px">${ch.name}</div>`:''}
        </div>`;
    }).join('');
}

function iptvMVPickChannel(cellIdx) {
    // Show a small inline picker using the current filtered list
    const pick = _iptvChannels.slice(0, 50).map(c =>
        `<div onclick="iptvMVAssign(${cellIdx},'${c.id}')" style="padding:5px 8px;cursor:pointer;font-size:11px;border-radius:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" onmouseover="this.style.background='var(--surface)'" onmouseout="this.style.background=''">${c.name}</div>`
    ).join('');
    const modal = document.getElementById('iptv-mv-picker') || (() => {
        const d = document.createElement('div');
        d.id = 'iptv-mv-picker';
        d.style = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);z-index:2000;width:260px;max-height:360px;overflow-y:auto;padding:8px;box-shadow:0 8px 32px rgba(0,0,0,.5)';
        document.body.appendChild(d);
        return d;
    })();
    modal.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px"><span style="font-size:12px;font-weight:600">Pick Channel</span><button onclick="document.getElementById('iptv-mv-picker').style.display='none'" style="background:none;border:none;color:var(--text);cursor:pointer;font-size:14px">✕</button></div><input placeholder="Search…" oninput="iptvMVSearch(this.value,${cellIdx})" style="width:100%;padding:5px 8px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:4px;font-size:11px;margin-bottom:6px;box-sizing:border-box"><div id="iptv-mv-pick-list">${pick}</div>`;
    modal.style.display = '';
    modal.dataset.cell  = cellIdx;
}

function iptvMVSearch(q, cellIdx) {
    const f = q.toLowerCase();
    const list = _iptvChannels.filter(c=>!f||c.name.toLowerCase().includes(f)).slice(0,50);
    const el = document.getElementById('iptv-mv-pick-list');
    if (el) el.innerHTML = list.map(c => `<div onclick="iptvMVAssign(${cellIdx},'${c.id}')" style="padding:5px 8px;cursor:pointer;font-size:11px;border-radius:3px" onmouseover="this.style.background='var(--surface)'" onmouseout="this.style.background=''">${c.name}</div>`).join('');
}

function iptvMVAssign(cellIdx, chId) {
    _iptvMVCells[cellIdx] = chId;
    const picker = document.getElementById('iptv-mv-picker');
    if (picker) picker.style.display = 'none';
    iptvRenderMV();
}

function iptvMVClearCell(cellIdx) {
    _iptvMVCells[cellIdx] = null;
    iptvRenderMV();
}

function iptvMVClearAll() {
    _iptvMVCells = [];
    iptvRenderMV();
}

function iptvAddToMultiview() {
    if (!_iptvCurrentId) { showToast('No channel selected','error'); return; }
    // Find first empty cell
    const emptyIdx = _iptvMVCells.findIndex(c=>!c);
    if (emptyIdx === -1) {
        showToast('Multiview grid is full — clear a slot first','error'); return;
    }
    _iptvMVCells[emptyIdx] = _iptvCurrentId;
    showToast(`${_iptvCurrentName} added to multiview`, 'success');
}

function iptvReload() {
    _iptvChannels = []; _iptvFiltered = []; _iptvInited = false;
    iptvInit();
}

function iptvBrowseChannels() {
    const panel = document.getElementById('iptv-browse-modal');
    const iframe = document.getElementById('iptv-browse-iframe');
    if (!panel) return;
    const isVisible = panel.style.display === 'flex';
    if (isVisible) { iptvHideBrowse(); return; }  // toggle off if already open
    if (iframe && !iframe.src) {
        const urls = { moviebite: 'https://live.moviebite.cc/channels', bintv: 'https://www.bintv.net/', daddylive: `https://${_daddyliveDomain}/` };
        iframe.src = urls[_iptvSource] || urls.moviebite;
    }
    panel.style.display = 'flex';
    panel.style.flexDirection = 'column';
    // Scroll the panel into view smoothly
    setTimeout(() => panel.scrollIntoView({behavior:'smooth', block:'nearest'}), 50);
}
function iptvHideBrowse() {
    const panel = document.getElementById('iptv-browse-modal');
    if (panel) panel.style.display = 'none';
}

// ── Add / delete custom channels ────────────────────────────────────────────
function iptvShowAddChannel() {
    const m = document.getElementById('iptv-add-modal');
    if (!m) return;
    document.getElementById('iptv-add-name').value = '';
    document.getElementById('iptv-add-slug').value = '';
    document.getElementById('iptv-add-group').value = '';
    m.style.display = 'flex';
}
function iptvHideAddChannel() {
    const m = document.getElementById('iptv-add-modal');
    if (m) m.style.display = 'none';
}
async function iptvSaveAddChannel() {
    const name  = document.getElementById('iptv-add-name').value.trim();
    const slug  = document.getElementById('iptv-add-slug').value.trim().toUpperCase().replace(/\s+/g,'-');
    const group = document.getElementById('iptv-add-group').value.trim() || 'Custom';
    if (!name || !slug) { showToast('Name and Slug are required','error',2000); return; }
    try {
        const r = await fetch(API+'/api/iptv/channels/custom', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({id:slug, name, group})
        });
        const d = await r.json();
        if (d.error) { showToast('Error: '+d.error,'error'); return; }
        iptvHideAddChannel();
        showToast(`"${name}" added to channels`,'success');
        // Reload channel list
        _iptvChannels = []; _iptvInited = false;
        await iptvFetchChannels();
    } catch(e) { showToast('Failed to add channel','error'); }
}
async function iptvDeleteCustomChannel(id, event) {
    if (event) event.stopPropagation();
    if (!confirm('Remove this channel from your list?')) return;
    try {
        await fetch(API+`/api/iptv/channels/custom/${encodeURIComponent(id)}`, {method:'DELETE'});
        showToast('Channel removed','success',2000);
        if (_iptvCurrentId === id) {
            const frame = document.getElementById('iptv-player-frame');
            const placeholder = document.getElementById('iptv-player-placeholder');
            if (frame) { frame.src='about:blank'; frame.style.display='none'; }
            if (placeholder) placeholder.style.display='';
            _iptvCurrentId = null;
        }
        _iptvChannels = []; _iptvInited = false;
        await iptvFetchChannels();
    } catch(e) { showToast('Failed to remove channel','error'); }
}

// ══════════════════════════════════════════════════════════════════════
// 11. ALERTS BAR
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
    // Recent logs excerpt — color-coded by severity
    try {
        const r = await fetch(API + '/api/logs?lines=12');
        const d = await r.json();
        const el = document.getElementById('ov-log-excerpt');
        if (el) {
            const lines = (d.lines || []).slice(-12);
            if (!lines.length) { el.textContent = '(no logs)'; }
            else {
                el.innerHTML = lines.map(line => {
                    const u = line.toUpperCase();
                    let col = 'var(--text2)';
                    if (/\b(ERR(?:OR)?|CRITICAL|FATAL)\b/.test(u))  col = 'var(--red)';
                    else if (/\b(WARN(?:ING)?)\b/.test(u))           col = 'var(--yellow,#e3b341)';
                    else if (/\b(INFO|NOTICE|STARTED|DONE|OK)\b/.test(u)) col = 'var(--green)';
                    else if (/\b(DEBUG|TRACE)\b/.test(u))            col = 'var(--text3)';
                    const safe = line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                    return `<span style="color:${col}">${safe}</span>`;
                }).join('\n');
            }
        }
    } catch(e) {}
    // Network I/O — update values + proportional bars
    try {
        const r = await fetch(API + '/api/network');
        const d = await r.json();
        const io = d.io || {};
        const sent = io.bytes_sent || 0;
        const recv = io.bytes_recv || 0;
        const total = sent + recv || 1;
        setEl('ov-net-sent', fmtBytes(sent));
        setEl('ov-net-recv', fmtBytes(recv));
        const sentBar = document.getElementById('net-sent-bar');
        const recvBar = document.getElementById('net-recv-bar');
        if (sentBar) sentBar.style.width = Math.max(4, Math.round(sent/total*100)) + '%';
        if (recvBar) recvBar.style.width = Math.max(4, Math.round(recv/total*100)) + '%';
    } catch(e) {}
    // Dashboard containers panel
    loadDashboardContainers();
    // Service integration cards
    loadRadarrCard();
    loadSonarrCard();
    loadPlexCard();
    loadSeerrCard();
    loadQbitCard('active');
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

// ── Downloader type switcher ────────────────────────────────────────────
function dlTypeChanged() {
    const t = document.getElementById('svc-dl-type')?.value || 'qbittorrent';
    ['qbittorrent','transmission','deluge'].forEach(dt => {
        const el = document.getElementById(`dl-fields-${dt}`);
        if (el) el.style.display = dt === t ? 'flex' : 'none';
    });
}

// ── qBittorrent downloads card ────────────────────────────────────────
async function loadQbitCard(filter) {
    const body = document.getElementById('qbit-card-body');
    const speedEl = document.getElementById('qbit-speed');
    if (!body) return;
    body.innerHTML = '<div style="color:var(--text3);font-size:12px;text-align:center;padding:12px">Loading…</div>';
    try {
        const r = await fetch('/api/services/downloader/torrents');
        const d = await r.json();
        if (speedEl) speedEl.textContent = `↓ ${d.dl_speed || '0 B/s'}  ↑ ${d.ul_speed || '0 B/s'}`;
        if (d.error) {
            body.innerHTML = `<div style="color:var(--text3);font-size:11px;padding:8px;text-align:center">${d.error}</div>`;
            return;
        }
        let torrents = d.torrents || [];
        if (filter === 'active') torrents = torrents.filter(t => ['downloading','uploading','stalledDL','forcedDL','metaDL'].includes(t.state));
        if (!torrents.length) {
            body.innerHTML = `<div style="color:var(--text3);font-size:12px;text-align:center;padding:12px">${filter==='active'?'No active downloads':'No torrents'}</div>`;
            return;
        }
        const stateColor = s => ({'downloading':'var(--blue)','uploading':'var(--green)','seeding':'var(--green)','stalledDL':'var(--yellow)','error':'var(--red)','pausedDL':'var(--text3)','pausedUP':'var(--text3)'}[s] || 'var(--text3)');
        const stateIcon  = s => ({'downloading':'↓','uploading':'↑','seeding':'↑','stalledDL':'⏸','error':'⚠','pausedDL':'⏸','pausedUP':'⏸','forcedDL':'↓','metaDL':'🔍'}[s] || '•');
        body.innerHTML = torrents.map(t => `
          <div style="padding:6px 8px;border-bottom:1px solid var(--border);display:flex;flex-direction:column;gap:3px">
            <div style="font-size:11px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${(t.name||'').replace(/</g,'&lt;')}</div>
            <div style="display:flex;align-items:center;gap:8px">
              <div style="flex:1;height:4px;background:var(--border);border-radius:2px;overflow:hidden">
                <div style="height:100%;width:${t.progress}%;background:${stateColor(t.state)};transition:width .3s"></div>
              </div>
              <span style="font-size:10px;color:var(--text3);font-family:var(--mono);white-space:nowrap">${t.progress}%</span>
            </div>
            <div style="display:flex;gap:10px;font-size:10px;color:var(--text3)">
              <span style="color:${stateColor(t.state)}">${stateIcon(t.state)} ${t.state}</span>
              <span>${t.size}</span>
              ${t.state==='downloading'?`<span style="color:var(--blue)">↓${t.dlspeed}</span>`:''}
              ${t.category?`<span style="background:var(--surface2);padding:0 4px;border-radius:3px">${t.category}</span>`:''}
            </div>
          </div>`).join('');
    } catch(e) {
        body.innerHTML = `<div style="color:var(--red);font-size:11px;padding:8px">Error: ${e.message}</div>`;
    }
}

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
    } else if (svc === 'qbit') {
        loadQbitCard(tab);
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
            // Poster thumbnail — Overseerr serves via /imageproxy/ or TMDB directly
            const posterUrl = req.poster ? `https://image.tmdb.org/t/p/w92${req.poster}` : '';
            const thumbEl = posterUrl
                ? `<img src="${posterUrl}" style="width:32px;height:48px;object-fit:cover;border-radius:3px;flex-shrink:0" onerror="this.style.display='none'">`
                : `<span style="font-size:18px;flex-shrink:0;width:32px;text-align:center">${typeIcon}</span>`;
            return `<div class="svc-item" onclick="_toggleDetail('${did}')" style="align-items:flex-start;gap:8px">
              ${thumbEl}
              <div style="flex:1;min-width:0">
                <div style="font-size:12px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${req.title || ''}">${req.title || '?'}</div>
                <div style="font-size:10px;color:var(--text3);margin-top:2px">${typeIcon} ${req.type || '?'} · ${req.createdAt}</div>
                <div style="font-size:10px;color:var(--text3)">by ${req.requestedBy || '?'}</div>
              </div>
              <span style="font-size:10px;font-weight:600;color:${color};flex-shrink:0;padding:2px 6px;background:${color}22;border-radius:4px">${lbl}</span>
            </div>
            <div class="svc-detail" id="${did}">
              ${req.type === 'movie' ? '🎬 Movie' : '📺 TV Show'} · Status: <strong>${lbl}</strong><br>
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
        seerr_url:  'svc-seerr-url',  seerr_api_key:  'svc-seerr-key',
        football_api_key: 'svc-football-key',
        qbittorrent_url: 'svc-qbit-url', qbittorrent_user: 'svc-qbit-user', qbittorrent_pass: 'svc-qbit-pass',
        downloader_type: 'svc-dl-type',
        transmission_url: 'svc-transmission-url', transmission_user: 'svc-transmission-user', transmission_pass: 'svc-transmission-pass',
        deluge_url: 'svc-deluge-url', deluge_pass: 'svc-deluge-pass'
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

async function saveWeatherLocation() {
    const city = (document.getElementById('cfg-weather-city')?.value || '').trim();
    const country = (document.getElementById('cfg-weather-country')?.value || '').trim();
    try {
        const r = await fetch(API + '/api/settings', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({ weather_city: city, weather_country: country })
        });
        const d = await r.json();
        if (d.error) { showToast('Error: ' + d.error, 'error'); return; }
        showToast(city ? `Weather set to ${city}${country ? ', ' + country : ''}` : 'Weather set to auto-detect (IP)', 'success');
        // Refresh weather immediately
        loadWeather();
    } catch(e) { showToast('Save failed', 'error'); }
}

async function saveRedditCredentials() {
    const st = document.getElementById('reddit-creds-status');
    const clientId     = (document.getElementById('cfg-reddit-client-id')?.value     || '').trim();
    const clientSecret = (document.getElementById('cfg-reddit-client-secret')?.value || '').trim();
    const username     = (document.getElementById('cfg-reddit-username')?.value      || '').trim();
    const password     = (document.getElementById('cfg-reddit-password')?.value      || '').trim();
    try {
        await fetch(API + '/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({reddit_client_id: clientId, reddit_client_secret: clientSecret,
                                  reddit_username: username, reddit_password: password})});
        if (st) { st.textContent = '✓ Saved — reload feeds to test'; setTimeout(() => { if(st) st.textContent=''; }, 5000); }
    } catch(e) {
        if (st) st.textContent = 'Failed to save';
    }
}

// ── Config Backup & Restore ─────────────────────────────────────────────────
async function exportConfig() {
    const st = document.getElementById('config-backup-status');
    try {
        const r = await fetch(API + '/api/config/export');
        if (!r.ok) throw new Error('Export failed: ' + r.status);
        const blob = await r.blob();
        const dt = new Date().toISOString().slice(0, 10);
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `arrhub-config-${dt}.json`;
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        if (st) { st.textContent = '✓ Config exported'; setTimeout(() => { if (st) st.textContent = ''; }, 4000); }
        showToast('Config exported successfully', 'success', 3000);
    } catch(e) {
        if (st) st.textContent = 'Export failed';
        showToast('Export failed: ' + e.message, 'error', 4000);
    }
}

function importConfig() {
    const input = document.createElement('input');
    input.type = 'file'; input.accept = '.json';
    input.onchange = async e => {
        const file = e.target.files[0];
        if (!file) return;
        const st = document.getElementById('config-backup-status');
        try {
            const text = await file.text();
            const data = JSON.parse(text);
            if (!data.arrhub_backup) throw new Error('Not a valid ArrHub backup file');
            const r = await fetch(API + '/api/config/import', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });
            const result = await r.json();
            if (result.error) throw new Error(result.error);
            if (st) st.textContent = `✓ Restored ${result.restored} settings — reloading…`;
            showToast(`Restored ${result.restored} settings — reloading page…`, 'success', 3000);
            setTimeout(() => location.reload(), 2800);
        } catch(err) {
            if (st) { st.style.color = 'var(--red)'; st.textContent = 'Import failed: ' + err.message; }
            showToast('Import failed: ' + err.message, 'error', 5000);
        }
    };
    input.click();
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

// ── Combined dashboard refresh (replaces separate logs + network calls) ──────
async function loadDashboardData() {
    // Only fetch when overview is active to save server load
    if (currentTab !== 'overview') return;
    try {
        const r = await fetch(API + '/api/dashboard');
        const d = await r.json();
        // Logs
        const el = document.getElementById('ov-log-excerpt');
        if (el && d.logs && d.logs.length) {
            el.innerHTML = d.logs.slice(-12).map(line => {
                const u = line.toUpperCase();
                let col = 'var(--text2)';
                if (/\b(ERR(?:OR)?|CRITICAL|FATAL)\b/.test(u))       col = 'var(--red)';
                else if (/\b(WARN(?:ING)?)\b/.test(u))                col = 'var(--yellow,#e3b341)';
                else if (/\b(INFO|NOTICE|STARTED|DONE|OK)\b/.test(u)) col = 'var(--green)';
                else if (/\b(DEBUG|TRACE)\b/.test(u))                 col = 'var(--text3)';
                const safe = line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                return `<span style="color:${col}">${safe}</span>`;
            }).join('\n');
        }
        // Network IO
        const io = d.net_io || {};
        const sent = io.bytes_sent || 0, recv = io.bytes_recv || 0, total = sent + recv || 1;
        setEl('ov-net-sent', fmtBytes(sent));
        setEl('ov-net-recv', fmtBytes(recv));
        const sentBar = document.getElementById('net-sent-bar');
        const recvBar = document.getElementById('net-recv-bar');
        if (sentBar) sentBar.style.width = Math.max(4, Math.round(sent/total*100)) + '%';
        if (recvBar) recvBar.style.width = Math.max(4, Math.round(recv/total*100)) + '%';
    } catch(e) {}
}

// ── Polling ───────────────────────────────────────────────────────────
// Pause all non-critical polling when the browser tab is hidden
let _pollPaused = false;
document.addEventListener('visibilitychange', () => {
    _pollPaused = document.hidden;
});

// Active containers tab: refresh full list every 10s
setInterval(() => {
    if (!_pollPaused && currentTab === 'containers') loadContainers();
}, 10000);

// Slower polls for other tabs (every 15s)
setInterval(() => {
    if (_pollPaused) return;
    if (currentTab === 'storage') loadStorage();
    else if (currentTab === 'stornet') { loadStorage(); loadNetwork(); }
    else if (currentTab === 'overview') loadDashboardContainers();
}, 15000);

// Background stats refresh for running containers (every 45s when NOT on containers tab)
setInterval(() => {
    if (!_pollPaused && currentTab !== 'containers') {
        allContainers.filter(c => c.status === 'running').forEach(c => loadCtrStats(c.name, false));
    }
}, 45000);

// Combined dashboard data (logs + network) — every 30s, only when on overview tab
setInterval(() => { if (!_pollPaused) { loadDashboardData(); loadDashStorage(); } }, 30000);

// Service integration cards — every 60s (these call external services; no need to hammer)
setInterval(() => {
    if (!_pollPaused && currentTab === 'overview') {
        loadRadarrCard(); loadSonarrCard(); loadPlexCard(); loadSeerrCard(); loadQbitCard('active');
    }
}, 60000);

// ── Boot — staggered to avoid stampeding a single gunicorn worker ─────
updateGreeting();
try { initGauges(); } catch(e) { console.warn('Chart.js not ready, gauges disabled:', e); }
startSSE();                                    // t=0  — SSE handles live CPU/RAM/load
loadOverview();                                // t=0  — initial static info (hostname etc.)
setTimeout(loadContainers,   300);             // t=300ms
setTimeout(loadWeather,      700);             // t=700ms
setTimeout(loadDockerInfo,  1100);             // t=1.1s
setTimeout(() => {                             // t=1.8s — first extras pass
    loadDashboardData();
    loadDashboardContainers();
    loadDashStorage();                         // storage widget (reuses /api/storage)
}, 1800);
setTimeout(() => {                             // t=3s — service cards last (external APIs)
    loadRadarrCard(); loadSonarrCard(); loadPlexCard(); loadSeerrCard(); loadQbitCard('active');
}, 3000);
setTimeout(refreshAlerts, 2500);
// Fallback: ensure dashboard is visible after 3s
setTimeout(() => {
    const grid = document.getElementById('ov-grid');
    if (grid && !grid.classList.contains('gs-ready')) {
        grid.querySelectorAll('.grid-stack-item').forEach(i => i.style.visibility = 'visible');
    }
}, 3000);

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

// ── Dashboard uses CSS Grid — no drag-and-drop ────────────────────────────
// _gs and _gsEditing stubs: dashboard is now CSS Grid, GridStack removed in v3.15.21
// These prevent ReferenceErrors from legacy widget helper functions below.
let _gs = null;
let _gsEditing = false;
(function() {
  const el = document.getElementById('ov-dashboard') || document.getElementById('ov-grid');
  if (el) el.classList.add('gs-ready');
})();

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

// ── Widget palette definitions ────────────────────────────────────────────────
// span: grid column span in the 12-col grid
// scrollable: whether the cell has overflow-y:auto (true = has max-height)
const WIDGET_DEFS = {
  gauges:   { label: 'System Gauges',    icon: '📊', desc: 'CPU, RAM & load gauges',              span: 8,  scrollable: false },
  sysinfo:  { label: 'System Info',      icon: 'ℹ️', desc: 'OS, kernel, hostname, uptime',        span: 4,  scrollable: false },
  weather:  { label: 'Weather',          icon: '🌤️', desc: 'Current weather & forecast',          span: 4,  scrollable: false },
  storage:  { label: 'Storage',          icon: '💾', desc: 'Disk usage per filesystem mount',     span: 4,  scrollable: true  },
  services: { label: 'Service Cards',    icon: '🃏', desc: 'ARR / Plex / qBit service cards',    span: 12, scrollable: true  },
  infra:    { label: 'Docker & Network', icon: '🐳', desc: 'Docker engine stats + network I/O',  span: 8,  scrollable: true  },
  logs:     { label: 'Recent Logs',      icon: '📋', desc: 'Live log stream excerpt',             span: 4,  scrollable: true  },
  apps:     { label: 'Apps & Containers',icon: '🚀', desc: 'Service launcher + live container list', span: 8, scrollable: false },
  todo:     { label: 'To-Do List',       icon: '✅', desc: 'Editable personal task list',         span: 4,  scrollable: true  },
  calendar: { label: 'Calendar',         icon: '📅', desc: 'Monthly calendar with custom events', span: 12, scrollable: false },
};

let _hiddenWidgets = new Set();

// Save full widget config (hidden list) to server
// CSS Grid layout — grid positions are fixed, only hidden state is persisted
async function _saveWidgetConfig() {
  try {
    const config = {
      hidden: [..._hiddenWidgets],
      grid: null
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
        const cell = document.getElementById(`dash-${id}`);
        if (cell) cell.style.display = 'none';
      });
      _syncResetBtn();
    }
  } catch(e) {}
}

function _syncResetBtn() {
  const btn = document.getElementById('ov-reset-btn');
  if (btn) btn.style.display = _hiddenWidgets.size > 0 ? '' : 'none';
}

// Remove (hide) a widget
function removeWidget(id) {
  const cell = document.getElementById(`dash-${id}`);
  if (!cell) return;
  _hiddenWidgets.add(id);
  cell.style.display = 'none';
  _syncResetBtn();
  _saveWidgetConfig();
  showToast(`"${WIDGET_DEFS[id]?.label || id}" hidden — click Widgets to restore`, 'info', 2500);
}

// Restore a hidden widget
function restoreWidget(id) {
  const def = WIDGET_DEFS[id];
  if (!def) return;
  _hiddenWidgets.delete(id);
  const cell = document.getElementById(`dash-${id}`);
  if (cell) cell.style.display = '';
  _syncResetBtn();
  _saveWidgetConfig();
  showToast(`"${def.label}" restored`, 'success', 2000);
  showWidgetPalette(); // refresh in-place
}

// Show widget palette modal — Homarr-style
function showWidgetPalette() {
  const body = document.getElementById('widget-palette-body');
  const countEl = document.getElementById('widget-palette-count');
  if (!body) return;
  body.innerHTML = '';
  const hidden = [..._hiddenWidgets].length;
  const total = Object.keys(WIDGET_DEFS).length;
  if (countEl) countEl.textContent = hidden > 0 ? `${hidden} widget${hidden>1?'s':''} hidden` : 'All widgets visible';
  Object.entries(WIDGET_DEFS).forEach(([id, def]) => {
    const isHidden = _hiddenWidgets.has(id);
    const div = document.createElement('div');
    div.className = 'widget-palette-card' + (isHidden ? '' : ' active');
    div.title = isHidden ? 'Click to show on dashboard' : 'Click to hide from dashboard';
    div.innerHTML = `
      <div class="wpc-icon">${def.icon}</div>
      <div class="wpc-name">${def.label}</div>
      <div class="wpc-status">${isHidden
        ? '<span style="color:var(--text3)">Hidden</span>'
        : '<span style="color:var(--green)">✓ Visible</span>'}</div>
      <div style="font-size:9px;color:var(--text3);margin-top:3px;text-align:center;line-height:1.3">${def.desc}</div>`;
    div.onclick = () => {
      if (isHidden) restoreWidget(id);
      else { removeWidget(id); showWidgetPalette(); }
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

function updateGreeting() {
    const h = new Date().getHours();
    let greeting = 'Good morning';
    if (h >= 12 && h < 18) greeting = 'Good afternoon';
    else if (h >= 18) greeting = 'Good evening';

    const greetEl = document.getElementById('ov-greeting');
    if (greetEl) greetEl.textContent = greeting;

    const dateEl = document.getElementById('ov-date');
    if (dateEl) {
        const now = new Date();
        dateEl.textContent = now.toLocaleDateString('en-US', {weekday:'long', month:'long', day:'numeric', year:'numeric'});
    }
}

async function loadServiceLauncher() {
  const elOld = document.getElementById('launcher-tiles');
  const elNew = document.getElementById('ov-launcher-grid');
  const targetEls = [elOld, elNew].filter(e => e);
  if (!targetEls.length) return;
  try {
    const r = await fetch('/api/containers');
    const data = await r.json();
    const running = (data.containers || []).filter(c => c.status === 'running');
    if (!running.length) {
      const msg = '<div style="color:var(--text3);font-size:12px;padding:8px">No running containers found.</div>';
      targetEls.forEach(el => { el.innerHTML = msg; });
      return;
    }
    const html = running.map(c => {
      const name = (c.name || '').replace(/^\//, '');
      const ports = c.ports || [];
      const skipPorts = new Set(['53','25','110','143','993','995','22','21','5432','3306','6379','27017']);
      const webPorts = ports.filter(p => {
        const hp = p.split(':')[0];
        return hp && /^\d+$/.test(hp) && !skipPorts.has(hp) && parseInt(hp) >= 1024;
      });
      const portEntry = webPorts[0] || ports.find(p => /^\d+:\d+/.test(p));
      const hostPort = portEntry ? portEntry.split(':')[0] : null;
      const scheme = hostPort && (hostPort === '443' || hostPort.endsWith('443')) ? 'https' : 'http';
      const url = hostPort ? `${scheme}://${window.location.hostname}:${hostPort}` : null;
      const icon = _launcherIcon(name);
      const tileHtml = `<div class="launcher-tile-icon">${icon}</div>
        <div class="launcher-tile-name">${name}</div>
        ${hostPort ? `<div class="launcher-tile-port">:${hostPort}</div>` : ''}`;
      return url
        ? `<a href="${url}" target="_blank" rel="noopener" class="launcher-tile">${tileHtml}</a>`
        : `<div class="launcher-tile" style="opacity:.5;cursor:default">${tileHtml}</div>`;
    }).join('');
    targetEls.forEach(el => { el.innerHTML = html; });
  } catch(e) {
    const msg = '<div style="color:var(--text3);font-size:12px">Failed to load containers.</div>';
    targetEls.forEach(el => { el.innerHTML = msg; });
  }
}

// ── Calendar ──────────────────────────────────────────────────────────────
let _calEvents = {};  // {"YYYY-MM-DD": [{id, title, color}, ...]}
let _calYear = new Date().getFullYear();
let _calMonth = new Date().getMonth();  // 0-based
let _calSelectedDate = null;
(function _calLoad() {
  try { _calEvents = JSON.parse(localStorage.getItem('arrhub_calendar') || '{}'); } catch(e) { _calEvents = {}; }
})();
function _calSave() { localStorage.setItem('arrhub_calendar', JSON.stringify(_calEvents)); }

function calRender() {
  const el = document.getElementById('cal-grid');
  const lbl = document.getElementById('cal-month-label');
  if (!el) return;
  const monthNames = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  if (lbl) lbl.textContent = `${monthNames[_calMonth]} ${_calYear}`;
  const today = new Date();
  const todayStr = `${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`;
  const firstDay = new Date(_calYear, _calMonth, 1).getDay();  // 0=Sun
  const daysInMonth = new Date(_calYear, _calMonth + 1, 0).getDate();
  let html = '';
  // Empty cells before first day
  for (let i = 0; i < firstDay; i++) html += '<div style="min-height:52px"></div>';
  for (let d = 1; d <= daysInMonth; d++) {
    const dateStr = `${_calYear}-${String(_calMonth+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
    const isToday = dateStr === todayStr;
    const isSelected = dateStr === _calSelectedDate;
    const evts = _calEvents[dateStr] || [];
    const dots = evts.slice(0,3).map(e => `<div style="width:6px;height:6px;border-radius:50%;background:${e.color || '#2563eb'};flex-shrink:0"></div>`).join('');
    const extraCount = evts.length > 3 ? `<span style="font-size:8px;color:var(--text3)">+${evts.length-3}</span>` : '';
    html += `<div onclick="calDayClick('${dateStr}')" style="min-height:52px;padding:4px 5px;border-radius:6px;background:${isSelected?'var(--blue2)':isToday?'var(--surface)':'var(--bg3)'};border:1px solid ${isSelected?'var(--blue)':isToday?'var(--accent,#2563eb)':'var(--border)'};cursor:pointer;transition:background .15s;position:relative" title="Click to add event">
      <div style="font-size:11px;font-weight:${isToday?'700':'500'};color:${isToday?'var(--accent,#2563eb)':'var(--text)'};">${d}</div>
      ${evts.length ? `<div style="display:flex;flex-wrap:wrap;gap:2px;margin-top:3px;">${dots}${extraCount}</div>` : ''}
      ${evts.slice(0,2).map(e => `<div title="${e.title}" style="font-size:9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:1px 3px;border-radius:3px;background:${e.color||'#2563eb'}22;color:${e.color||'#2563eb'};margin-top:1px">${e.title.replace(/</g,'&lt;')}</div>`).join('')}
    </div>`;
  }
  el.innerHTML = html;
}

function calDayClick(dateStr) {
  _calSelectedDate = dateStr;
  const form = document.getElementById('cal-event-form');
  const lbl = document.getElementById('cal-form-date-label');
  if (lbl) lbl.textContent = new Date(dateStr + 'T12:00:00').toLocaleDateString('en-GB',{weekday:'long',day:'numeric',month:'long',year:'numeric'});
  const inp = document.getElementById('cal-event-title');
  if (inp) inp.value = '';
  if (form) form.style.display = 'flex';
  calRender();
  // If there are existing events on this day, show them above the form
  const evts = _calEvents[dateStr] || [];
  if (evts.length) {
    const existingEl = document.getElementById('cal-existing-events');
    // Inject existing events list if not already present
    if (!existingEl && form) {
      const div = document.createElement('div');
      div.id = 'cal-existing-events';
      div.style.cssText = 'margin-top:8px;display:flex;flex-direction:column;gap:3px';
      form.appendChild(div);
    }
    const eel = document.getElementById('cal-existing-events');
    if (eel) eel.innerHTML = evts.map(e =>
      `<div style="display:flex;align-items:center;gap:6px;padding:3px 6px;background:var(--bg3);border-radius:5px">
        <div style="width:8px;height:8px;border-radius:50%;background:${e.color||'#2563eb'};flex-shrink:0"></div>
        <span style="flex:1;font-size:11px">${e.title.replace(/</g,'&lt;')}</span>
        <button onclick="calDeleteEvent('${dateStr}','${e.id}')" style="background:none;border:none;color:var(--text3);cursor:pointer;font-size:12px;padding:0" title="Delete">✕</button>
      </div>`
    ).join('');
  } else {
    const eel = document.getElementById('cal-existing-events');
    if (eel) eel.innerHTML = '';
  }
}

function calSubmitEvent() {
  if (!_calSelectedDate) return;
  const title = (document.getElementById('cal-event-title')?.value || '').trim();
  const color = document.getElementById('cal-event-color')?.value || '#2563eb';
  if (!title) return;
  if (!_calEvents[_calSelectedDate]) _calEvents[_calSelectedDate] = [];
  _calEvents[_calSelectedDate].push({ id: Date.now().toString(36), title, color });
  _calSave(); calRender();
  document.getElementById('cal-event-title').value = '';
  // Refresh existing events display
  calDayClick(_calSelectedDate);
}

function calDeleteEvent(dateStr, eventId) {
  if (_calEvents[dateStr]) {
    _calEvents[dateStr] = _calEvents[dateStr].filter(e => e.id !== eventId);
    if (!_calEvents[dateStr].length) delete _calEvents[dateStr];
    _calSave(); calRender();
    calDayClick(dateStr);
  }
}

function calCloseForm() {
  _calSelectedDate = null;
  const form = document.getElementById('cal-event-form');
  if (form) form.style.display = 'none';
  calRender();
}

function calPrevMonth() {
  _calMonth--;
  if (_calMonth < 0) { _calMonth = 11; _calYear--; }
  calRender();
}
function calNextMonth() {
  _calMonth++;
  if (_calMonth > 11) { _calMonth = 0; _calYear++; }
  calRender();
}
function calGoToday() {
  const now = new Date();
  _calYear = now.getFullYear();
  _calMonth = now.getMonth();
  calRender();
}

// ── To-Do List ─────────────────────────────────────────────────────────────
let _todoItems = [];
(function _todoLoad() {
  try { _todoItems = JSON.parse(localStorage.getItem('arrhub_todos') || '[]'); } catch(e) { _todoItems = []; }
})();
function _todoSave() { localStorage.setItem('arrhub_todos', JSON.stringify(_todoItems)); }
function _todoRender() {
  const el = document.getElementById('todo-list');
  const badge = document.getElementById('todo-pending-count');
  if (!el) return;
  const pending = _todoItems.filter(t => !t.done).length;
  if (badge) badge.textContent = pending > 0 ? pending : '';
  if (!_todoItems.length) { el.innerHTML = '<div style="color:var(--text3);font-size:12px;text-align:center;padding:12px">No tasks yet — add one above!</div>'; return; }
  el.innerHTML = _todoItems.map(t =>
    `<div style="display:flex;align-items:center;gap:6px;padding:5px 8px;background:var(--bg3);border:1px solid var(--border);border-radius:6px;${t.done?'opacity:.55':''}">
      <input type="checkbox" ${t.done?'checked':''} onchange="todoToggle('${t.id}')"
        style="cursor:pointer;width:14px;height:14px;accent-color:var(--accent,#2563eb)">
      <span style="flex:1;font-size:12px;${t.done?'text-decoration:line-through;color:var(--text3)':''}">${t.text.replace(/</g,'&lt;')}</span>
      <button onclick="todoDelete('${t.id}')" style="background:none;border:none;color:var(--text3);cursor:pointer;font-size:14px;padding:0 2px;line-height:1" title="Delete">✕</button>
    </div>`
  ).join('');
}
function todoAdd() {
  const inp = document.getElementById('todo-new-input');
  if (!inp) return;
  const text = inp.value.trim();
  if (!text) return;
  _todoItems.unshift({ id: Date.now().toString(36)+Math.random().toString(36).slice(2,5), text, done: false, created: Date.now() });
  _todoSave(); _todoRender();
  inp.value = '';
}
function todoToggle(id) {
  const item = _todoItems.find(t => t.id === id);
  if (item) { item.done = !item.done; _todoSave(); _todoRender(); }
}
function todoDelete(id) {
  _todoItems = _todoItems.filter(t => t.id !== id);
  _todoSave(); _todoRender();
}

function appsTabSwitch(tab) {
  const panelL = document.getElementById('apps-panel-launcher');
  const panelC = document.getElementById('apps-panel-ctrs');
  const btnL   = document.getElementById('apps-tab-btn-launcher');
  const btnC   = document.getElementById('apps-tab-btn-ctrs');
  const showLauncher = tab === 'launcher';
  if (panelL) panelL.style.display = showLauncher ? '' : 'none';
  if (panelC) panelC.style.display = showLauncher ? 'none' : '';
  if (btnL) { btnL.style.borderBottom = showLauncher ? '2px solid var(--accent,#2563eb)' : '2px solid transparent'; btnL.style.color = showLauncher ? 'var(--text)' : 'var(--text2)'; btnL.style.fontWeight = showLauncher ? '600' : '400'; }
  if (btnC) { btnC.style.borderBottom = showLauncher ? '2px solid transparent' : '2px solid var(--accent,#2563eb)'; btnC.style.color = showLauncher ? 'var(--text2)' : 'var(--text)'; btnC.style.fontWeight = showLauncher ? '400' : '600'; }
}

function toggleGridEdit() {
  const dash = document.getElementById('ov-dashboard');
  const btn  = document.getElementById('ov-edit-btn');
  if (!dash || !btn) return;
  const entering = !dash.classList.contains('edit-mode');
  dash.classList.toggle('edit-mode', entering);
  if (entering) {
    btn.style.background = 'var(--blue)';
    btn.style.color      = '#fff';
    btn.innerHTML = `<svg width="11" height="11" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg> Done`;
    showToast('Edit mode — click ✕ on any widget to hide it', 'info', 3000);
  } else {
    btn.style.background = '';
    btn.style.color      = '';
    btn.innerHTML = `<svg width="11" height="11" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg> Edit`;
  }
}

function resetGridLayout() {
  _hiddenWidgets.clear();
  _saveWidgetConfig();
  // Restore all cells
  Object.keys(WIDGET_DEFS).forEach(id => {
    const cell = document.getElementById(`dash-${id}`);
    if (cell) cell.style.display = '';
  });
  _syncResetBtn();
  showToast('All widgets restored', 'success', 2000);
}

// Init GridStack in static mode on load to apply any saved positions
window.addEventListener('load', async () => {
  loadServiceLauncher();       // populate launcher widget
  calRender();                 // render calendar
  _todoRender();               // render todo list
});
</script>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
</body>
</html>

"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9999, debug=False, threaded=True)
