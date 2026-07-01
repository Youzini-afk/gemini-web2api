"""Configuration management with persistence."""
import json
import os
import secrets
import tempfile
from typing import Optional

DEFAULT_CONFIG = {
    "port": 8081,
    "host": "0.0.0.0",
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
    "gemini_bl": "boq_assistant-bard-web-server_20260525.09_p0",
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.5-flash",
    "log_requests": True,
    "cookie_file": None,
    "proxy": None,
    "api_key": None,
    "api_keys": [],
    "admin_password": None,
}

CONFIG = dict(DEFAULT_CONFIG)
_config_path = None
_data_dir: str = ""

# Fields the admin settings page can read/write (everything except internals)
SETTINGS_FIELDS = [
    "retry_attempts", "retry_delay_sec", "request_timeout_sec",
    "default_model", "log_requests", "gemini_bl",
    "auth_user", "xsrf_token", "proxy", "cookie_file",
]


def get_config_path():
    """Return the path of the loaded config file, or None."""
    return _config_path


def get_data_dir():
    """Return the data directory for persistent files (nodes, health, etc.).

    Resolved as: directory of the loaded config file, or ~/.config/gemini-web2api/.
    """
    global _data_dir
    if _data_dir:
        return _data_dir
    if _config_path:
        d = os.path.dirname(os.path.abspath(_config_path))
    else:
        d = os.path.expanduser("~/.config/gemini-web2api")
    _data_dir = d
    os.makedirs(d, exist_ok=True)
    return d


def load_config(path: Optional[str] = None):
    """Load config from JSON file and track the path for later saves.

    Container deployments often mount an empty persistent volume on first boot.
    If GEMINI_WEB2API_CONFIG points at /app/config/config.json but that file is
    not created yet, we still need to bind _config_path to the future file so
    nodes.json, node_health.json, subscriptions.json, api_keys.json, and later
    config writes all land in /app/config instead of the ephemeral fallback
    under /root/.config/gemini-web2api.
    """
    global _config_path, _data_dir
    if path:
        _config_path = path
        _data_dir = ""
        if os.path.exists(path):
            with open(path) as f:
                CONFIG.update(json.load(f))
        else:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return CONFIG


def find_config():
    """Search for config file in standard locations."""
    for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
        if os.path.exists(p):
            return p
    return None


def save_config():
    """Atomically write current CONFIG back to the loaded config file.

    Returns True on success, False if no config path is set or write fails.
    """
    if not _config_path:
        return False
    try:
        d = os.path.dirname(os.path.abspath(_config_path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(CONFIG, f, indent=2, ensure_ascii=False)
        os.replace(tmp, _config_path)
        return True
    except Exception:
        return False


def ensure_admin_password():
    """Generate and persist an admin password if none is set.

    Prints it once to stderr. Env var GEMINI_WEB2API_ADMIN_PASSWORD overrides.
    """
    import sys
    env_pw = os.environ.get("GEMINI_WEB2API_ADMIN_PASSWORD", "").strip()
    if env_pw:
        CONFIG["admin_password"] = env_pw
        save_config()
        return
    if not CONFIG.get("admin_password"):
        pw = secrets.token_urlsafe(9)
        CONFIG["admin_password"] = pw
        save_config()
        print(f"  Admin password (auto-generated, save it now): {pw}", file=sys.stderr)


def load_env():
    """Load environment variable overrides."""
    api_key = os.environ.get("GEMINI_WEB2API_API_KEY") or os.environ.get("API_KEY")
    if api_key:
        CONFIG["api_key"] = api_key
    return CONFIG


def get_effective_proxy(mihomo_proxy=None):
    """Return the proxy URL that upstream requests should use.

    Priority:
    1. mihomo local proxy (when node pool is active and mihomo is running)
    2. CONFIG["proxy"]
    3. None (system env / direct)
    """
    if mihomo_proxy:
        return mihomo_proxy
    return CONFIG.get("proxy")
