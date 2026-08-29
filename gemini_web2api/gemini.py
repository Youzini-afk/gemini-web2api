"""Gemini StreamGenerate protocol implementation with httpx streaming."""
import json
import time
import uuid
import re
import urllib.request
import urllib.parse
import ssl
import os
import hashlib

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

from .config import CONFIG

_ssl_ctx = None
_cookie_cache = {"str": "", "sapisid": None, "mtime": 0}
_httpx_client = None
_httpx_client_proxy = None


def _node_attempts():
    raw = os.environ.get("GEMINI_WEB2API_NODE_ATTEMPTS") or CONFIG.get("node_attempts") or 16
    try:
        return max(1, min(int(raw), 64))
    except (TypeError, ValueError):
        return 16


def log(msg: str):
    if CONFIG["log_requests"]:
        import sys
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()


def _get_ssl_ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def _get_httpx_client():
    """Return an httpx client, rebuilding it when the effective proxy changes.

    This is critical: if the proxy changes (e.g. mihomo node pool starts/stops),
    the cached client must be rebuilt to pick up the new proxy. Without this,
    streaming requests would silently keep using the old proxy.
    """
    global _httpx_client, _httpx_client_proxy
    # Delegate to config.get_effective_proxy if available (supports mihomo),
    # otherwise fall back to CONFIG["proxy"].
    try:
        from .config import get_effective_proxy
        proxy = get_effective_proxy()
    except Exception:
        proxy = CONFIG.get("proxy")
    if _httpx_client is None and HAS_HTTPX:
        transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
        _httpx_client = httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True, trust_env=False)
        _httpx_client_proxy = proxy
    elif _httpx_client is not None and proxy != _httpx_client_proxy:
        # Proxy changed — rebuild client
        try:
            _httpx_client.close()
        except Exception:
            pass
        transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
        _httpx_client = httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True, trust_env=False)
        _httpx_client_proxy = proxy
    return _httpx_client


def _enabled_clash_candidates_exist() -> bool:
    try:
        from . import nodes
        return bool(nodes.has_enabled_clash_nodes())
    except Exception:
        return False


def _classify_proxy_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if "429" in msg or "resource exhausted" in msg or "rate limit" in msg or "ratelimit" in msg or "too many requests" in msg:
        return "ratelimit"
    if "recaptcha" in msg or "captcha" in msg or "token" in msg:
        return "recaptcha"
    if "auth" in msg or "permission" in msg or "401" in msg or "403" in msg or "forbidden" in msg or "unauth" in msg:
        return "auth"
    if "client canceled" in msg or "client cancelled" in msg or "request canceled" in msg or "request cancelled" in msg:
        return "client_canceled"
    if "context canceled" in msg or "context cancelled" in msg:
        return "context_canceled"
    if "safety" in msg or "content filter" in msg or "blocked" in msg or "prohibited" in msg:
        return "safety"
    if "invalid" in msg or "bad request" in msg or "unsupported" in msg or "invalid_argument" in msg:
        return "invalid_request"
    if "timed out" in msg or "timeout" in msg:
        return "timeout"
    if "ssl" in msg or "tls" in msg or "certificate" in msg or "eof" in msg or "curl: (35)" in msg or "boringssl" in msg or "http/2 stream" in msg:
        return "tls"
    if "refused" in msg:
        return "connection_refused"
    if "network" in msg or "unreachable" in msg:
        return "network_unreachable"
    return "unknown"


def _select_mihomo_proxy(failed_nodes, preferred_raw_uri=None):
    try:
        from . import mihomo
        return mihomo.get_proxy_for_request(
            preferred_raw_uri=preferred_raw_uri,
            exclude=failed_nodes,
            attempts=_node_attempts(),
        )
    except Exception as e:
        return None, None, str(e)


def select_request_proxy():
    """Select the proxy route used to prepare an image-bearing request.

    Returns ``(raw_uri, proxy_url)``.  A raw URI is present only when the
    custom Mihomo node pool selected the route; callers can pass it back to
    generate()/generate_stream() so upload and inference prefer the same node.
    """
    if not _enabled_clash_candidates_exist():
        return None, CONFIG.get("proxy")
    raw_uri, proxy, message = _select_mihomo_proxy(set())
    if not proxy:
        raise RuntimeError(f"Mihomo node pool unavailable: {message}")
    return raw_uri, proxy


def _record_node_health(raw_uri, success, latency_ms=0, error=None):
    if not raw_uri:
        return
    try:
        from . import browser_transport
        if isinstance(error, browser_transport.BrowserTransportUnavailable):
            return
        from . import nodes
        if success:
            nodes.record_health(raw_uri, True, latency_ms=latency_ms)
        else:
            err = error if isinstance(error, Exception) else Exception(str(error or "unknown"))
            nodes.record_health(raw_uri, False, error_type=_classify_proxy_error(err), error_msg=str(error))
    except Exception:
        pass


def _post_upstream(body, url, headers, proxy, ctx):
    """POST to Gemini upstream. Proxied requests use Go tls-client helper."""
    if proxy:
        from . import browser_transport
        if browser_transport.available():
            return browser_transport.post(url, headers, body, proxy, CONFIG["request_timeout_sec"])
        if not browser_transport.allow_python_fallback():
            raise browser_transport.BrowserTransportUnavailable(
                "Gemini TLS helper unavailable for proxied request; set GEMINI_WEB2API_TLS_HELPER "
                "or enable GEMINI_WEB2API_ALLOW_PYTHON_TRANSPORT_FALLBACK=1"
            )

    if proxy and HAS_HTTPX:
        transport = httpx.HTTPTransport(proxy=proxy)
        with httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True, trust_env=False) as client:
            resp = client.post(url, content=body, headers=headers)
            resp.raise_for_status()
            return resp.text

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=ctx)
        )
        resp = opener.open(req, timeout=CONFIG["request_timeout_sec"])
    else:
        resp = urllib.request.urlopen(req, context=ctx, timeout=CONFIG["request_timeout_sec"])
    return resp.read().decode("utf-8", errors="replace")


def load_cookie() -> tuple:
    """Load cookie from file with mtime-based caching."""
    cookie_file = CONFIG.get("cookie_file")
    if not cookie_file or not os.path.exists(cookie_file):
        return "", None
    try:
        mtime = os.path.getmtime(cookie_file)
        if mtime == _cookie_cache["mtime"] and _cookie_cache["str"]:
            return _cookie_cache["str"], _cookie_cache["sapisid"]
        with open(cookie_file, "r") as f:
            content = f.read().strip()
        if content.startswith("{"):
            data = json.loads(content)
            cookie_str = data.get("cookie", "")
            sapisid = data.get("sapisid", "")
        else:
            cookie_str = content
            pairs = dict(p.split("=", 1) for p in cookie_str.split("; ") if "=" in p)
            sapisid = pairs.get("SAPISID", "")
        _cookie_cache.update({"str": cookie_str, "sapisid": sapisid or None, "mtime": mtime})
        return cookie_str, sapisid if sapisid else None
    except Exception as e:
        log(f"Cookie load error: {e}")
        return _cookie_cache["str"], _cookie_cache["sapisid"]


def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


def _account_prefix() -> str:
    """Return the Gemini account path prefix for non-default Google accounts."""
    auth_user = CONFIG.get("auth_user")
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


def _http_status(exc: Exception):
    """Best-effort HTTP status extraction across urllib/httpx/helper errors."""
    for attr in ("code", "status", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int):
        return value
    match = re.search(r"(?:HTTP(?:\s+status)?\s+|\()(\d{3})(?:\)|:|\b)", str(exc), re.IGNORECASE)
    return int(match.group(1)) if match else None


def fetch_latest_bl(proxy=None):
    """Fetch the current Gemini Web frontend version through the active route."""
    try:
        account_prefix = _account_prefix()
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        cookie_str, sapisid = load_cookie()
        if cookie_str:
            headers["Cookie"] = cookie_str
        if sapisid:
            headers["Authorization"] = make_sapisidhash(sapisid)
        if account_prefix:
            headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])

        request = urllib.request.Request(
            f"https://gemini.google.com{account_prefix}/app",
            headers=headers,
        )
        effective_proxy = CONFIG.get("proxy") if proxy is None else proxy
        if effective_proxy:
            from . import browser_transport
            if browser_transport.available():
                html = browser_transport.get(
                    request.full_url,
                    headers,
                    effective_proxy,
                    15,
                )
            else:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": effective_proxy, "https": effective_proxy}),
                    urllib.request.HTTPSHandler(context=_get_ssl_ctx()),
                )
                with opener.open(request, timeout=15) as response:
                    html = response.read().decode("utf-8", errors="replace")
        else:
            with urllib.request.urlopen(request, context=_get_ssl_ctx(), timeout=15) as response:
                html = response.read().decode("utf-8", errors="replace")
        match = re.search(r"(boq_assistant-bard-web-server_\d+\.\d+_p\d+)", html)
        return match.group(1) if match else None
    except Exception as e:
        log(f"BL auto-update fetch failed: {e}")
        return None


def update_bl_if_needed(proxy=None) -> bool:
    """Refresh the Gemini Web frontend version and report whether it changed."""
    new_bl = fetch_latest_bl(proxy)
    old_bl = CONFIG.get("gemini_bl")
    if new_bl and new_bl != old_bl:
        CONFIG["gemini_bl"] = new_bl
        log(f"BL auto-updated: {old_bl} -> {new_bl}")
        return True
    return False


def _build_headers() -> dict:
    account_prefix = _account_prefix()
    headers = {
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Accept": "*/*",
        "Origin": "https://gemini.google.com",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": f"https://gemini.google.com{account_prefix}/app",
        "Accept-Language": "en-US,en;q=0.9",
        "Priority": "u=1, i",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "X-Same-Domain": "1",
    }
    if account_prefix:
        headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    return headers


def _apply_chat_persistence_flags(inner: list) -> None:
    """Apply Gemini Web persistence flags to an outgoing request payload."""
    if CONFIG.get("temporary_chats", False):
        # Match Gemini Web temporary-chat requests.
        inner[41] = [1]
        inner[45] = 1
    else:
        inner[41] = [2]


def _build_payload(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None) -> str:
    inner = [None] * 102
    if file_refs:
        refs = [[None, None, ref] for ref in file_refs]
        inner[0] = [prompt, 0, None, refs, None, None, 0]
    else:
        inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    _apply_chat_persistence_flags(inner)
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id
    if extra_fields:
        for k, v in extra_fields.items():
            inner[k] = v
    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if CONFIG.get("xsrf_token"):
        params["at"] = CONFIG["xsrf_token"]
    return urllib.parse.urlencode(params)


def _get_url() -> str:
    reqid = int(time.time()) % 1000000
    account_prefix = _account_prefix()
    return (
        f"https://gemini.google.com{account_prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )


def clean_text(text: str, strip: bool = True) -> str:
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    text = re.sub(r'http://googleusercontent\.com/card_content/\d+\n?', '', text)
    return text.strip() if strip else text


def _extract_texts_from_line(line: str) -> list:
    """Parse a single wrb.fr line and return list of text strings found."""
    if '"wrb.fr"' not in line or len(line) < 200:
        return []
    try:
        arr = json.loads(line)
        inner_str = arr[0][2]
        if not inner_str or len(inner_str) < 50:
            return []
        inner = json.loads(inner_str)
        if not (isinstance(inner, list) and len(inner) > 4 and inner[4]):
            return []
        texts = []
        for part in inner[4]:
            if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                for t in part[1]:
                    if isinstance(t, str) and t:
                        texts.append(t)
        return texts
    except (json.JSONDecodeError, IndexError, TypeError):
        return []


def _raise_bard_error_if_present(raw: str):
    """Surface upstream BardErrorInfo instead of silently returning empty text."""
    match = re.search(r"BardErrorInfo\s*\[(\d+)\]", raw)
    if match:
        raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{match.group(1)}]")


def extract_response_text(raw: str) -> str:
    """Parse full response to get final text."""
    _raise_bard_error_if_present(raw)
    last_text = ""
    for line in raw.split("\n"):
        for t in _extract_texts_from_line(line):
            if len(t) > len(last_text):
                last_text = t
    return clean_text(last_text)


def generate(
    prompt: str,
    model_id: int,
    think_mode: int,
    file_refs: list = None,
    extra_fields: dict = None,
    preferred_raw_uri: str = None,
) -> str:
    """Non-streaming generation with retry."""
    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields).encode()
    url = _get_url()
    headers = _build_headers()
    ctx = _get_ssl_ctx()
    has_node_pool = _enabled_clash_candidates_exist()

    if has_node_pool:
        from . import browser_transport
        if not browser_transport.available() and not browser_transport.allow_python_fallback():
            raise browser_transport.BrowserTransportUnavailable(
                "Gemini TLS helper unavailable for node pool; Docker image must include gemini-tls-helper"
            )
        if browser_transport.available() and not browser_transport.allow_python_fallback():
            browser_transport.ensure_available()

    last_err = None
    failed_nodes = set()
    attempts = _node_attempts() if has_node_pool else CONFIG["retry_attempts"]
    for attempt in range(attempts):
        raw_uri = None
        proxy = None
        if has_node_pool:
            preferred = preferred_raw_uri if attempt == 0 else None
            raw_uri, proxy, proxy_msg = _select_mihomo_proxy(failed_nodes, preferred)
            if not proxy:
                last_err = RuntimeError(f"Mihomo node pool unavailable: {proxy_msg}")
                log(str(last_err))
                if attempt < attempts - 1:
                    time.sleep(CONFIG["retry_delay_sec"])
                    continue
                break
        if not proxy and not has_node_pool:
            proxy = CONFIG.get("proxy")
        try:
            started = time.time()
            for bl_attempt in range(2):
                try:
                    raw = _post_upstream(body, url, headers, proxy, ctx)
                    break
                except Exception as e:
                    if bl_attempt == 0 and _http_status(e) == 405 and update_bl_if_needed(proxy):
                        url = _get_url()
                        log("Retrying with updated BL...")
                        continue
                    raise
            _record_node_health(raw_uri, True, latency_ms=int((time.time() - started) * 1000))
            return extract_response_text(raw)
        except Exception as e:
            last_err = e
            if raw_uri and _http_status(e) != 405:
                failed_nodes.add(raw_uri)
                _record_node_health(raw_uri, False, error=e)
            if attempt < attempts - 1:
                log(f"Retry {attempt+1}/{attempts}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
    raise last_err


def generate_stream(
    prompt: str,
    model_id: int,
    think_mode: int,
    file_refs: list = None,
    extra_fields: dict = None,
    preferred_raw_uri: str = None,
):
    """Streaming generation with retry on connection failure."""
    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields).encode()
    url = _get_url()
    headers = _build_headers()
    has_node_pool = _enabled_clash_candidates_exist()

    if has_node_pool:
        from . import browser_transport
        if not browser_transport.available() and not browser_transport.allow_python_fallback():
            raise browser_transport.BrowserTransportUnavailable(
                "Gemini TLS helper unavailable for node pool; Docker image must include gemini-tls-helper"
            )
        if browser_transport.available() and not browser_transport.allow_python_fallback():
            browser_transport.ensure_available()

    if not HAS_HTTPX and not has_node_pool:
        text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
        if text:
            yield text
        return

    last_err = None
    failed_nodes = set()
    emitted_raw_text = ""
    attempts = _node_attempts() if has_node_pool else CONFIG["retry_attempts"]
    for attempt in range(attempts):
        raw_uri = None
        proxy = None
        client = None
        try:
            if has_node_pool:
                preferred = preferred_raw_uri if attempt == 0 else None
                raw_uri, proxy, proxy_msg = _select_mihomo_proxy(failed_nodes, preferred)
                if not proxy:
                    last_err = RuntimeError(f"Mihomo node pool unavailable: {proxy_msg}")
                    log(str(last_err))
                    if attempt < attempts - 1:
                        time.sleep(CONFIG["retry_delay_sec"])
                        continue
                    break
            if proxy:
                from . import browser_transport
                if not browser_transport.available():
                    if not browser_transport.allow_python_fallback():
                        raise RuntimeError(
                            "Gemini TLS helper unavailable for proxied stream; set GEMINI_WEB2API_TLS_HELPER "
                            "or enable GEMINI_WEB2API_ALLOW_PYTHON_TRANSPORT_FALLBACK=1"
                        )
                else:
                    started = time.time()
                    buf = ""
                    for chunk in browser_transport.stream(url, headers, body, proxy, CONFIG["request_timeout_sec"]):
                        buf += chunk
                        _raise_bard_error_if_present(buf)
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            for t in _extract_texts_from_line(line):
                                if t == emitted_raw_text or emitted_raw_text.startswith(t):
                                    continue
                                if not t.startswith(emitted_raw_text):
                                    raise RuntimeError("Gemini stream content changed during retry")
                                delta = clean_text(t[len(emitted_raw_text):], strip=False)
                                emitted_raw_text = t
                                if delta:
                                    yield delta
                    _record_node_health(raw_uri, True, latency_ms=int((time.time() - started) * 1000))
                    return

            if proxy and not HAS_HTTPX:
                raise RuntimeError("httpx unavailable for Python proxy fallback")

            if proxy:
                transport = httpx.HTTPTransport(proxy=proxy)
                client = httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True, trust_env=False)
            elif not has_node_pool:
                client = _get_httpx_client()
            else:
                client = httpx.Client(timeout=CONFIG["request_timeout_sec"], verify=True, trust_env=False)
            started = time.time()
            with client.stream("POST", url, content=body, headers=headers) as resp:
                resp.raise_for_status()
                buf = ""
                for chunk in resp.iter_text():
                    buf += chunk
                    _raise_bard_error_if_present(buf)
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        for t in _extract_texts_from_line(line):
                            if t == emitted_raw_text or emitted_raw_text.startswith(t):
                                continue
                            if not t.startswith(emitted_raw_text):
                                raise RuntimeError("Gemini stream content changed during retry")
                            delta = clean_text(t[len(emitted_raw_text):], strip=False)
                            emitted_raw_text = t
                            if delta:
                                yield delta
            _record_node_health(raw_uri, True, latency_ms=int((time.time() - started) * 1000))
            return
        except Exception as e:
            last_err = e
            if str(e) == "Gemini stream content changed during retry":
                raise
            if _http_status(e) == 405 and not emitted_raw_text and update_bl_if_needed(proxy):
                log("Retrying stream with updated BL...")
                for delta in generate_stream(
                    prompt,
                    model_id,
                    think_mode,
                    file_refs,
                    extra_fields,
                    preferred_raw_uri=raw_uri or preferred_raw_uri,
                ):
                    yield delta
                return
            if raw_uri and _http_status(e) != 405:
                failed_nodes.add(raw_uri)
                _record_node_health(raw_uri, False, error=e)
            if attempt < attempts - 1:
                log(f"Stream retry {attempt+1}/{attempts}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
        finally:
            if proxy and client is not None:
                try:
                    client.close()
                except Exception:
                    pass
    raise last_err
