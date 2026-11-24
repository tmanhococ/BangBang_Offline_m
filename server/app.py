"""Bang Bang Private — HTTP + login + static + start policy/socket threads."""
from __future__ import annotations

import argparse
import os
import json
import logging
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
WWW = ROOT / "www"
DATA = ROOT / "data"

sys.path.insert(0, str(Path(__file__).resolve().parent))


from auth_store import Store
from storage_import import initialize
from storage import connection, SCHEMA_VERSION
from server_logging import configure, get_logger, LEVELS

log = get_logger('http')

STORE = Store()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WWW), **kwargs)

    def end_headers(self) -> None:
        path = urlparse(getattr(self, 'path', '')).path
        import admin_buff
        if admin_buff.page_path(path):
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'")
        super().end_headers()

    def do_HEAD(self) -> None:
        path = urlparse(self.path).path
        import admin_buff
        if path.startswith('/api/admin/'):
            return admin_buff.handle(self)
        if admin_buff.page_path(path):
            if not admin_buff.protect_page(self):
                return
        super().do_HEAD()

    def log_message(self, fmt: str, *args) -> None:
        log.info('[http] ' + fmt, *args)

    def log_request(self, code='-', size='-') -> None:
        status = int(code) if str(code).isdigit() else 0
        level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
        if not log.isEnabledFor(level):
            return
        # Log status failures, including JSON responses that do not call send_error.
        # Avoid copying query tokens into persistent diagnostics.
        log.log(level, '[http] %s %s %s %s', self.command,
                urlparse(getattr(self, 'path', '')).path, code, size)

    def log_error(self, fmt: str, *args) -> None:
        # The status/path is recorded once by log_request().
        log.debug('[http] ' + fmt, *args)

    def _json(self, code: int, obj: dict, set_cookie: str | None = None, clear_cookie: bool = False) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        if clear_cookie:
            self.send_header(
                "Set-Cookie",
                "bb_web_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",
            )
        elif set_cookie:
            self.send_header(
                "Set-Cookie",
                f"bb_web_session={set_cookie}; Path=/; Max-Age=86400; HttpOnly; SameSite=Lax",
            )
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def _cookie(self) -> str | None:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == "bb_web_session":
                    return v
        return None

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        import admin_buff
        if path.startswith('/api/admin/'):
            return admin_buff.handle(self)
        if admin_buff.page_path(path):
            if not admin_buff.protect_page(self):
                return
        if path == "/html5/ws":
            from html5_bridge import handle_websocket
            session = STORE.session_from_cookie(self._cookie())
            return handle_websocket(self, session=session, store=STORE)
        if path == "/health":
            from cpp_backend import status as acceleration_status
            with connection() as db:
                db.execute('SELECT 1 FROM worlds LIMIT 1').fetchone()
            return self._json(200, {"ok": True, "service": "bangbang-private",
                                    "storage": "sqlite", "schema": SCHEMA_VERSION, "instance": os.environ.get("BB_LAUNCH_ID", ""),
                                    "combat_acceleration": acceleration_status()})
        from guild_protocol import HTTP_ROUTES
        from character_info import HTTP_ROUTES as CHARACTER_ROUTES
        from capi import CAPI_ROUTES
        if path.startswith("/capi") or path.lstrip('/').lower() in HTTP_ROUTES | CHARACTER_ROUTES | CAPI_ROUTES:
            from capi import handle

            hit = handle(self.path)
            if hit is None:
                self.send_error(404)
                return
            body, ctype = hit
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/", "/index.html"):
            self.path = "/index.html"
            return super().do_GET()

        file_path = WWW / path.lstrip("/")
        if not file_path.exists():
            # Dynamically fetch & cache any missing asset from netzone mirror
            if path.startswith("/rez/") or path.endswith((".swf", ".png", ".jpg", ".dat", ".cub", ".xml")):
                try:
                    import urllib.request
                    import ssl
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    upstream_url = f"https://netzone.id.vn{path}"
                    req = urllib.request.Request(upstream_url, headers={"User-Agent": "Mozilla/5.0 BB-private-mirror"})
                    with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
                        if resp.status == 200:
                            data = resp.read()
                            file_path.parent.mkdir(parents=True, exist_ok=True)
                            file_path.write_bytes(data)
                            log.info(f"[http] Proxy-cached {path} ({len(data)} bytes) from netzone")
                            self.send_response(200)
                            ctype = self.guess_type(str(file_path))
                            self.send_header("Content-Type", ctype)
                            self.send_header("Content-Length", str(len(data)))
                            self.end_headers()
                            self.wfile.write(data)
                            return
                except Exception:
                    pass

            if path.endswith(".xml"):
                log.warning(f"[http] Missing XML fallback for {path}")
                empty_xml = b'<?xml version="1.0" encoding="utf-8"?><config><localDatas></localDatas></config>'
                self.send_response(200)
                self.send_header("Content-Type", "application/xml; charset=utf-8")
                self.send_header("Content-Length", str(len(empty_xml)))
                self.end_headers()
                self.wfile.write(empty_xml)
                return

        return super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path.startswith('/api/admin/'):
            import admin_buff
            return admin_buff.handle(self)
        if path in ('/api/bots/get','/api/bots/save'):
            session = STORE.session_from_cookie(self._cookie())
            if not session:
                return self._json(401,dict(success=False,message='Đăng nhập lại để thiết lập trận.'))
            from bot_setup import load_setup, save_setup, AI_TANKS
            try:
                import characters
                from storage import for_profile
                with for_profile(characters.for_username(session['username'])):
                    setup = save_setup(self._read_json()) if path.endswith('/save') else load_setup()
                return self._json(200,dict(success=True,setup=setup,tanks=list(AI_TANKS.values())))
            except ValueError as exc:
                return self._json(400,dict(success=False,message=str(exc)))
        if path == "/api/register":
            payload = self._read_json()
            result = STORE.register(str(payload.get("username") or ""), str(payload.get("password") or ""))
            if result.get("success"):
                cookie = result.pop("cookie")
                return self._json(200, result, set_cookie=cookie)
            return self._json(200, result)
        if path == "/api/login":
            payload = self._read_json()
            result = STORE.login(str(payload.get("username") or ""), str(payload.get("password") or ""))
            if result.get("success"):
                cookie = result.pop("cookie")
                return self._json(200, result, set_cookie=cookie)
            return self._json(200, result, clear_cookie=True)
        if path == "/api/session":
            result = STORE.refresh_from_cookie(self._cookie())
            if result is None:
                return self._json(200, {"success": False}, clear_cookie=True)
            return self._json(200, result)
        if path == "/api/logout":
            STORE.logout_cookie(self._cookie())
            return self._json(200, {"success": True}, clear_cookie=True)
        if path == "/capi":
            return self._json(200, {"ok": True})
        self.send_error(404)


class Server(ThreadingHTTPServer):
    request_queue_size = 128

    def handle_error(self, request, client_address):
        log.exception('[http] Request failed from %s', client_address)


def start_side_servers(html5_local=False, native_port=49119) -> None:
    from policy import start_policy_servers
    from sixcube_server import start_sixcube

    if html5_local:
        import sixcube_server, html5_bridge
        sixcube_server.LISTEN_PORT = native_port
        html5_bridge.TCP_DESTINATION = ("127.0.0.1", native_port)
        start_sixcube(bind="127.0.0.1")
    else:
        start_policy_servers()
        start_sixcube()


def start_https(bind: str, port: int) -> None:
    import ssl

    cert = DATA / "certs" / "cert.pem"
    key = DATA / "certs" / "key.pem"
    if not cert.exists() or not key.exists():
        log.warning("[https] missing certs, skip")
        return
    httpd = Server((bind, port), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    log.info(f"[https] https://{bind}:{port}/")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--html5-local", action="store_true", help="HTML5 loopback only; no Flash policy or HTTPS services")
    parser.add_argument("--native-port", type=int, default=49119)
    parser.add_argument("--port", type=int, default=18083)
    parser.add_argument("--https-port", type=int, default=18443)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument('--enable-admin-buff', action='store_true',
                        help='Explicitly enable loopback-only gift-mail admin; never grants on startup')
    parser.add_argument('--admin-key-file', type=Path,
                        help='Private key file outside www; contents are never printed or logged')
    parser.add_argument("--log-level", choices=LEVELS, default="WARNING", type=str.upper,
                        help="WARNING by default; DEBUG also records raw TCP packets")
    args = parser.parse_args()
    if args.admin_key_file and not args.enable_admin_buff:
        parser.error('--admin-key-file requires --enable-admin-buff')
    configure(args.log_level)
    from cpp_backend import initialize as initialize_acceleration
    initialize_acceleration()
    DATA.mkdir(parents=True, exist_ok=True)
    WWW.mkdir(parents=True, exist_ok=True)
    initialize()
    import characters
    characters.initialize()
    import guilds
    guilds.initialize()
    import offline_mail
    offline_mail.initialize()
    import world_boss_store
    world_boss_store.initialize()
    if args.enable_admin_buff:
        import admin_buff
        key_path = admin_buff.enable(args.admin_key_file)
        print('[admin] Local gift-mail admin: http://127.0.0.1:' + str(args.port) + '/admin/buff/')
        print('[admin] Operator key file (open locally, never share): ' + str(key_path))
    start_side_servers(args.html5_local, args.native_port)
    if not args.html5_local:
        start_https(args.bind, args.https_port)

    Server.allow_reuse_address = True
    for attempt in range(5):
        try:
            httpd = Server((args.bind, args.port), Handler)
            break
        except OSError as e:
            log.warning(f"[http] bind attempt {attempt+1} failed: {e}, retrying...")
            time.sleep(1)
    else:
        log.error(f"[http] ERROR: Could not bind port {args.port}!")
        return

    log.info(f"[http] http://{args.bind}:{args.port}/  www={WWW}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("[http] stop")


if __name__ == "__main__":
    main()
