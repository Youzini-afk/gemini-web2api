"""Entry point: python -m gemini_web2api"""
import argparse
import os
import threading

from .config import CONFIG, load_config, find_config, load_env, ensure_admin_password, get_config_path, get_data_dir
from .models import MODELS
from .gemini import HAS_HTTPX, update_bl_if_needed
from .server import GeminiHandler, ThreadedServer
from . import admin_api, admin_session
from . import __version__


def main():
    parser = argparse.ArgumentParser(description="Gemini Web to OpenAI API")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--cookie-file", type=str, default=None)
    parser.add_argument("--proxy", type=str, default=None, help="HTTP proxy, e.g. http://127.0.0.1:7890")
    parser.add_argument("--version", action="version", version=f"gemini-web2api {__version__}")
    args = parser.parse_args()

    config_path = args.config or os.environ.get("GEMINI_WEB2API_CONFIG") or find_config()
    if config_path:
        load_config(config_path)
    load_env()

    if args.port:
        CONFIG["port"] = args.port
    if args.cookie_file:
        CONFIG["cookie_file"] = args.cookie_file
    if args.proxy:
        CONFIG["proxy"] = args.proxy

    # Keep the Gemini Web frontend version current. If startup cannot reach the
    # page, requests still use the configured value and retry a 405 in-band.
    update_bl_if_needed()

    # Ensure admin password exists (auto-generate + persist if missing)
    ensure_admin_password()

    # Start admin session cleanup timer (hourly)
    def _cleanup_loop():
        import time
        while True:
            time.sleep(3600)
            admin_session.cleanup_expired()
    t = threading.Thread(target=_cleanup_loop, daemon=True)
    t.start()

    port = CONFIG["port"]
    server = ThreadedServer((CONFIG["host"], port), GeminiHandler)
    print(f"gemini-web2api v{__version__}")
    print(f"  Listening: http://0.0.0.0:{port}")
    print(f"  Base URL:  http://localhost:{port}/v1")
    print(f"  Admin UI:  http://localhost:{port}/admin/")
    print(f"  Config:    {get_config_path() or 'none'}")
    print(f"  Data dir:  {get_data_dir()}")
    print(f"  Models:    {', '.join(MODELS.keys())}")
    print(f"  Cookie:    {'yes' if CONFIG.get('cookie_file') else 'none (anonymous)'}")
    print(f"  API Auth:  {'enabled' if admin_api.get_all_key_values() else 'disabled'}")
    print(f"  Proxy:     {CONFIG.get('proxy') or 'system env'}")
    print(f"  Streaming: {'httpx (true streaming)' if HAS_HTTPX else 'urllib (buffered)'}")
    print(f"  BL:        {CONFIG['gemini_bl']}")
    print(f"  Temporary: {'yes' if CONFIG.get('temporary_chats', False) else 'no'}")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
