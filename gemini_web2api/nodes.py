"""Node store: persistent JSON storage for proxy nodes with thread-safe CRUD."""
import json
import os
import random
import tempfile
import threading
import time
import secrets
from typing import Optional

from .config import get_data_dir, CONFIG

_lock = threading.RLock()
_node_list = []
_health_map = {}  # raw_uri -> health dict
_loaded = False


def _nodes_file():
    return os.path.join(get_data_dir(), "nodes.json")


def _health_file():
    return os.path.join(get_data_dir(), "node_health.json")


def _atomic_write(path, data):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _ensure_loaded():
    """Load from disk if not yet loaded, or recover if in-memory is empty but disk has data."""
    global _loaded, _node_list, _health_map
    with _lock:
        if _loaded and _node_list:
            return
        # Try loading from disk
        if os.path.exists(_nodes_file()):
            try:
                with open(_nodes_file()) as f:
                    _node_list = json.load(f)
            except Exception:
                _node_list = []
        if os.path.exists(_health_file()):
            try:
                with open(_health_file()) as f:
                    _health_map = json.load(f)
            except Exception:
                _health_map = {}
        _loaded = True


def _save_nodes():
    _atomic_write(_nodes_file(), _node_list)


def _save_health():
    _atomic_write(_health_file(), _health_map)


# ─── Public API ──────────────────────────────────────────────────────────────

def list_nodes():
    """Return all nodes with health data merged in."""
    _ensure_loaded()
    with _lock:
        result = []
        for n in _node_list:
            node = dict(n)
            h = _health_map.get(n.get("raw_uri", ""), {})
            node["health"] = h
            result.append(node)
        return result


def get_node(raw_uri):
    """Return one node with health data merged in, or None."""
    _ensure_loaded()
    with _lock:
        for n in _node_list:
            if n.get("raw_uri") == raw_uri:
                node = dict(n)
                node["health"] = _health_map.get(raw_uri, {})
                return node
        return None


def get_stats():
    """Return aggregate node statistics."""
    _ensure_loaded()
    with _lock:
        total = len(_node_list)
        enabled = sum(1 for n in _node_list if not n.get("disabled", False))
        disabled = total - enabled
        now = time.time()
        cooling = 0
        probation = 0
        for n in _node_list:
            if n.get("disabled"):
                continue
            h = _health_map.get(n.get("raw_uri", ""), {})
            cooldown = h.get("cooldown_until", 0)
            fails = h.get("consecutive_failures", 0)
            if cooldown > now:
                cooling += 1
            elif fails > 0:
                probation += 1
        available = enabled - cooling - probation
        return {
            "total": total,
            "enabled": enabled,
            "disabled": disabled,
            "cooling": cooling,
            "probation": probation,
            "available": available,
        }


def merge_nodes(new_nodes, source_id=""):
    """Add new nodes, skipping exact RawURI duplicates. Returns (added, skipped)."""
    _ensure_loaded()
    with _lock:
        existing = {n.get("raw_uri", "") for n in _node_list}
        added = 0
        skipped = 0
        for n in new_nodes:
            uri = n.get("raw_uri", "")
            if not uri or uri in existing:
                skipped += 1
                continue
            if source_id:
                n["source_id"] = source_id
            _node_list.append(n)
            existing.add(uri)
            added += 1
        if added:
            _save_nodes()
        return added, skipped


def delete_node(raw_uri):
    _ensure_loaded()
    with _lock:
        before = len(_node_list)
        _node_list[:] = [n for n in _node_list if n.get("raw_uri") != raw_uri]
        if len(_node_list) < before:
            _health_map.pop(raw_uri, None)
            _save_nodes()
            _save_health()
            return True
        return False


def batch_delete(raw_uris):
    _ensure_loaded()
    with _lock:
        targets = set(raw_uris)
        before = len(_node_list)
        _node_list[:] = [n for n in _node_list if n.get("raw_uri") not in targets]
        for uri in raw_uris:
            _health_map.pop(uri, None)
        if len(_node_list) < before:
            _save_nodes()
            _save_health()
        return before - len(_node_list)


def set_disabled(raw_uri, disabled):
    _ensure_loaded()
    with _lock:
        for n in _node_list:
            if n.get("raw_uri") == raw_uri:
                n["disabled"] = disabled
                if not disabled:
                    h = _health_map.get(raw_uri)
                    if h:
                        h["cooldown_until"] = 0
                        h["consecutive_failures"] = 0
                        h["last_test_error"] = ""
                        _save_health()
                _save_nodes()
                return True
        return False


def batch_set_disabled(raw_uris, disabled):
    _ensure_loaded()
    with _lock:
        targets = set(raw_uris)
        changed = 0
        for n in _node_list:
            if n.get("raw_uri") in targets:
                n["disabled"] = disabled
                changed += 1
        if changed:
            _save_nodes()
        return changed


def dedup_nodes():
    """Remove exact RawURI duplicates. Returns count removed."""
    _ensure_loaded()
    with _lock:
        seen = set()
        kept = []
        removed = 0
        for n in _node_list:
            uri = n.get("raw_uri", "")
            if uri in seen:
                removed += 1
                continue
            seen.add(uri)
            kept.append(n)
        if removed:
            _node_list[:] = kept
            _save_nodes()
        return removed


def delete_disabled():
    """Delete all disabled nodes. Returns count removed."""
    _ensure_loaded()
    with _lock:
        before = len(_node_list)
        removed_uris = [n.get("raw_uri", "") for n in _node_list if n.get("disabled")]
        _node_list[:] = [n for n in _node_list if not n.get("disabled")]
        for uri in removed_uris:
            _health_map.pop(uri, None)
        if len(_node_list) < before:
            _save_nodes()
            _save_health()
        return before - len(_node_list)


def get_enabled_clash_proxies():
    """Return list of (raw_uri, clash_config) for enabled, non-cooling nodes.
    Used by mihomo config generation."""
    _ensure_loaded()
    with _lock:
        now = time.time()
        result = []
        for n in _node_list:
            if n.get("disabled"):
                continue
            h = _health_map.get(n.get("raw_uri", ""), {})
            if h.get("cooldown_until", 0) > now:
                continue
            clash = n.get("clash_config")
            if clash:
                result.append((n.get("raw_uri", ""), clash))
        return result


def _node_top_k():
    raw = os.environ.get("GEMINI_WEB2API_NODE_TOP_K") or CONFIG.get("node_top_k") or 80
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 80


def _health_score(h, now=None):
    """Score a node using vertex-like health/exploration heuristics."""
    now = now or time.time()
    score = 100.0

    success_count = int(h.get("success_count", 0) or 0)
    fail_count = int(h.get("fail_count", 0) or 0)
    consecutive = int(h.get("consecutive_failures", 0) or 0)
    latency = float(h.get("last_test_latency", 0) or 0)
    last_success = float(h.get("last_success_at", 0) or 0)
    last_fail = float(h.get("last_fail_at", 0) or 0)
    last_test = float(h.get("last_test_at", 0) or 0)

    score += min(success_count, 100) * 3

    fail_decay = 1.0
    effective_consecutive = consecutive
    if last_fail > 0:
        age = now - last_fail
        if age > 6 * 3600:
            fail_decay = 0.15
            effective_consecutive = 0
        elif age > 3600:
            fail_decay = 0.5
    score -= min(fail_count, 100) * 4 * fail_decay
    score -= effective_consecutive * 25

    if latency > 0:
        score -= min(latency / 1000.0, 30)

    last_seen = max(last_success, last_fail, last_test)
    if last_seen <= 0:
        score += 30
    else:
        idle = now - last_seen
        if idle > 6 * 3600:
            score += 35
        elif idle > 3600:
            score += 15

    return max(1.0, score)


def _weighted_without_replacement(scored, limit):
    """Return up to limit nodes from [(score, node)] using weighted random selection."""
    if limit <= 0 or not scored:
        return []
    top_k = _node_top_k()
    pool = sorted(scored, key=lambda item: item[0], reverse=True)[:top_k]
    result = []
    while pool and len(result) < limit:
        total = sum(max(1.0, item[0]) for item in pool)
        pick = random.uniform(0, total)
        upto = 0.0
        selected_idx = len(pool) - 1
        for i, (score, _node) in enumerate(pool):
            upto += max(1.0, score)
            if upto >= pick:
                selected_idx = i
                break
        _score, node = pool.pop(selected_idx)
        result.append(node)
    return result


def select_available_clash_nodes(limit=8, exclude=None):
    """Return enabled Clash nodes using health-aware weighted selection.

    Non-cooling nodes are selected first. Cooling nodes are only used as a
    fallback when there are not enough non-cooling candidates to satisfy limit.
    This hot path performs no disk writes.
    """
    _ensure_loaded()
    exclude = set(exclude or [])
    with _lock:
        now = time.time()
        primary = []
        cooling = []
        for n in _node_list:
            raw_uri = n.get("raw_uri", "")
            if not raw_uri or raw_uri in exclude:
                continue
            if n.get("disabled"):
                continue
            clash = n.get("clash_config")
            if not clash:
                continue
            h = _health_map.get(raw_uri, {})
            node = dict(n)
            node["health"] = h
            scored = (_health_score(h, now), node)
            if h.get("cooldown_until", 0) > now:
                cooling.append(scored)
            else:
                primary.append(scored)

        selected = _weighted_without_replacement(primary, limit)
        if len(selected) < limit:
            selected.extend(_weighted_without_replacement(cooling, limit - len(selected)))
        return selected


def count_available_clash_nodes(include_cooling=False):
    """Count enabled Clash-capable nodes, optionally including cooling nodes."""
    _ensure_loaded()
    with _lock:
        now = time.time()
        count = 0
        for n in _node_list:
            if n.get("disabled") or not n.get("clash_config"):
                continue
            h = _health_map.get(n.get("raw_uri", ""), {})
            if not include_cooling and h.get("cooldown_until", 0) > now:
                continue
            count += 1
        return count


def has_enabled_clash_nodes():
    """Return True if the user has any enabled Clash-capable node configured."""
    _ensure_loaded()
    with _lock:
        return any((not n.get("disabled")) and n.get("clash_config") for n in _node_list)


def record_health(raw_uri, success, error_type="", latency_ms=0, error_msg=""):
    """Record a health event for a node. Updates cooldown/score based on error type."""
    _ensure_loaded()
    with _lock:
        if not success and error_type in ("client_canceled", "race_loser", "context_canceled",
                                         "invalid_request", "safety"):
            return
        h = _health_map.setdefault(raw_uri, {
            "success_count": 0,
            "fail_count": 0,
            "consecutive_failures": 0,
            "cooldown_until": 0,
            "last_test_error": "",
            "last_test_latency": 0,
            "last_test_at": 0,
            "last_success_at": 0,
            "last_fail_at": 0,
            "last_error_type": "",
        })

        now = time.time()

        if success:
            h["last_test_at"] = int(now)
            h["last_success_at"] = int(now)
            h["success_count"] += 1
            h["consecutive_failures"] = 0
            h["cooldown_until"] = 0
            h["last_test_error"] = ""
            h["last_error_type"] = ""
            if latency_ms > 0:
                h["last_test_latency"] = latency_ms
        else:
            h["last_test_at"] = int(now)
            h["last_fail_at"] = int(now)
            if latency_ms > 0:
                h["last_test_latency"] = latency_ms
            if error_type == "ratelimit":
                # Soft cooldown: do not count as a hard node failure.
                h["last_test_error"] = error_msg or error_type
                h["last_error_type"] = error_type
                cf = h.get("consecutive_failures", 0) or 0
                h["cooldown_until"] = int(now + min(30 + cf * 30, 180))
                _save_health()
                return
            h["fail_count"] += 1
            h["consecutive_failures"] += 1
            h["last_test_error"] = error_msg or error_type
            h["last_error_type"] = error_type
            # Graded cooldown based on error type and consecutive failures
            cf = h["consecutive_failures"]
            if error_type == "invalid_config":
                h["cooldown_until"] = int(now + 3600)
            elif error_type == "proxy_start":
                h["cooldown_until"] = int(now + 300)
            elif error_type in ("timeout", "tls", "unknown"):
                ladder = [30, 120, 300, 900, 1800]
                h["cooldown_until"] = int(now + ladder[min(cf - 1, len(ladder) - 1)])
            elif error_type in ("connection_refused", "network_unreachable"):
                ladder = [120, 300, 900, 3600]
                h["cooldown_until"] = int(now + ladder[min(cf - 1, len(ladder) - 1)])
            elif error_type in ("recaptcha", "auth"):
                ladder = [15, 30, 60, 300, 900]
                h["cooldown_until"] = int(now + ladder[min(cf - 1, len(ladder) - 1)])
            else:
                h["cooldown_until"] = int(now + 60)

        _save_health()


def test_node(raw_uri, timeout=10):
    """Test a node by dialing through mihomo. Returns (success, latency_ms, error)."""
    try:
        from . import mihomo
        ok, latency, err = mihomo.test_node_delay(raw_uri, timeout)
        if ok:
            record_health(raw_uri, True, latency_ms=latency)
            return True, latency, ""
        else:
            msg = err or "delay test failed"
            error_type = "invalid_config" if "config" in msg.lower() else "timeout"
            record_health(raw_uri, False, error_type=error_type, error_msg=msg)
            return False, 0, msg
    except Exception as e:
        record_health(raw_uri, False, error_type="unknown", error_msg=str(e))
        return False, 0, str(e)


def replace_nodes_for_source(source_id, parsed_nodes):
    """Replace all nodes belonging to a subscription source.
    Returns dict with added/updated/removed/skipped/adopted counts."""
    _ensure_loaded()
    with _lock:
        # Build incoming identity map (by raw_uri)
        incoming = {}
        for n in parsed_nodes:
            uri = n.get("raw_uri", "")
            if uri:
                incoming[uri] = n

        kept = []
        removed_uris = []
        result = {"added": 0, "updated": 0, "removed": 0, "skipped": 0, "adopted": 0}

        # Track which incoming nodes were matched
        matched = set()

        for n in _node_list:
            uri = n.get("raw_uri", "")
            if n.get("source_id") == source_id:
                if uri in incoming:
                    # Update in place
                    updated = incoming[uri]
                    updated["source_id"] = source_id
                    updated["disabled"] = n.get("disabled", False)
                    kept.append(updated)
                    matched.add(uri)
                    result["updated"] += 1
                else:
                    # Remove (not in upstream anymore)
                    removed_uris.append(uri)
                    result["removed"] += 1
            elif not n.get("source_id") and uri in incoming:
                # Adopt orphan node
                adopted = incoming[uri]
                adopted["source_id"] = source_id
                adopted["disabled"] = n.get("disabled", False)
                kept.append(adopted)
                matched.add(uri)
                result["adopted"] += 1
            else:
                kept.append(n)

        # Add new nodes not matched
        for uri, n in incoming.items():
            if uri not in matched:
                n["source_id"] = source_id
                kept.append(n)
                result["added"] += 1

        _node_list[:] = kept
        _save_nodes()

        # Clean health for removed URIs (but only if they're truly gone)
        all_uris = {n.get("raw_uri", "") for n in _node_list}
        for uri in removed_uris:
            if uri not in all_uris:
                _health_map.pop(uri, None)
        _save_health()

        return result


def delete_nodes_for_source(source_id):
    """Delete all nodes belonging to a subscription source. Returns count."""
    _ensure_loaded()
    with _lock:
        before = len(_node_list)
        removed_uris = [n.get("raw_uri", "") for n in _node_list if n.get("source_id") == source_id]
        _node_list[:] = [n for n in _node_list if n.get("source_id") != source_id]
        for uri in removed_uris:
            _health_map.pop(uri, None)
        if len(_node_list) < before:
            _save_nodes()
            _save_health()
        return before - len(_node_list)
