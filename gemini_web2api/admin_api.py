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
        reader = getattr(self.handler, "_read_request_body", None)
        if callable(reader):
            body = reader()
        else:
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
        # Nodes
        elif path == "/api/admin/nodes" and method == "GET":
            return self._get_nodes()
        elif path == "/api/admin/nodes" and method == "DELETE":
            return self._delete_node()
        elif path == "/api/admin/nodes/test" and method == "POST":
            return self._test_node()
        elif path == "/api/admin/nodes/enable" and method == "POST":
            return self._enable_node()
        elif path == "/api/admin/nodes/disable" and method == "POST":
            return self._disable_node()
        elif path == "/api/admin/nodes/import" and method == "POST":
            return self._import_nodes()
        elif path == "/api/admin/nodes/batch-enable" and method == "POST":
            return self._batch_enable()
        elif path == "/api/admin/nodes/batch-disable" and method == "POST":
            return self._batch_disable()
        elif path == "/api/admin/nodes/batch-delete" and method == "POST":
            return self._batch_delete()
        elif path == "/api/admin/nodes/dedup" and method == "POST":
            return self._dedup_nodes()
        elif path == "/api/admin/nodes/disabled" and method == "DELETE":
            return self._delete_disabled()
        # Subscriptions
        elif path == "/api/admin/subscriptions" and method == "GET":
            return self._get_subscriptions()
        elif path == "/api/admin/subscriptions" and method == "POST":
            return self._add_subscription()
        elif path == "/api/admin/subscriptions" and method == "PUT":
            return self._update_subscription()
        elif path == "/api/admin/subscriptions" and method == "DELETE":
            return self._delete_subscription()
        elif path == "/api/admin/subscriptions/fetch" and method == "POST":
            return self._fetch_subscription()
        elif path == "/api/admin/subscriptions/refresh" and method == "POST":
            return self._refresh_subscription()
        elif path == "/api/admin/subscriptions/refresh-all" and method == "POST":
            return self._refresh_all_subscriptions()
        # Mihomo
        elif path == "/api/admin/mihomo/status" and method == "GET":
            return self._mihomo_status()
        elif path == "/api/admin/mihomo/start" and method == "POST":
            return self._mihomo_start()
        elif path == "/api/admin/mihomo/stop" and method == "POST":
            return self._mihomo_stop()
        elif path == "/api/admin/mihomo/switch" and method == "POST":
            return self._mihomo_switch()
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

    # ─── Nodes ──────────────────────────────────────────────────────────────────

    def _get_nodes(self):
        from . import nodes
        node_list = nodes.list_nodes()
        stats = nodes.get_stats()
        self._send_json({"nodes": node_list, "stats": stats})

    def _delete_node(self):
        from . import nodes, mihomo
        body = self._parse_body()
        if body is None:
            return self._send_json({"error": {"message": "invalid JSON"}}, 400)
        uri = body.get("raw_uri", "")
        if not uri:
            return self._send_json({"error": {"message": "raw_uri required"}}, 400)
        ok = nodes.delete_node(uri)
        if ok:
            mihomo.stop_worker(uri, clear_bad=True)
        self._send_json({"ok": ok})

    def _test_node(self):
        from . import nodes
        body = self._parse_body()
        if body is None:
            return self._send_json({"error": {"message": "invalid JSON"}}, 400)
        uri = body.get("raw_uri", "")
        timeout = body.get("timeout_seconds", 10)
        if not uri:
            return self._send_json({"error": {"message": "raw_uri required"}}, 400)
        ok, latency, err = nodes.test_node(uri, timeout)
        self._send_json({"success": ok, "latency_ms": latency, "error": err})

    def _enable_node(self):
        from . import nodes, mihomo
        body = self._parse_body()
        if body is None:
            return self._send_json({"error": {"message": "invalid JSON"}}, 400)
        uri = body.get("raw_uri", "")
        ok = nodes.set_disabled(uri, False)
        if ok:
            mihomo.clear_bad(uri)
        self._send_json({"ok": ok})

    def _disable_node(self):
        from . import nodes, mihomo
        body = self._parse_body()
        if body is None:
            return self._send_json({"error": {"message": "invalid JSON"}}, 400)
        uri = body.get("raw_uri", "")
        ok = nodes.set_disabled(uri, True)
        if ok:
            mihomo.stop_worker(uri)
        self._send_json({"ok": ok})

    def _import_nodes(self):
        from . import nodes, node_import
        body = self._parse_body()
        if body is None:
            return self._send_json({"error": {"message": "invalid JSON"}}, 400)
        text = body.get("text", "")
        if not text.strip():
            return self._send_json({"error": {"message": "text required"}}, 400)
        try:
            parsed = node_import.parse_subscription_text(text)
            added, skipped = nodes.merge_nodes(parsed)
            self._send_json({"added": added, "skipped": skipped})
        except Exception as e:
            self._send_json({"error": {"message": str(e)}}, 400)

    def _batch_enable(self):
        from . import nodes, mihomo
        body = self._parse_body()
        if body is None:
            return self._send_json({"error": {"message": "invalid JSON"}}, 400)
        uris = body.get("uris", [])
        changed = nodes.batch_set_disabled(uris, False)
        if changed:
            for uri in uris:
                mihomo.clear_bad(uri)
        self._send_json({"changed": changed})

    def _batch_disable(self):
        from . import nodes, mihomo
        body = self._parse_body()
        if body is None:
            return self._send_json({"error": {"message": "invalid JSON"}}, 400)
        uris = body.get("uris", [])
        changed = nodes.batch_set_disabled(uris, True)
        if changed:
            for uri in uris:
                mihomo.stop_worker(uri)
        self._send_json({"changed": changed})

    def _batch_delete(self):
        from . import nodes, mihomo
        body = self._parse_body()
        if body is None:
            return self._send_json({"error": {"message": "invalid JSON"}}, 400)
        uris = body.get("uris", [])
        removed = nodes.batch_delete(uris)
        if removed:
            for uri in uris:
                mihomo.stop_worker(uri, clear_bad=True)
        self._send_json({"removed": removed})

    def _dedup_nodes(self):
        from . import nodes
        removed = nodes.dedup_nodes()
        self._send_json({"removed": removed})

    def _delete_disabled(self):
        from . import nodes, mihomo
        disabled_uris = [n.get("raw_uri", "") for n in nodes.list_nodes() if n.get("disabled")]
        removed = nodes.delete_disabled()
        if removed:
            for uri in disabled_uris:
                if uri:
                    mihomo.stop_worker(uri, clear_bad=True)
        self._send_json({"removed": removed})

    # ─── Subscriptions ───────────────────────────────────────────────────────

    def _get_subscriptions(self):
        from . import subscriptions
        subs = subscriptions.list_subscriptions()
        self._send_json({"subscriptions": subs})

    def _add_subscription(self):
        from . import subscriptions
        body = self._parse_body()
        if body is None:
            return self._send_json({"error": {"message": "invalid JSON"}}, 400)
        url = (body.get("url") or "").strip()
        if not url:
            return self._send_json({"error": {"message": "url required"}}, 400)
        source = subscriptions.upsert_subscription({
            "url": url,
            "name": body.get("name", ""),
            "auto_refresh": body.get("auto_refresh", False),
            "refresh_interval_minutes": body.get("refresh_interval_minutes", 360),
        })
        self._send_json(source)

    def _update_subscription(self):
        from . import subscriptions
        body = self._parse_body()
        if body is None:
            return self._send_json({"error": {"message": "invalid JSON"}}, 400)
        sid = body.get("id") or body.get("source_id") or ""
        if not sid:
            return self._send_json({"error": {"message": "id required"}}, 400)
        fields = {k: v for k, v in body.items() if k not in ("id", "source_id")}
        result = subscriptions.update_subscription(sid, fields)
        if result:
            self._send_json(result)
        else:
            self._send_json({"error": {"message": "not found"}}, 404)

    def _delete_subscription(self):
        from . import subscriptions
        body = self._parse_body()
        if body is None:
            return self._send_json({"error": {"message": "invalid JSON"}}, 400)
        sid = body.get("id") or ""
        delete_nodes = bool(body.get("delete_nodes", False))
        ok = subscriptions.delete_subscription(sid, delete_nodes)
        self._send_json({"ok": ok})

    def _fetch_subscription(self):
        from . import subscriptions
        body = self._parse_body()
        if body is None:
            return self._send_json({"error": {"message": "invalid JSON"}}, 400)
        url = (body.get("url") or "").strip()
        if not url:
            return self._send_json({"error": {"message": "url required"}}, 400)
        source, result, error = subscriptions.fetch_and_save(
            url,
            name=body.get("name", ""),
            auto_refresh=body.get("auto_refresh", False),
            refresh_interval_minutes=body.get("refresh_interval_minutes", 360),
            adopt_existing=body.get("adopt_existing", True),
        )
        if error:
            self._send_json({"error": {"message": error}}, 400)
        else:
            self._send_json({"source_id": source["id"], "result": result})

    def _refresh_subscription(self):
        from . import subscriptions
        body = self._parse_body()
        if body is None:
            return self._send_json({"error": {"message": "invalid JSON"}}, 400)
        sid = body.get("id") or ""
        source, result, error = subscriptions.refresh_source(sid, body.get("adopt_existing", True))
        if error:
            self._send_json({"error": {"message": error}}, 400)
        else:
            self._send_json({"source_id": source["id"], "result": result})

    def _refresh_all_subscriptions(self):
        from . import subscriptions
        results = subscriptions.refresh_all(True)
        self._send_json({"results": results})

    # ─── Mihomo ────────────────────────────────────────────────────────────────

    def _mihomo_status(self):
        from . import mihomo
        self._send_json({
            "available": mihomo.is_available(),
            "running": mihomo.is_running(),
            "local_proxy": mihomo.get_local_proxy(),
            "worker_count": mihomo.worker_count(),
            "active_raw_uri": mihomo.get_active_raw_uri(),
            "manual_active_raw_uri": mihomo.get_manual_active_raw_uri(),
            "last_used_raw_uri": mihomo.get_last_used_raw_uri(),
            "bad_count": mihomo.bad_count(),
        })

    def _mihomo_start(self):
        from . import mihomo
        ok, msg = mihomo.start()
        self._send_json({"ok": ok, "message": msg})

    def _mihomo_stop(self):
        from . import mihomo
        mihomo.stop()
        self._send_json({"ok": True})

    def _mihomo_switch(self):
        from . import mihomo
        body = self._parse_body()
        if body is None:
            return self._send_json({"error": {"message": "invalid JSON"}}, 400)
        uri = body.get("raw_uri", "")
        ok, msg = mihomo.switch_proxy(uri)
        self._send_json({"ok": ok, "message": msg})
