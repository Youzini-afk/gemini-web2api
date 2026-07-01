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


def _config_file():
    return os.path.join(get_data_dir(), "mihomo_config.yaml")


def _log_file():
    return os.path.join(get_data_dir(), "mihomo.log")


def _tail_log(max_chars=1600):
    """Return the tail of mihomo.log for actionable startup errors."""
    path = _log_file()
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


def is_available():
    """Check if mihomo binary is available."""
    return _find_binary() is not None


def is_running():
    """Check if mihomo subprocess is currently running."""
    with _lock:
        return _is_running_unlocked()


def _is_running_unlocked():
    """Check process state. Caller must hold _lock."""
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
    """Start mihomo subprocess. Returns (success, message)."""
    global _process, _enabled

    with _lock:
        if _is_running_unlocked():
            return True, "already running"

        binary = _find_binary()
        if not binary:
            return False, "mihomo binary not found"

        config_str = _generate_config()
        if not config_str:
            if _last_config_skips:
                first = _last_config_skips[0]
                return False, f"all enabled nodes were skipped; first skip: {first.get('name')}: {first.get('reason')}"
            return False, "no enabled nodes with clash config"

        cfg_path = _config_file()
        with open(cfg_path, "w") as f:
            f.write(config_str)

        ok, test_msg = _run_config_test(binary, cfg_path)
        if not ok:
            _enabled = False
            return False, f"mihomo config test failed: {test_msg}"

        log_path = _log_file()
        log_fd = open(log_path, "a", buffering=1)

        try:
            _process = subprocess.Popen(
                [binary, "-f", cfg_path],
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,  # New process group for clean kill
            )
            log_fd.close()
            _enabled = True
            time.sleep(1)  # Give it a moment to start
            if _process.poll() is not None:
                code = _process.returncode
                _enabled = False
                tail = _tail_log()
                if tail:
                    return False, f"mihomo exited immediately (code={code}). Log tail: {tail}"
                return False, f"mihomo exited immediately (code={code}) — check {_config_file()}"
            skip_note = f", skipped {len(_last_config_skips)} invalid node(s)" if _last_config_skips else ""
            return True, f"mihomo started (pid={_process.pid}, proxy=127.0.0.1:{_local_proxy_port}{skip_note})"
        except Exception as e:
            try:
                log_fd.close()
            except Exception:
                pass
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
