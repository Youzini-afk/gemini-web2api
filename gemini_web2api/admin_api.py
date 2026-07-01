"""Admin API handler: dispatches /api/admin/* routes.

Integrates with server.py by exposing handle_admin_api() and is_admin_path().
"""
import json
import os
import time
import threading
import secrets
import tempfile
from http.cookies import SimpleCookie

from .config import CONFIG, save_config, get_data_dir, SETTINGS_FIELDS
from . import admin_session
from . import __version__

_stats_lock = threading.Lock()
_stats = {
    "start_time": time.time(),
    "total_requests": 0,
    "success_count": 0,
    "error_count": 0,
    "history": [],  # list of {time, path, success, latency_ms}
}
_HISTORY_MAX = 200


def record_request(path: str, success: bool, latency_ms: float):
    """Record a request for stats/history. Called by server for API requests."""
    with _stats_lock:
        _stats["total_requests"] += 1
        if success:
            _stats["success_count"] += 1
        else:
            _stats["error_count"] += 1
        _stats["history"].append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "path": path,
            "success": success,
            "latency": round(latency_ms),
        })
        if len(_stats["history"]) > _HISTORY_MAX:
            _stats["history"] = _stats["history"][-_HISTORY_MAX:]


def reset_stats():
    with _stats_lock:
        _stats["success_count"] = 0
        _stats["error_count"] = 0
        _stats["total_requests"] = 0
        _stats["history"] = []


def is_admin_api_path(path: str) -> bool:
    return path.startswith("/api/admin/")


def is_admin_page_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


# ─── API key store ───────────────────────────────────────────────────────────

_keys_lock = threading.Lock()
_keys_cache = None


def _keys_file():
    return os.path.join(get_data_dir(), "api_keys.json")


def _load_keys():
    global _keys_cache
    with _keys_lock:
        if _keys_cache is not None:
            return _keys_cache
        path = _keys_file()
        if os.path.exists(path):
            try:
                with open(path) as f:
                    _keys_cache = json.load(f)
            except Exception:
                _keys_cache = []
        else:
            _keys_cache = []
        return _keys_cache


def _save_keys(keys):
    global _keys_cache
    _keys_cache = keys
    path = _keys_file()
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(keys, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def list_keys():
    """Return list of keys with masked values."""
    keys = _load_keys()
    result = []
    for k in keys:
        raw = k.get("key", "")
        masked = raw[:3] + "·" * max(0, len(raw) - 7) + raw[-4:] if len(raw) > 7 else "sk-····"
        result.append({"name": k.get("name", ""), "key": masked, "description": k.get("description", "")})
    return result


def add_key(name: str, key: str, description: str = ""):
    if not name:
        raise ValueError("key name is required")
    if not key:
        key = "sk-" + secrets.token_hex(16)
    if not key.startswith("sk-"):
        raise ValueError("key must start with 'sk-'")
    keys = _load_keys()
    for k in keys:
        if k.get("name") == name:
            raise ValueError(f"key named '{name}' already exists")
    keys.append({"name": name, "key": key, "description": description})
    _save_keys(keys)
    return {"name": name, "key": key}


def delete_key(name: str):
    keys = _load_keys()
    new_keys = [k for k in keys if k.get("name") != name]
    if len(new_keys) == len(keys):
        raise ValueError(f"key named '{name}' not found")
    _save_keys(new_keys)


def get_all_key_values():
    """Return all valid key strings (from both named store and config) for auth."""
    keys = []
    # Named keys from api_keys.json
    for k in _load_keys():
        v = k.get("key", "")
        if v:
            keys.append(v)
    # Config-based keys
    value = CONFIG.get("api_keys") or CONFIG.get("api_key")
    if isinstance(value, str):
        keys.extend([k.strip() for k in value.split(",") if k.strip()])
    elif isinstance(value, list):
        keys.extend([str(k).strip() for k in value if str(k).strip()])
    return keys


# ─── Admin API handler ───────────────────────────────────────────────────────

class AdminAPI:
    """Handles /api/admin/* requests. Called by server.py."""

    def __init__(self, handler):
        self.handler = handler
        self.path = handler.path.split("?")[0]

    def _send_json(self, data, status=200):
        self.handler.send_json(data, status)

    def _parse_body(self):
        length = int(self.handler.headers.get("Content-Length", 0))
        body = self.handler.rfile.read(length) if length else b""
        try:
            return json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            return None

    def _get_session_token(self):
        """Extract session token from cookie or Authorization header."""
        cookie_header = self.handler.headers.get("Cookie", "")
        if cookie_header:
            cookie = SimpleCookie()
            try:
                cookie.load(cookie_header)
                if "admin_token" in cookie:
                    return cookie["admin_token"].value
            except Exception:
                pass
        auth = self.handler.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return None

    def _require_auth(self):
        """Check admin session. Returns True if authenticated."""
        token = self._get_session_token()
        return admin_session.validate_session(token) if token else False

    def _set_session_cookie(self, token):
        secure = self.handler.headers.get("X-Forwarded-Proto", "") == "https"
        self.handler.send_header("Set-Cookie", f"admin_token={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400{'; Secure' if secure else ''}")

    def _same_origin(self):
        """Check Origin/Referer for CSRF protection on mutating requests."""
        origin = self.handler.headers.get("Origin", "")
        referer = self.handler.headers.get("Referer", "")
        host = self.handler.headers.get("Host", "")
        if not origin and not referer:
            return True  # Non-browser clients (curl)
        for url in (origin, referer):
            if not url:
                continue
            if f"//{host}" in url:
                return True
        return False

    def handle(self):
        """Main dispatch: route the request to the appropriate handler."""
        path = self.path

        # Auth-free endpoints
        if path == "/api/admin/check-auth":
            return self._check_auth()
        if path == "/api/admin/login":
            return self._login()
        if path == "/api/admin/logout":
            return self._logout()

        # All other endpoints require auth
        if not self._require_auth():
            self._send_json({"error": {"message": "unauthorized"}}, 401)
            return

        # Mutating endpoints need same-origin check
        method = self.handler.command
        if method in ("POST", "PUT", "DELETE") and not self._same_origin():
            self._send_json({"error": {"message": "cross-origin request blocked"}}, 403)
            return

        # Route
        if path == "/api/admin/settings" and method == "GET":
            return self._get_settings()
        elif path == "/api/admin/settings" and method == "PUT":
            return self._put_settings()
        elif path == "/api/admin/stats" and method == "GET":
            return self._get_stats()
        elif path == "/api/admin/stats/reset" and method == "POST":
            return self._reset_stats()
        elif path == "/api/admin/history" and method == "GET":
            return self._get_history()
        elif path == "/api/admin/keys" and method == "GET":
            return self._get_keys()
        elif path == "/api/admin/keys" and method == "POST":
            return self._add_key()
        elif path.startswith("/api/admin/keys/") and method == "DELETE":
            return self._delete_key(path.split("/api/admin/keys/")[-1])
        elif path == "/api/admin/models" and method == "GET":
            return self._get_models()
        elif path == "/api/admin/models" and method == "PUT":
            return self._put_models()
        else:
            self._send_json({"error": {"message": "not found"}}, 404)

    # ─── Auth ─────────────────────────────────────────────────────────────────

    def _check_auth(self):
        token = self._get_session_token()
        authed = bool(token and admin_session.validate_session(token))
        self._send_json({"authenticated": authed})

    def _login(self):
        body = self._parse_body()
        if body is None:
            return self._send_json({"error": {"message": "invalid JSON"}}, 400)
        password = (body.get("password") or "").strip()
        if not admin_session.verify_password(password):
            return self._send_json({"error": {"message": "wrong password"}}, 401)
        token = admin_session.create_session()
        self.handler.send_response(200)
        self.handler.send_header("Content-Type", "application/json")
        self._set_session_cookie(token)
        self.handler.send_header("Content-Length", "2")
        self.handler.end_headers()
        self.handler.wfile.write(b"{}")

    def _logout(self):
        token = self._get_session_token()
        if token:
            admin_session.destroy_session(token)
        self.handler.send_response(200)
        self.handler.send_header("Content-Type", "application/json")
        self.handler.send_header("Set-Cookie", "admin_token=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        self.handler.send_header("Content-Length", "2")
        self.handler.end_headers()
        self.handler.wfile.write(b"{}")

    # ─── Settings ──────────────────────────────────────────────────────────────

    def _get_settings(self):
        result = {}
        for field in SETTINGS_FIELDS:
            result[field] = CONFIG.get(field)
        self._send_json(result)

    def _put_settings(self):
        body = self._parse_body()
        if body is None:
            return self._send_json({"error": {"message": "invalid JSON"}}, 400)
        changed = []
        for field in SETTINGS_FIELDS:
            if field in body:
                CONFIG[field] = body[field]
                changed.append(field)
        if changed:
            save_config()
        self._send_json({"ok": True, "changed": changed})

    # ─── Stats ─────────────────────────────────────────────────────────────────

    def _get_stats(self):
        with _stats_lock:
            data = {
                "uptime": int(time.time() - _stats["start_time"]),
                "total_requests": _stats["total_requests"],
                "success_count": _stats["success_count"],
                "error_count": _stats["error_count"],
                "version": __version__,
            }
        data["key_count"] = len(list_keys())
        from .models import MODELS
        data["model_count"] = len(MODELS)
        self._send_json(data)

    def _reset_stats(self):
        reset_stats()
        self._send_json({"ok": True})

    def _get_history(self):
        with _stats_lock:
            data = list(_stats["history"])
        self._send_json(data)

    # ─── Keys ──────────────────────────────────────────────────────────────────

    def _get_keys(self):
        self._send_json({"keys": list_keys()})

    def _add_key(self):
        body = self._parse_body()
        if body is None:
            return self._send_json({"error": {"message": "invalid JSON"}}, 400)
        try:
            name = (body.get("name") or "").strip()
            key = (body.get("key") or "").strip()
            desc = (body.get("description") or "").strip()
            result = add_key(name, key, desc)
            self._send_json(result)
        except ValueError as e:
            self._send_json({"error": {"message": str(e)}}, 400)

    def _delete_key(self, name):
        from urllib.parse import unquote
        name = unquote(name)
        try:
            delete_key(name)
            self._send_json({"ok": True})
        except ValueError as e:
            self._send_json({"error": {"message": str(e)}}, 404)

    # ─── Models ────────────────────────────────────────────────────────────────

    def _get_models(self):
        from .models import MODELS
        models = list(MODELS.keys())
        self._send_json({"models": models, "alias_map": {}})

    def _put_models(self):
        # Models are currently static; accept but note limitation
        self._send_json({"ok": True, "note": "models are built-in, custom model mapping not yet supported"})
