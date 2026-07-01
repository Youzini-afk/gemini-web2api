"""Clash/Mihomo YAML parser: parse subscription content into node list.

Supports standard multiline Clash YAML and inline YAML entries.
Converts ss, vmess, vless, trojan, hysteria2 proxy entries to node URIs.
Stores original Clash proxy dict for mihomo config generation.
"""
import base64
import json
import urllib.request
import urllib.parse
import ssl
import socket
import re

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

FETCH_TIMEOUT = 300  # 5 minutes for large subscriptions
FETCH_MAX_SIZE = 50 * 1024 * 1024  # 50 MiB


def has_yaml():
    return HAS_YAML


def fetch_subscription(url):
    """Fetch subscription content from URL. Returns text or raises.
    Timeout: 5 min. Max size: 50 MiB. Rejects private/local IPs.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {parsed.scheme}")

    # SSRF guard: reject private/loopback IPs
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for family, _, _, _, addr in infos:
            ip = addr[0]
            if ip.startswith("127.") or ip.startswith("10.") or ip.startswith("192.168."):
                raise ValueError(f"private IP rejected: {ip}")
            if ip.startswith("172."):
                parts = ip.split(".")
                if len(parts) >= 2 and 16 <= int(parts[1]) <= 31:
                    raise ValueError(f"private IP rejected: {ip}")
            if ip == "::1" or ip.startswith("fe80:") or ip.startswith("fc00:"):
                raise ValueError(f"private IP rejected: {ip}")
    except socket.gaierror:
        pass  # DNS may fail for valid URLs with proxy

    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={
        "User-Agent": "clash-verge/v1.0",
        "Accept": "text/yaml, application/yaml, text/plain, */*",
    })
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT, context=ctx) as resp:
        data = resp.read(FETCH_MAX_SIZE + 1)
        if len(data) > FETCH_MAX_SIZE:
            raise ValueError(f"subscription body exceeds {FETCH_MAX_SIZE // 1024 // 1024}MiB")
        return data.decode("utf-8", errors="replace")


def parse_subscription_text(text):
    """Parse subscription text into a list of node dicts.

    Detects:
    - Clash/Mihomo YAML (contains 'proxies:')
    - Base64 subscription (decoded to URIs)
    - One URI per line

    Returns list of {raw_uri, name, protocol, clash_config}
    """
    text = text.strip()
    if not text:
        return []

    # Detect Clash YAML
    if "proxies:" in text or text.startswith("proxy-providers:") or _looks_like_yaml(text):
        return parse_clash_yaml(text)

    # Try base64 decode
    try:
        decoded = base64.b64decode(text).decode("utf-8", errors="replace")
        if "://" in decoded:
            return parse_uri_lines(decoded)
    except Exception:
        pass

    # Fall back to URI-per-line
    return parse_uri_lines(text)


def _looks_like_yaml(text):
    yaml_indicators = ["---", "proxies:", "proxy-groups:", "rules:", "port:", "mixed-port:"]
    first_500 = text[:500]
    return any(ind in first_500 for ind in yaml_indicators)


def parse_clash_yaml(text):
    """Parse Clash/Mihomo YAML into node list."""
    if not HAS_YAML:
        raise RuntimeError("PyYAML is required for Clash YAML parsing. Install with: pip install pyyaml")

    data = yaml.safe_load(text)
    if not data or not isinstance(data, dict):
        return []

    proxies = data.get("proxies", [])
    if not proxies:
        return []

    result = []
    for p in proxies:
        if not isinstance(p, dict):
            continue
        node = clash_proxy_to_node(p)
        if node:
            result.append(node)
    return result


def clash_proxy_to_node(p):
    """Convert a Clash proxy dict to a node dict with raw_uri.
    Returns None for unsupported protocols.
    """
    ptype = p.get("type", "").lower()
    name = p.get("name", "")
    server = p.get("server", "")
    port = p.get("port", 0)

    if not server or not port:
        return None

    uri = None
    protocol = None

    if ptype == "ss":
        protocol = "ss"
        uri = _ss_to_uri(p)
    elif ptype == "vmess":
        protocol = "vmess"
        uri = _vmess_to_uri(p)
    elif ptype == "vless":
        protocol = "vless"
        uri = _vless_to_uri(p)
    elif ptype == "trojan":
        protocol = "trojan"
        uri = _trojan_to_uri(p)
    elif ptype in ("hysteria2", "hy2"):
        protocol = "hysteria2"
        uri = _hysteria2_to_uri(p)
    else:
        return None  # Unsupported protocol (http, socks5, anytls, mieru, etc.)

    if not uri:
        return None

    return {
        "raw_uri": uri,
        "name": name,
        "protocol": protocol,
        "clash_config": p,
    }


def _ss_to_uri(p):
    """Convert ss proxy to URI."""
    method = p.get("cipher", "")
    password = p.get("password", "")
    server = p.get("server", "")
    port = p.get("port", 0)
    name = p.get("name", "")
    plugin = p.get("plugin", "")
    plugin_opts = p.get("plugin-opts", {})

    userinfo = f"{method}:{password}"
    if plugin:
        opts_str = ",".join(f"{k}={v}" for k, v in plugin_opts.items()) if isinstance(plugin_opts, dict) else str(plugin_opts)
        userinfo += f"?plugin={urllib.parse.quote(plugin + ';' + opts_str, safe='')}"

    fragment = urllib.parse.quote(name, safe="") if name else ""
    return f"ss://{base64.b64encode(userinfo.encode()).decode()}@{server}:{port}" + (f"#{fragment}" if fragment else "")


def _vmess_to_uri(p):
    """Convert vmess proxy to URI (vmess://base64(json))."""
    obj = {
        "v": "2",
        "ps": p.get("name", ""),
        "add": p.get("server", ""),
        "port": str(p.get("port", 0)),
        "id": p.get("uuid", p.get("id", "")),
        "aid": str(p.get("alterId", p.get("alterid", 0))),
        "net": p.get("network", "tcp"),
        "type": p.get("type", "none"),
        "host": p.get("servername", ""),
        "path": p.get("ws-opts", {}).get("path", "/") if p.get("network") == "ws" else "",
        "tls": "tls" if p.get("tls") else "",
        "sni": p.get("servername", p.get("sni", "")),
    }
    return "vmess://" + base64.b64encode(json.dumps(obj).encode()).decode()


def _vless_to_uri(p):
    """Convert vless proxy to URI."""
    uuid = p.get("uuid", "")
    server = p.get("server", "")
    port = p.get("port", 0)
    name = p.get("name", "")

    params = {}
    # Transport
    net = p.get("network", "tcp")
    if net == "ws":
        params["type"] = "ws"
        ws = p.get("ws-opts", {})
        if ws.get("path"):
            params["path"] = ws["path"]
        headers = ws.get("headers", {})
        if headers.get("Host"):
            params["host"] = headers["Host"]
    elif net == "grpc":
        params["type"] = "grpc"
        grpc = p.get("grpc-opts", {})
        if grpc.get("grpc-service-name"):
            params["serviceName"] = grpc["grpc-service-name"]
    elif net == "tcp":
        pass

    # TLS / Reality
    if p.get("tls"):
        params["security"] = "tls"
        if p.get("servername") or p.get("sni"):
            params["sni"] = p.get("servername", p.get("sni", ""))
    reality = p.get("reality-opts", {})
    if reality:
        params["security"] = "reality"
        if reality.get("public-key"):
            params["pbk"] = reality["public-key"]
        if reality.get("short-id"):
            params["sid"] = reality["short-id"]
        if p.get("servername"):
            params["sni"] = p["servername"]
        if reality.get("fingerprint"):
            params["fp"] = reality["fingerprint"]

    flow = p.get("flow", "")
    if flow:
        params["flow"] = flow

    if p.get("client-fingerprint"):
        params["fp"] = p["client-fingerprint"]
    if p.get("skip-cert-verify"):
        params["allowInsecure"] = "1"

    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    fragment = urllib.parse.quote(name, safe="") if name else ""
    base = f"vless://{uuid}@{server}:{port}"
    if query:
        base += f"?{query}"
    if fragment:
        base += f"#{fragment}"
    return base


def _trojan_to_uri(p):
    """Convert trojan proxy to URI."""
    password = p.get("password", "")
    server = p.get("server", "")
    port = p.get("port", 0)
    name = p.get("name", "")

    params = {}
    if p.get("sni") or p.get("servername"):
        params["sni"] = p.get("sni", p.get("servername", ""))
    if p.get("type") and p.get("type") != "tcp":
        params["type"] = p["type"]
    if p.get("network") == "ws":
        params["type"] = "ws"
        ws = p.get("ws-opts", {})
        if ws.get("path"):
            params["path"] = ws["path"]
        headers = ws.get("headers", {})
        if headers.get("Host"):
            params["host"] = headers["Host"]
    if p.get("network") == "grpc":
        params["type"] = "grpc"
        grpc = p.get("grpc-opts", {})
        if grpc.get("grpc-service-name"):
            params["serviceName"] = grpc["grpc-service-name"]
    if p.get("skip-cert-verify"):
        params["allowInsecure"] = "1"
    if p.get("client-fingerprint"):
        params["fp"] = p["client-fingerprint"]
    if p.get("alpn"):
        params["alpn"] = p["alpn"] if isinstance(p["alpn"], str) else ",".join(p["alpn"])

    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    fragment = urllib.parse.quote(name, safe="") if name else ""
    base = f"trojan://{urllib.parse.quote(password, safe='')}@{server}:{port}"
    if query:
        base += f"?{query}"
    if fragment:
        base += f"#{fragment}"
    return base


def _hysteria2_to_uri(p):
    """Convert hysteria2 proxy to URI."""
    password = p.get("password", "")
    server = p.get("server", "")
    port = p.get("port", 0)
    name = p.get("name", "")

    params = {}
    if p.get("sni"):
        params["sni"] = p["sni"]
    if p.get("obfs"):
        params["obfs"] = p["obfs"]
    if p.get("obfs-password"):
        params["obfs-password"] = p["obfs-password"]
    if p.get("skip-cert-verify"):
        params["insecure"] = "1"
    if p.get("client-fingerprint"):
        params["fp"] = p["client-fingerprint"]
    if p.get("alpn"):
        params["alpn"] = p["alpn"] if isinstance(p["alpn"], str) else ",".join(p["alpn"])

    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    fragment = urllib.parse.quote(name, safe="") if name else ""
    base = f"hy2://{urllib.parse.quote(password, safe='')}@{server}:{port}"
    if query:
        base += f"?{query}"
    if fragment:
        base += f"#{fragment}"
    return base


def parse_uri_lines(text):
    """Parse one-URI-per-line text into node list."""
    result = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" not in line:
            continue
        protocol = line.split("://")[0].lower()
        if protocol not in ("ss", "vmess", "vless", "trojan", "hysteria2", "hy2"):
            continue

        # Extract name from fragment
        name = ""
        if "#" in line:
            name = urllib.parse.unquote(line.split("#")[-1])

        # Normalize hy2 → hysteria2
        if protocol == "hy2":
            protocol = "hysteria2"

        result.append({
            "raw_uri": line,
            "name": name,
            "protocol": protocol,
            "clash_config": None,  # No Clash config for URI-only nodes
        })
    return result
