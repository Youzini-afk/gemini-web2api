"""Subscription source management: persist URLs, fetch+refresh, auto-refresh."""
import json
import os
import tempfile
import threading
import time
import secrets

from .config import get_data_dir
from . import nodes, node_import

_lock = threading.RLock()
_subscriptions = []
_loaded = False
_refresh_lock = threading.Lock()  # Global: no concurrent refreshes


def _file():
    return os.path.join(get_data_dir(), "subscriptions.json")


def _ensure_loaded():
    global _loaded, _subscriptions
    with _lock:
        if _loaded and _subscriptions:
            return
        path = _file()
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                # Accept multiple shapes
                if isinstance(data, list):
                    _subscriptions = data
                elif isinstance(data, dict):
                    _subscriptions = data.get("subscriptions") or data.get("sources") or data.get("items") or []
                    if isinstance(_subscriptions, dict):
                        # id -> source map
                        _subscriptions = [{"id": k, **v} for k, v in _subscriptions.items()]
                else:
                    _subscriptions = []
            except Exception:
                _subscriptions = []
        else:
            _subscriptions = []
        _loaded = True


def _save():
    d = os.path.dirname(_file())
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(_subscriptions, f, indent=2, ensure_ascii=False)
    os.replace(tmp, _file())


def list_subscriptions():
    _ensure_loaded()
    with _lock:
        return list(_subscriptions)


def upsert_subscription(source):
    """Create or update a subscription source. Returns the source with id."""
    _ensure_loaded()
    with _lock:
        sid = source.get("id", "").strip()
        if not sid:
            sid = "sub_" + secrets.token_hex(6)
            source["id"] = sid

        # Check if exists
        found = False
        for i, s in enumerate(_subscriptions):
            if s.get("id") == sid:
                _subscriptions[i] = source
                found = True
                break
        if not found:
            source.setdefault("created_at", int(time.time()))
            _subscriptions.append(source)

        source.setdefault("last_fetched_at", 0)
        source.setdefault("last_status", "pending")
        source.setdefault("last_node_count", 0)
        source.setdefault("auto_refresh", False)
        source.setdefault("refresh_interval_minutes", 360)

        _save()
        return source


def update_subscription(source_id, fields):
    """Update specific fields of a subscription source."""
    _ensure_loaded()
    with _lock:
        for i, s in enumerate(_subscriptions):
            if s.get("id") == source_id:
                for k, v in fields.items():
                    if k != "id":
                        s[k] = v
                _subscriptions[i] = s
                _save()
                return s
        return None


def delete_subscription(source_id, delete_nodes=False):
    """Delete a subscription source. Optionally delete its nodes."""
    _ensure_loaded()
    with _lock:
        before = len(_subscriptions)
        _subscriptions[:] = [s for s in _subscriptions if s.get("id") != source_id]
        if len(_subscriptions) < before:
            _save()
    if delete_nodes:
        nodes.delete_nodes_for_source(source_id)
    return len(_subscriptions) < before


def update_fetch_status(source_id, status, error, result):
    """Update the fetch status of a subscription after refresh."""
    _ensure_loaded()
    with _lock:
        for i, s in enumerate(_subscriptions):
            if s.get("id") == source_id:
                s["last_fetched_at"] = int(time.time())
                s["last_status"] = status
                s["last_error"] = error
                if status != "error":
                    s["last_node_count"] = result.get("node_count", 0)
                    s["last_added"] = result.get("added", 0)
                    s["last_updated"] = result.get("updated", 0)
                    s["last_removed"] = result.get("removed", 0)
                    s["last_skipped"] = result.get("skipped", 0)
                    s["last_adopted"] = result.get("adopted", 0)
                _subscriptions[i] = s
                _save()
                return True
        return False


def fetch_and_save(url, name="", auto_refresh=False, refresh_interval_minutes=360, adopt_existing=True):
    """Fetch a subscription URL, parse it, and save/replace nodes.

    Returns (source, result_dict, error).
    """
    if not _refresh_lock.acquire(blocking=False):
        return None, None, "another refresh is in progress"

    try:
        # Fetch content
        text = node_import.fetch_subscription(url)

        # Parse
        parsed = node_import.parse_subscription_text(text)
        if not parsed:
            return None, None, "no nodes parsed from subscription"

        # Create or update source
        source = upsert_subscription({
            "url": url,
            "name": name or url[:60],
            "auto_refresh": auto_refresh,
            "refresh_interval_minutes": refresh_interval_minutes,
        })

        # Replace nodes for this source
        result = nodes.replace_nodes_for_source(source["id"], parsed)
        result["node_count"] = result.get("added", 0) + result.get("updated", 0) + result.get("adopted", 0)

        update_fetch_status(source["id"], "success", "", result)
        return source, result, None
    except Exception as e:
        return None, None, str(e)
    finally:
        _refresh_lock.release()


def refresh_source(source_id, adopt_existing=True):
    """Refresh a specific subscription source."""
    _ensure_loaded()
    with _lock:
        source = next((s for s in _subscriptions if s.get("id") == source_id), None)
    if not source:
        return None, None, "subscription not found"
    return fetch_and_save(
        source.get("url", ""),
        source.get("name", ""),
        source.get("auto_refresh", False),
        source.get("refresh_interval_minutes", 360),
        adopt_existing,
    )


def refresh_all(adopt_existing=True):
    """Refresh all subscription sources. Returns per-source results."""
    _ensure_loaded()
    with _lock:
        sources = list(_subscriptions)

    results = []
    for s in sources:
        sid = s.get("id", "")
        source, result, error = refresh_source(sid, adopt_existing)
        results.append({
            "id": sid,
            "name": s.get("name", ""),
            "success": error is None,
            "error": error,
            "result": result,
        })
    return results
