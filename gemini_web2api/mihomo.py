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
import subprocess
import threading
import time
import secrets
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
_lock = threading.Lock()
_local_proxy_port = 7890
_controller_port = 9090
_controller_secret = ""
_enabled = False
_select_group = "GEMINI_SELECT"
_uri_to_name = {}  # raw_uri -> mihomo proxy name


def _config_file():
    return os.path.join(get_data_dir(), "mihomo_config.yaml")


def _log_file():
    return os.path.join(get_data_dir(), "mihomo.log")


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


def is_available():
    """Check if mihomo binary is available."""
    return _find_binary() is not None


def is_running():
    """Check if mihomo subprocess is currently running."""
    with _lock:
        if _process is None:
            return False
        return _process.poll() is None


def get_local_proxy():
    """Return the local proxy URL if mihomo is running, else None."""
    if is_running():
        return f"http://127.0.0.1:{_local_proxy_port}"
    return None


def get_proxy_name(raw_uri):
    """Get the mihomo proxy name for a raw_uri."""
    return _uri_to_name.get(raw_uri)


def _generate_config():
    """Generate Clash YAML config from enabled nodes."""
    if not HAS_YAML:
        return None

    enabled = nodes.get_enabled_clash_proxies()
    if not enabled:
        return None

    proxy_list = []
    proxy_names = []
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
        proxy_list.append(proxy_dict)
        proxy_names.append(name)
        _uri_to_name[raw_uri] = name

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
    """Start mihomo subprocess. Returns (success, message)."""
    global _process, _enabled

    with _lock:
        if is_running():
            return True, "already running"

        binary = _find_binary()
        if not binary:
            return False, "mihomo binary not found"

        config_str = _generate_config()
        if not config_str:
            return False, "no enabled nodes with clash config"

        cfg_path = _config_file()
        with open(cfg_path, "w") as f:
            f.write(config_str)

        log_path = _log_file()
        log_fd = open(log_path, "a")

        try:
            _process = subprocess.Popen(
                [binary, "-f", cfg_path],
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,  # New process group for clean kill
            )
            _enabled = True
            time.sleep(1)  # Give it a moment to start
            if _process.poll() is not None:
                _enabled = False
                return False, "mihomo exited immediately — check config"
            return True, f"mihomo started (pid={_process.pid}, proxy=127.0.0.1:{_local_proxy_port})"
        except Exception as e:
            _enabled = False
            return False, str(e)


def stop():
    """Stop mihomo subprocess."""
    global _process, _enabled

    with _lock:
        if _process is None:
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
    """Switch the selector group to a specific node via controller API."""
    name = _uri_to_name.get(raw_uri)
    if not name:
        return False, "node not found in mihomo config"

    if not is_running():
        return False, "mihomo not running"

    try:
        url = f"http://127.0.0.1:{_controller_port}/proxies/{urllib.parse.quote(_select_group)}"
        data = json.dumps({"name": name}).encode()
        req = urllib.request.Request(url, data=data, method="PUT", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_controller_secret}",
        })
        urllib.request.urlopen(req, timeout=5)
        return True, f"switched to {name}"
    except Exception as e:
        return False, str(e)


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
