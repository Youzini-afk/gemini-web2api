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


def _b64decode_text(value):
    """Decode standard/url-safe base64 with forgiving padding."""
    if not value:
        return ""
    s = value.strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s).decode("utf-8", errors="replace")


def _to_int(value, default=0):
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _truthy(value):
    return str(value).lower() in ("1", "true", "yes", "y")


def _first(params, *names, default=""):
    for name in names:
        if name in params and params[name]:
            return params[name][0]
    return default


def _query_dict(query):
    return urllib.parse.parse_qs(query, keep_blank_values=True)


def _split_alpn(value):
    if not value:
        return None
    parts = [p.strip() for p in str(value).split(",") if p.strip()]
    return parts if len(parts) > 1 else (parts[0] if parts else None)


def _parse_plugin(plugin_value):
    if not plugin_value:
        return "", {}
    value = urllib.parse.unquote(plugin_value)
    parts = [p for p in value.split(";") if p]
    if not parts:
        return "", {}
    opts = {}
    for part in parts[1:]:
        if "=" in part:
            k, v = part.split("=", 1)
            opts[k] = v
    return parts[0], opts


def _parse_host_port(text):
    """Parse host:port, including bracketed IPv6."""
    text = text.strip()
    if text.startswith("[") and "]" in text:
        host, rest = text[1:].split("]", 1)
        if rest.startswith(":"):
            return host, _to_int(rest[1:])
        return host, 0
    if ":" not in text:
        return text, 0
    host, port = text.rsplit(":", 1)
    return host, _to_int(port)


def _parse_ss_uri(uri, name):
    raw = uri[len("ss://"):]
    raw_no_frag = raw.split("#", 1)[0]
    query = ""
    if "?" in raw_no_frag:
        raw_no_frag, query = raw_no_frag.split("?", 1)

    method = password = server = ""
    port = 0
    if "@" in raw_no_frag:
        userinfo, hostport = raw_no_frag.rsplit("@", 1)
        userinfo = urllib.parse.unquote(userinfo)
        if ":" not in userinfo:
            try:
                userinfo = _b64decode_text(userinfo)
            except Exception:
                pass
        if ":" not in userinfo:
            raise ValueError("ss URI missing method/password")
        method, password = userinfo.split(":", 1)
        server, port = _parse_host_port(hostport)
    else:
        decoded = _b64decode_text(raw_no_frag)
        if "@" not in decoded:
            raise ValueError("ss full-base64 URI missing server")
        userinfo, hostport = decoded.rsplit("@", 1)
        if ":" not in userinfo:
            raise ValueError("ss URI missing method/password")
        method, password = userinfo.split(":", 1)
        server, port = _parse_host_port(hostport)

    if not method or not password or not server or not port:
        raise ValueError("ss URI missing required fields")
    cfg = {"name": name or server, "type": "ss", "server": server, "port": port, "cipher": method, "password": password}
    params = _query_dict(query)
    plugin, plugin_opts = _parse_plugin(_first(params, "plugin"))
    if plugin:
        cfg["plugin"] = plugin
        if plugin_opts:
            cfg["plugin-opts"] = plugin_opts
    return cfg


def _parse_vmess_uri(uri, name):
    payload = uri[len("vmess://"):].split("#", 1)[0]
    data = json.loads(_b64decode_text(payload))
    server = data.get("add") or data.get("server") or ""
    port = _to_int(data.get("port"))
    uuid = data.get("id") or data.get("uuid") or ""
    if not server or not port or not uuid:
        raise ValueError("vmess URI missing required fields")
    cfg = {
        "name": name or data.get("ps") or server,
        "type": "vmess",
        "server": server,
        "port": port,
        "uuid": uuid,
        "alterId": _to_int(data.get("aid", data.get("alterId", 0))),
        "cipher": data.get("scy") or data.get("cipher") or "auto",
    }
    if str(data.get("tls", "")).lower() in ("tls", "true", "1"):
        cfg["tls"] = True
    sni = data.get("sni") or data.get("servername") or data.get("host")
    if sni:
        cfg["servername"] = sni
    if _truthy(data.get("allowInsecure")):
        cfg["skip-cert-verify"] = True
    net = data.get("net") or data.get("network") or "tcp"
    if net and net != "tcp":
        cfg["network"] = net
    if net == "ws":
        ws = {}
        if data.get("path"):
            ws["path"] = data["path"]
        if data.get("host"):
            ws["headers"] = {"Host": data["host"]}
        if ws:
            cfg["ws-opts"] = ws
    elif net == "grpc":
        service = data.get("path") or data.get("serviceName") or data.get("serviceName")
        if service:
            cfg["grpc-opts"] = {"grpc-service-name": service}
    elif net == "http":
        http_opts = {}
        if data.get("path"):
            http_opts["path"] = [data["path"]] if isinstance(data["path"], str) else data["path"]
        if data.get("host"):
            http_opts["headers"] = {"Host": [data["host"]] if isinstance(data["host"], str) else data["host"]}
        if http_opts:
            cfg["http-opts"] = http_opts
    return cfg


def _apply_transport_opts(cfg, params):
    net = _first(params, "type", "network", default="tcp") or "tcp"
    if net and net != "tcp":
        cfg["network"] = net
    if net == "ws":
        ws = {}
        path = _first(params, "path")
        host = _first(params, "host", "Host")
        if path:
            ws["path"] = path
        if host:
            ws["headers"] = {"Host": host}
        if ws:
            cfg["ws-opts"] = ws
    elif net == "grpc":
        service = _first(params, "serviceName", "service-name", "grpc-service-name")
        if service:
            cfg["grpc-opts"] = {"grpc-service-name": service}


def _parse_vless_uri(uri, name):
    parsed = urllib.parse.urlsplit(uri)
    params = _query_dict(parsed.query)
    uuid = urllib.parse.unquote(parsed.username or "")
    server = parsed.hostname or ""
    port = parsed.port or 0
    if not uuid or not server or not port:
        raise ValueError("vless URI missing required fields")
    cfg = {"name": name or server, "type": "vless", "server": server, "port": port, "uuid": uuid}
    security = _first(params, "security")
    if security in ("tls", "reality"):
        cfg["tls"] = True
    sni = _first(params, "sni", "servername", "peer")
    if sni:
        cfg["servername"] = sni
    fp = _first(params, "fp", "fingerprint", "client-fingerprint")
    if fp:
        cfg["client-fingerprint"] = fp
    flow = _first(params, "flow")
    if flow:
        cfg["flow"] = flow
    if security == "reality":
        reality = {}
        pbk = _first(params, "pbk", "public-key")
        sid = _first(params, "sid", "short-id")
        if pbk:
            reality["public-key"] = pbk
        if sid:
            reality["short-id"] = sid
        if reality:
            cfg["reality-opts"] = reality
    if _truthy(_first(params, "allowInsecure", "insecure", "skip-cert-verify")):
        cfg["skip-cert-verify"] = True
    _apply_transport_opts(cfg, params)
    return cfg


def _parse_trojan_uri(uri, name):
    parsed = urllib.parse.urlsplit(uri)
    params = _query_dict(parsed.query)
    password = urllib.parse.unquote(parsed.username or "")
    server = parsed.hostname or ""
    port = parsed.port or 0
    if not password or not server or not port:
        raise ValueError("trojan URI missing required fields")
    cfg = {"name": name or server, "type": "trojan", "server": server, "port": port, "password": password}
    sni = _first(params, "sni", "servername", "peer")
    if sni:
        cfg["sni"] = sni
    fp = _first(params, "fp", "fingerprint", "client-fingerprint")
    if fp:
        cfg["client-fingerprint"] = fp
    alpn = _split_alpn(_first(params, "alpn"))
    if alpn:
        cfg["alpn"] = alpn
    if _truthy(_first(params, "allowInsecure", "insecure", "skip-cert-verify")):
        cfg["skip-cert-verify"] = True
    _apply_transport_opts(cfg, params)
    return cfg


def _parse_hysteria2_uri(uri, name):
    parsed = urllib.parse.urlsplit(uri)
    params = _query_dict(parsed.query)
    password = urllib.parse.unquote(parsed.username or "")
    server = parsed.hostname or ""
    port = parsed.port or 0
    if not password or not server or not port:
        raise ValueError("hysteria2 URI missing required fields")
    cfg = {"name": name or server, "type": "hysteria2", "server": server, "port": port, "password": password}
    sni = _first(params, "sni", "servername", "peer")
    if sni:
        cfg["sni"] = sni
    mport = _first(params, "mport", "ports")
    if mport:
        cfg["ports"] = mport
    obfs = _first(params, "obfs")
    if obfs:
        cfg["obfs"] = obfs
    obfs_password = _first(params, "obfs-password", "obfs_password")
    if obfs_password:
        cfg["obfs-password"] = obfs_password
    fp = _first(params, "fp", "fingerprint", "client-fingerprint")
    if fp:
        cfg["client-fingerprint"] = fp
    alpn = _split_alpn(_first(params, "alpn"))
    if alpn:
        cfg["alpn"] = alpn
    if _truthy(_first(params, "insecure", "allowInsecure", "skip-cert-verify")):
        cfg["skip-cert-verify"] = True
    return cfg


def uri_to_clash_config(uri, protocol, name=""):
    """Convert one supported URI to a Mihomo proxy dict, or raise ValueError."""
    if protocol == "ss":
        return _parse_ss_uri(uri, name)
    if protocol == "vmess":
        return _parse_vmess_uri(uri, name)
    if protocol == "vless":
        return _parse_vless_uri(uri, name)
    if protocol == "trojan":
        return _parse_trojan_uri(uri, name)
    if protocol in ("hysteria2", "hy2"):
        return _parse_hysteria2_uri(uri, name)
    raise ValueError(f"unsupported protocol: {protocol}")


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

        clash_config = None
        import_error = ""
        try:
            clash_config = uri_to_clash_config(line, protocol, name)
            name = clash_config.get("name") or name
        except Exception as e:
            import_error = str(e)

        node = {
            "raw_uri": line,
            "name": name,
            "protocol": protocol,
            "clash_config": clash_config,
        }
        if import_error:
            node["import_error"] = import_error
        result.append(node)
    return result
