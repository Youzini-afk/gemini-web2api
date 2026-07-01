"""Static asset serving for admin UI pages."""
import os
import mimetypes

_ADMIN_DIR = os.path.join(os.path.dirname(__file__), "admin")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def serve_admin_page(handler):
    """Serve the admin HTML page or a static asset.

    Called by server.py when path is /admin or /admin/*.
    """
    from urllib.parse import urlparse, unquote
    path = urlparse(handler.path).path

    # /admin → redirect to /admin/
    if path == "/admin":
        handler.send_response(302)
        handler.send_header("Location", "/admin/")
        handler.end_headers()
        return

    # /admin/ → admin.html
    if path == "/admin/":
        _serve_file(handler, "admin.html")
        return

    # /admin/{file} → static asset
    rel = unquote(path[len("/admin/"):])
    # Security: prevent path traversal
    rel = rel.replace("\\", "/").lstrip("/")
    if ".." in rel.split("/"):
        handler.send_json({"error": "forbidden"}, 403)
        return

    file_path = os.path.join(_ADMIN_DIR, rel)
    if not os.path.isfile(file_path):
        handler.send_json({"error": "not found"}, 404)
        return

    _serve_file(handler, rel)


def _serve_file(handler, rel_name):
    file_path = os.path.join(_ADMIN_DIR, rel_name)
    if not os.path.isfile(file_path):
        handler.send_json({"error": "not found"}, 404)
        return

    ext = os.path.splitext(rel_name)[1].lower()
    content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")

    with open(file_path, "rb") as f:
        body = f.read()

    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    # No-cache for HTML/JS to avoid stale UI after updates
    if ext in (".html", ".js", ".css", ".json"):
        handler.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
    else:
        handler.send_header("Cache-Control", "max-age=3600")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
