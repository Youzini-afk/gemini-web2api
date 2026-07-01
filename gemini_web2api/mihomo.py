"""Mihomo (Clash) subprocess manager: config generation, lifecycle, controller API.

Runs mihomo as an external binary. Generates Clash YAML from enabled nodes.
Uses mihomo's external controller API for runtime node switching and delay testing.

Design constraints (@oracle):
- stdout/stderr redirected to log files (not PIPE — avoids deadlock)
- Process-group cleanup on stop
- Controller bound to 127.0.0.1 with generated secret
- Selector proxy group for runtime switching
- Graceful degradation when mihomo binary is not installed
"""
import json
import os
import signal
import socket
import subprocess
import threading
import time
import secrets
import hashlib
import urllib.request
import urllib.parse

from .config import get_data_dir
from . import nodes

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

_process = None
# RLock prevents self-deadlock when lifecycle operations call helpers that also
# inspect process state. start() previously held _lock and called is_running(),
# which tried to acquire the same non-reentrant Lock and blocked forever.
_lock = threading.RLock()
_local_proxy_port = 7890
_controller_port = 9090
_controller_secret = ""
_enabled = False
_select_group = "GEMINI_SELECT"
_uri_to_name = {}  # raw_uri -> mihomo proxy name
_last_config_skips = []

# Per-node worker pool. Each worker owns an independent mihomo process and
# single-node config, so one invalid proxy can never poison global startup.
_workers = {}  # raw_uri -> worker dict
_manual_active_raw_uri = None
_last_used_raw_uri = None
_bad_until = {}  # raw_uri -> unix ts for local startup/config backoff
_max_workers = int(os.environ.get("MIHOMO_MAX_WORKERS", "8") or 8)
_idle_timeout = int(os.environ.get("MIHOMO_IDLE_TIMEOUT", "900") or 900)


def _config_file():
    return os.path.join(get_data_dir(), "mihomo_config.yaml")


def _log_file():
    return os.path.join(get_data_dir(), "mihomo.log")


def _tail_log(max_chars=1600, path=None):
    """Return the tail of a mihomo log for actionable startup errors."""
    path = path or _log_file()
    try:
        with open(path, "rb") as f:
            try:
                f.seek(-max_chars, os.SEEK_END)
            except OSError:
                f.seek(0)
            data = f.read().decode("utf-8", errors="replace").strip()
            return data[-max_chars:]
    except Exception:
        return ""


def _run_config_test(binary, cfg_path, max_chars=2400):
    """Run `mihomo -t -f <config>` and return (ok, message)."""
    try:
        result = subprocess.run(
            [binary, "-t", "-f", cfg_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = ((result.stdout or "") + ("\n" if result.stdout and result.stderr else "") + (result.stderr or "")).strip()
        if result.returncode == 0:
            return True, output[-max_chars:]
        return False, output[-max_chars:] or f"mihomo config test failed with code {result.returncode}"
    except subprocess.TimeoutExpired:
        return False, "mihomo config test timed out"
    except Exception as e:
        return False, str(e)


def _find_binary():
    """Find mihomo binary. Returns path or None."""
    # Check common locations
    candidates = [
        os.environ.get("MIHOMO_PATH", ""),
        "/usr/local/bin/mihomo",
        "/usr/bin/mihomo",
        "/app/mihomo",
        "mihomo",  # PATH lookup
    ]
    for c in candidates:
        if not c:
            continue
        try:
            result = subprocess.run(
                ["which", c] if c != "mihomo" else ["which", "mihomo"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass
    # Direct check
    for c in candidates:
        if c and c != "mihomo" and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _workers_dir():
    path = os.path.join(get_data_dir(), "mihomo_workers")
    os.makedirs(path, exist_ok=True)
    return path


def _raw_hash(raw_uri):
    return hashlib.sha256(raw_uri.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _worker_alive(worker):
    proc = worker.get("process") if worker else None
    return proc is not None and proc.poll() is None


def _stop_worker_unlocked(raw_uri):
    global _manual_active_raw_uri, _last_used_raw_uri
    worker = _workers.pop(raw_uri, None)
    _uri_to_name.pop(raw_uri, None)
    if _manual_active_raw_uri == raw_uri:
        _manual_active_raw_uri = None
    if _last_used_raw_uri == raw_uri:
        _last_used_raw_uri = None
    if not worker:
        return
    proc = worker.get("process")
    if not proc:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _prune_workers_unlocked():
    now = time.time()
    for raw_uri, worker in list(_workers.items()):
        if not _worker_alive(worker):
            _stop_worker_unlocked(raw_uri)
            continue
        if raw_uri == _manual_active_raw_uri:
            continue
        if now - worker.get("last_used_at", now) > _idle_timeout:
            _stop_worker_unlocked(raw_uri)
    if len(_workers) <= _max_workers:
        return
    victims = sorted(
        ((w.get("last_used_at", 0), raw) for raw, w in _workers.items() if raw != _manual_active_raw_uri),
        key=lambda x: x[0],
    )
    for _, raw_uri in victims[:max(0, len(_workers) - _max_workers)]:
        _stop_worker_unlocked(raw_uri)


def _single_node_config(raw_uri, node, mixed_port, controller_port, secret):
    if not HAS_YAML:
        return None, "PyYAML is required for mihomo config generation"
    clash = node.get("clash_config")
    if not clash:
        return None, "node has no clash_config"
    proxy = dict(clash)
    name = proxy.get("name") or node.get("name") or f"node-{_raw_hash(raw_uri)}"
    proxy["name"] = name
    skip_reason = _proxy_skip_reason(proxy)
    if skip_reason:
        return None, skip_reason
    config = {
        "mixed-port": mixed_port,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "external-controller": f"127.0.0.1:{controller_port}",
        "secret": secret,
        "proxies": [proxy],
        "proxy-groups": [
            {"name": _select_group, "type": "select", "proxies": [name]},
        ],
        "rules": [f"MATCH,{_select_group}"],
    }
    return yaml.dump(config, allow_unicode=True, default_flow_style=False), ""


def ensure_worker(raw_uri, allow_cooling=False):
    """Ensure one mihomo worker is running for raw_uri. Returns (ok, proxy_url, msg)."""
    global _last_used_raw_uri
    with _lock:
        _prune_workers_unlocked()
        now = time.time()
        worker = _workers.get(raw_uri)

        node = nodes.get_node(raw_uri)
        if not node:
            if worker:
                _stop_worker_unlocked(raw_uri)
            return False, None, "node not found"
        if node.get("disabled"):
            if worker:
                _stop_worker_unlocked(raw_uri)
            return False, None, "node is disabled"
        h = node.get("health") or {}
        last_err = str(h.get("last_test_error", "")).lower()
        hard_cooldown = h.get("last_error_type") in ("invalid_config", "proxy_start") or "config" in last_err or "mihomo worker" in last_err
        if h.get("cooldown_until", 0) > now and (hard_cooldown or not allow_cooling):
            if worker:
                _stop_worker_unlocked(raw_uri)
            return False, None, "node is cooling"
        if _bad_until.get(raw_uri, 0) > now:
            if worker:
                _stop_worker_unlocked(raw_uri)
            return False, None, "node is cooling after mihomo startup/config failure"

        if _worker_alive(worker):
            worker["last_used_at"] = now
            _last_used_raw_uri = raw_uri
            return True, f"http://127.0.0.1:{worker['mixed_port']}", "worker already running"
        if worker:
            _stop_worker_unlocked(raw_uri)
        if not node.get("clash_config"):
            return False, None, "node has no clash_config"

        binary = _find_binary()
        if not binary:
            return False, None, "mihomo binary not found"

        mixed_port = _free_port()
        controller_port = _free_port()
        secret = secrets.token_hex(12)
        stem = _raw_hash(raw_uri)
        cfg_path = os.path.join(_workers_dir(), f"{stem}.yaml")
        log_path = os.path.join(_workers_dir(), f"{stem}.log")
        config_str, err = _single_node_config(raw_uri, node, mixed_port, controller_port, secret)
        if not config_str:
            nodes.record_health(raw_uri, False, error_type="invalid_config", error_msg=err)
            _bad_until[raw_uri] = now + 3600
            return False, None, err
        with open(cfg_path, "w") as f:
            f.write(config_str)

        ok, test_msg = _run_config_test(binary, cfg_path)
        if not ok:
            nodes.record_health(raw_uri, False, error_type="invalid_config", error_msg=test_msg)
            _bad_until[raw_uri] = now + 3600
            return False, None, f"mihomo config test failed for node: {test_msg}"

        log_fd = open(log_path, "a", buffering=1)
        try:
            proc = subprocess.Popen(
                [binary, "-f", cfg_path],
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,
            )
        except Exception as e:
            log_fd.close()
            msg = str(e)
            nodes.record_health(raw_uri, False, error_type="proxy_start", error_msg=msg)
            _bad_until[raw_uri] = now + 300
            return False, None, msg
        finally:
            try:
                log_fd.close()
            except Exception:
                pass

        time.sleep(0.5)
        if proc.poll() is not None:
            code = proc.returncode
            tail = _tail_log(path=log_path)
            msg = f"mihomo worker exited immediately (code={code})"
            if tail:
                msg += f". Log tail: {tail}"
            nodes.record_health(raw_uri, False, error_type="proxy_start", error_msg=msg)
            _bad_until[raw_uri] = now + 300
            return False, None, msg

        proxy_name = (node.get("clash_config") or {}).get("name") or node.get("name") or f"node-{stem}"
        _workers[raw_uri] = {
            "process": proc,
            "mixed_port": mixed_port,
            "controller_port": controller_port,
            "secret": secret,
            "config_path": cfg_path,
            "log_path": log_path,
            "proxy_name": proxy_name,
            "last_used_at": now,
        }
        _uri_to_name[raw_uri] = proxy_name
        _last_used_raw_uri = raw_uri
        return True, f"http://127.0.0.1:{mixed_port}", f"mihomo worker started (pid={proc.pid}, proxy=127.0.0.1:{mixed_port})"


def _node_usable(raw_uri):
    now = time.time()
    node = nodes.get_node(raw_uri)
    if not node or node.get("disabled") or not node.get("clash_config"):
        return False
    if (node.get("health") or {}).get("cooldown_until", 0) > now:
        return False
    if _bad_until.get(raw_uri, 0) > now:
        return False
    return True


def get_proxy_for_request(preferred_raw_uri=None, exclude=None, attempts=8):
    """Return (raw_uri, proxy_url, msg), trying candidates until a worker starts."""
    exclude = set(exclude or [])
    tried = set()
    choices = []
    with _lock:
        preferred = preferred_raw_uri or _manual_active_raw_uri
        last_used = _last_used_raw_uri
    if last_used and last_used != preferred and not _node_usable(last_used):
        stop_worker(last_used)
    if preferred and preferred not in exclude and _node_usable(preferred):
        choices.append(preferred)
    elif preferred and preferred not in exclude:
        stop_worker(preferred)
    for node in nodes.select_available_clash_nodes(limit=attempts, exclude=exclude | set(choices)):
        choices.append(node.get("raw_uri"))

    last_msg = "no enabled nodes with clash config"
    for raw_uri in [c for c in choices if c]:
        if raw_uri in tried or raw_uri in exclude:
            continue
        tried.add(raw_uri)
        ok, proxy, msg = ensure_worker(raw_uri, allow_cooling=True)
        if ok:
            return raw_uri, proxy, msg
        last_msg = msg
    return None, None, last_msg


def worker_count():
    with _lock:
        _prune_workers_unlocked()
        return len(_workers)


def get_active_raw_uri():
    with _lock:
        return _manual_active_raw_uri or _last_used_raw_uri


def get_manual_active_raw_uri():
    with _lock:
        return _manual_active_raw_uri


def get_last_used_raw_uri():
    with _lock:
        return _last_used_raw_uri


def bad_count():
    now = time.time()
    with _lock:
        return sum(1 for until in _bad_until.values() if until > now)


def stop_worker(raw_uri, clear_bad=False):
    """Stop and forget one worker. Does not touch node storage/health."""
    with _lock:
        _stop_worker_unlocked(raw_uri)
        if clear_bad:
            _bad_until.pop(raw_uri, None)


def clear_bad(raw_uri):
    """Clear local mihomo startup/config backoff for a node."""
    with _lock:
        _bad_until.pop(raw_uri, None)


def is_available():
    """Check if mihomo binary is available."""
    return _find_binary() is not None


def is_running():
    """Check if any per-node mihomo subprocess is currently running."""
    with _lock:
        _prune_workers_unlocked()
        return any(_worker_alive(w) for w in _workers.values())


def _is_running_unlocked():
    """Check process state. Caller must hold _lock."""
    if _process is None:
        return False
    return _process.poll() is None


def get_local_proxy():
    """Return the local proxy URL if mihomo is running, else None."""
    with _lock:
        _prune_workers_unlocked()
        for raw_uri in (_manual_active_raw_uri, _last_used_raw_uri):
            if raw_uri in _workers and _worker_alive(_workers[raw_uri]):
                w = _workers[raw_uri]
                return f"http://127.0.0.1:{w['mixed_port']}"
        for w in _workers.values():
            if _worker_alive(w):
                return f"http://127.0.0.1:{w['mixed_port']}"
        return None


def get_proxy_name(raw_uri):
    """Get the mihomo proxy name for a raw_uri."""
    node = nodes.get_node(raw_uri)
    now = time.time()
    if not node or node.get("disabled") or (node.get("health") or {}).get("cooldown_until", 0) > now:
        stop_worker(raw_uri)
        return None
    with _lock:
        w = _workers.get(raw_uri)
        if w:
            return w.get("proxy_name")
    if node and node.get("clash_config"):
        return node["clash_config"].get("name") or node.get("name")
    return _uri_to_name.get(raw_uri)


def _valid_reality_short_id(value):
    """Validate Mihomo/Xray REALITY short-id.

    REALITY short-id must be an even-length hex string up to 16 chars. Empty is
    valid/omitted. Some free subscriptions contain placeholder or malformed
    short IDs; Mihomo rejects the entire config if even one proxy is invalid.
    """
    if value is None:
        return True
    s = str(value).strip()
    if not s:
        return True
    if len(s) > 16 or len(s) % 2 != 0:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in s)


def _proxy_skip_reason(proxy_dict):
    """Return reason to skip a proxy, or empty string if it is safe to emit."""
    reality = proxy_dict.get("reality-opts")
    if isinstance(reality, dict):
        sid = reality.get("short-id")
        if not _valid_reality_short_id(sid):
            return f"invalid REALITY short ID: {sid!r}"
    return ""


def _generate_config():
    """Generate Clash YAML config from enabled nodes."""
    global _last_config_skips
    if not HAS_YAML:
        return None

    enabled = nodes.get_enabled_clash_proxies()
    if not enabled:
        return None

    proxy_list = []
    proxy_names = []
    _last_config_skips = []
    _uri_to_name.clear()

    for i, (raw_uri, clash_cfg) in enumerate(enabled):
        # Use original name or generate a unique one
        name = clash_cfg.get("name", f"node-{i}")
        # Ensure uniqueness
        base_name = name
        suffix = 1
        while name in proxy_names:
            name = f"{base_name}-{suffix}"
            suffix += 1

        proxy_dict = dict(clash_cfg)
        proxy_dict["name"] = name
        skip_reason = _proxy_skip_reason(proxy_dict)
        if skip_reason:
            _last_config_skips.append({"name": name, "raw_uri": raw_uri, "reason": skip_reason})
            continue
        proxy_list.append(proxy_dict)
        proxy_names.append(name)
        _uri_to_name[raw_uri] = name

    if not proxy_list:
        return None

    global _controller_secret
    if not _controller_secret:
        _controller_secret = secrets.token_hex(8)

    config = {
        "mixed-port": _local_proxy_port,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "external-controller": f"127.0.0.1:{_controller_port}",
        "secret": _controller_secret,
        "proxies": proxy_list,
        "proxy-groups": [
            {
                "name": _select_group,
                "type": "select",
                "proxies": proxy_names,
            },
        ],
        "rules": [
            f"MATCH,{_select_group}",
        ],
    }

    return yaml.dump(config, allow_unicode=True, default_flow_style=False)


def start():
    """Warm one per-node mihomo worker. Returns (success, message)."""
    raw_uri, proxy, msg = get_proxy_for_request(attempts=max(32, _max_workers))
    if raw_uri and proxy:
        return True, msg
    return False, msg


def stop():
    """Stop all mihomo worker subprocesses."""
    global _process, _enabled, _manual_active_raw_uri, _last_used_raw_uri

    with _lock:
        for raw_uri in list(_workers.keys()):
            _stop_worker_unlocked(raw_uri)
        _manual_active_raw_uri = None
        _last_used_raw_uri = None
        if _process is None:
            _enabled = False
            return

        try:
            # Kill entire process group
            os.killpg(os.getpgid(_process.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            try:
                _process.terminate()
            except Exception:
                pass

        # Wait up to 5 seconds
        try:
            _process.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(_process.pid), signal.SIGKILL)
            except Exception:
                _process.kill()

        _process = None
        _enabled = False


def restart():
    """Restart mihomo with updated config."""
    stop()
    return start()


def switch_proxy(raw_uri):
    """Set active node and ensure its dedicated worker is running."""
    global _manual_active_raw_uri
    ok, proxy, msg = ensure_worker(raw_uri)
    if ok:
        with _lock:
            _manual_active_raw_uri = raw_uri
        return True, f"switched to {get_proxy_name(raw_uri)} ({proxy})"
    return False, msg


def test_proxy_delay(proxy_name, timeout=10):
    """Test delay for a specific proxy via controller API.
    Returns (success, latency_ms).
    """
    if not is_running():
        return False, 0

    try:
        url = f"http://127.0.0.1:{_controller_port}/proxies/{urllib.parse.quote(proxy_name)}/delay?timeout={timeout*1000}&url=https://www.google.com/generate_204"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {_controller_secret}",
        })
        with urllib.request.urlopen(req, timeout=timeout+5) as resp:
            data = json.loads(resp.read())
            delay = data.get("delay", 0)
            if delay > 0:
                return True, delay
            return False, 0
    except Exception:
        return False, 0


def test_node_delay(raw_uri, timeout=10):
    """Test delay for a node through its dedicated worker controller.
    Returns (success, latency_ms, error_msg).
    """
    ok, _proxy, msg = ensure_worker(raw_uri)
    if not ok:
        return False, 0, msg
    with _lock:
        worker = _workers.get(raw_uri)
    if not worker or not _worker_alive(worker):
        return False, 0, "mihomo worker not running"
    proxy_name = worker.get("proxy_name") or get_proxy_name(raw_uri)
    try:
        url = (
            f"http://127.0.0.1:{worker['controller_port']}/proxies/"
            f"{urllib.parse.quote(proxy_name)}/delay?timeout={timeout*1000}"
            "&url=https://www.google.com/generate_204"
        )
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {worker['secret']}",
        })
        with urllib.request.urlopen(req, timeout=timeout+5) as resp:
            data = json.loads(resp.read())
            delay = data.get("delay", 0)
            if delay > 0:
                return True, delay, ""
            return False, 0, "delay test failed"
    except Exception as e:
        return False, 0, str(e)


def get_proxy_list():
    """Get list of proxies from controller API."""
    if not is_running():
        return {}

    try:
        url = f"http://127.0.0.1:{_controller_port}/proxies"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {_controller_secret}",
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("proxies", {})
    except Exception:
        return {}
