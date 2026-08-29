"""Local Go tls-client helper transport for Gemini upstream requests.

The helper performs the browser-like TLS/proxy leg. Python only talks to the
helper over localhost HTTP, so proxied Gemini requests avoid Python's TLS stack.
"""
import base64
import codecs
import json
import os
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

from .config import CONFIG, get_data_dir

_lock = threading.RLock()
_process = None
_port = 0
_secret = ""
_process_path = ""


class BrowserTransportUnavailable(RuntimeError):
    """Raised when the local Go browser transport is missing or cannot start."""



def _truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _helper_path():
    candidates = [
        os.environ.get("GEMINI_WEB2API_TLS_HELPER", ""),
        CONFIG.get("tls_helper_path") or "",
        "/usr/local/bin/gemini-tls-helper",
    ]
    repo_local = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "tls_helper", "gemini-tls-helper"))
    candidates.append(repo_local)
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def available():
    return _helper_path() is not None


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _base_url():
    return f"http://127.0.0.1:{_port}"


def _health_ok(timeout=1):
    if not _port:
        return False
    try:
        with urllib.request.urlopen(_base_url() + "/health", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _ensure_started():
    global _process, _port, _secret, _process_path
    with _lock:
        path = _helper_path()
        if _process is not None and _process.poll() is None and path and path == _process_path and _health_ok(timeout=0.5):
            return
        if _process is not None and _process.poll() is None:
            try:
                _process.terminate()
            except Exception:
                pass
        if not path:
            raise BrowserTransportUnavailable(
                "Gemini TLS helper unavailable: set GEMINI_WEB2API_TLS_HELPER or install gemini-tls-helper"
            )
        _port = _free_port()
        _secret = secrets.token_urlsafe(24)
        log_path = os.path.join(get_data_dir(), "tls_helper.log")
        log_fd = open(log_path, "a", buffering=1)
        try:
            _process = subprocess.Popen(
                [path, "--port", str(_port), "--secret", _secret],
                stdout=log_fd,
                stderr=subprocess.STDOUT,
            )
            _process_path = path
        finally:
            try:
                log_fd.close()
            except Exception:
                pass
        deadline = time.time() + 5
        while time.time() < deadline:
            if _process.poll() is not None:
                raise BrowserTransportUnavailable(f"Gemini TLS helper exited during startup with code {_process.returncode}")
            if _health_ok(timeout=0.5):
                return
            time.sleep(0.1)
        raise BrowserTransportUnavailable("Gemini TLS helper did not become healthy")


def ensure_available():
    """Verify helper exists and can start; raises BrowserTransportUnavailable otherwise."""
    _ensure_started()


def _profile():
    return os.environ.get("GEMINI_WEB2API_TLS_PROFILE") or CONFIG.get("tls_profile") or "chrome_131"


def _headers_to_pairs(headers):
    return [[str(k), str(v)] for k, v in headers.items()]


def _payload(url, headers, body_bytes, proxy, timeout_sec, method="POST"):
    return json.dumps({
        "method": method,
        "url": url,
        "headers": _headers_to_pairs(headers),
        "body_base64": base64.b64encode(body_bytes).decode("ascii"),
        "proxy": proxy or "",
        "timeout_sec": int(min(max(timeout_sec or 180, 1), 300)),
        "profile": _profile(),
    }).encode("utf-8")


def _request(path, payload, timeout_sec):
    _ensure_started()
    req = urllib.request.Request(
        _base_url() + path,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_secret}",
        },
    )
    return urllib.request.urlopen(req, timeout=min((timeout_sec or 180) + 10, 310))


def post(url, headers, body_bytes, proxy, timeout_sec):
    payload = _payload(url, headers, body_bytes, proxy, timeout_sec)
    try:
        with _request("/request", payload, timeout_sec) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini TLS helper request failed ({e.code}): {detail}") from e
    status = int(data.get("status", 0) or 0)
    body = base64.b64decode(data.get("body_base64", ""))
    if status >= 400:
        msg = body.decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Gemini upstream returned HTTP {status}: {msg}")
    return body.decode("utf-8", errors="replace")


def get(url, headers, proxy, timeout_sec):
    """Fetch a page through the same browser-like transport used for requests."""
    payload = _payload(url, headers, b"", proxy, timeout_sec, method="GET")
    try:
        with _request("/request", payload, timeout_sec) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini TLS helper request failed ({e.code}): {detail}") from e
    status = int(data.get("status", 0) or 0)
    body = base64.b64decode(data.get("body_base64", ""))
    if status >= 400:
        msg = body.decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Gemini upstream returned HTTP {status}: {msg}")
    return body.decode("utf-8", errors="replace")


def stream(url, headers, body_bytes, proxy, timeout_sec):
    payload = _payload(url, headers, body_bytes, proxy, timeout_sec)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    try:
        with _request("/stream", payload, timeout_sec) as resp:
            while True:
                chunk = resp.read(16384)
                if not chunk:
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        yield tail
                    break
                text = decoder.decode(chunk, final=False)
                if text:
                    yield text
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini TLS helper stream failed ({e.code}): {detail}") from e


def allow_python_fallback():
    raw = os.environ.get("GEMINI_WEB2API_ALLOW_PYTHON_TRANSPORT_FALLBACK")
    if raw is None:
        raw = CONFIG.get("allow_python_transport_fallback", False)
    return _truthy(raw)
