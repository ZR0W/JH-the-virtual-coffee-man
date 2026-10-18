"""``coffee-kb web serve``: the read-only status web app.

Standard library only (``http.server``). GET-only routes:

* ``/``            the dashboard (auto-refreshes every minute)
* ``/api/status``  the same data as JSON
* ``/logs?n=N``    the last N lines of ``data/logs/coffee-kb.log`` as plain text
* ``/healthz``     ``ok``

It opens the manifest read-only and never talks to YouTube; there is deliberately no way to
start a fetch from the browser.
"""

from __future__ import annotations

import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from coffee_kb import sdnotify
from coffee_kb.clock import Clock
from coffee_kb.config import Config
from coffee_kb.dashboard import build_dashboard, render_html, system_info
from coffee_kb.logs import tail_lines
from coffee_kb.manifest import Manifest
from coffee_kb.paths import DataPaths

log = logging.getLogger(__name__)

REFRESH_S = 60
MAX_LOG_LINES = 5000
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:",
    "Cache-Control": "no-store",
}


def make_handler(cfg: Config, paths: DataPaths, clock: Clock) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "coffee-kb"
        sys_version = ""

        def log_message(self, fmt: str, *args: object) -> None:  # quiet: debug log only
            log.debug("web %s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _data(self) -> dict:
            with Manifest(paths.manifest, readonly=True) as manifest:
                data = build_dashboard(cfg, paths, manifest, clock.now(), system_info(paths))
            data["refresh_s"] = REFRESH_S
            return data

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            url = urlparse(self.path)
            try:
                if url.path == "/":
                    body = render_html(self._data()).encode("utf-8")
                    self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")
                elif url.path == "/api/status":
                    body = json.dumps(self._data(), indent=2, default=str).encode("utf-8")
                    self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
                elif url.path == "/logs":
                    try:
                        n = int(parse_qs(url.query).get("n", ["500"])[0])
                    except ValueError:
                        n = 500
                    n = max(1, min(n, MAX_LOG_LINES))
                    text = "\n".join(tail_lines(paths.log_file, n)) + "\n"
                    self._send(HTTPStatus.OK, text.encode("utf-8"), "text/plain; charset=utf-8")
                elif url.path == "/healthz":
                    self._send(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
                else:
                    self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")
            except Exception:
                log.exception("web: error serving %s", url.path)
                self._send(
                    HTTPStatus.INTERNAL_SERVER_ERROR, b"internal error (see log)\n", "text/plain"
                )

        do_HEAD = do_GET

        def _read_only(self) -> None:
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"read-only\n", "text/plain; charset=utf-8")

        do_POST = do_PUT = do_DELETE = do_PATCH = _read_only

    return Handler


def make_server(
    cfg: Config, paths: DataPaths, clock: Clock, host: str | None = None, port: int | None = None
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(
        (host if host is not None else cfg.web.host, port if port is not None else cfg.web.port),
        make_handler(cfg, paths, clock),
    )
    server.daemon_threads = True
    return server


def serve(cfg: Config, paths: DataPaths, clock: Clock) -> None:
    # Binding happens here; if the port is taken this raises and the process exits non-zero
    # *before* READY=1, so `systemctl restart` reports the failure.
    server = make_server(cfg, paths, clock)
    host, port = server.server_address[:2]
    log.info("coffee-kb web: serving the status dashboard on http://%s:%s/ (read-only)", host, port)
    sdnotify.notify("READY=1", f"STATUS=serving on http://{host}:{port}/ (read-only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sdnotify.notify("STOPPING=1")
        server.server_close()
