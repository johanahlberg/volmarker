"""Local HTTP server (stdlib only). Binds to 127.0.0.1 - each trader runs
their own instance against the shared data folder."""

from __future__ import annotations

import base64
import json
import logging
import queue
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .conventions import TENORS
from .service import Service
from .store import ConflictError, DefaultsConflict, LockTimeout

STATIC = Path(__file__).parent / "static"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}

log = logging.getLogger("volmarker")


def make_handler(svc: Service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        # ---- helpers --------------------------------------------------
        def _json(self, obj, status=HTTPStatus.OK):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _static(self, name: str):
            path = (STATIC / name).resolve()
            if STATIC.resolve() not in path.parents or not path.is_file():
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        # ---- routes ---------------------------------------------------
        def do_GET(self):
            url = urlparse(self.path)
            p = url.path
            if p == "/":
                return self._static("index.html")
            if p.startswith("/static/"):
                return self._static(p[len("/static/"):])
            if p == "/api/me":
                cfg = svc.store.config()
                return self._json({"user": svc.user, "data": str(svc.store.root), "tenors": TENORS,
                                   "cuts": cfg.get("cuts") or {}, "calendar_tz": cfg.get("calendar_tz")})
            if p == "/api/pairs":
                return self._json(svc.summaries())
            if p == "/api/prefs":
                return self._json(svc.prefs())
            if p == "/api/stream":
                return self._stream()
            m = re.fullmatch(r"/api/pairs/([A-Z]{6})", p)
            if m:
                v = svc.view(m.group(1))
                return self._json(v) if v else self._json({"error": "unknown pair"}, 404)
            m = re.fullmatch(r"/api/pairs/([A-Z]{6})/history", p)
            if m:
                limit = int(parse_qs(url.query).get("limit", ["50"])[0])
                return self._json(svc.store.history(m.group(1), limit))
            if p == "/api/alerts":
                return self._json({"alerts": svc.alerts.list(), "config": svc.store.config()["alerts"]})
            if p == "/api/market/status":
                return self._json(svc.feed_status())
            m = re.fullmatch(r"/api/pairs/([A-Z]{6})/market", p)
            if m:
                q = parse_qs(url.query)
                return self._json(svc.market_for(m.group(1), q.get("brand", [None])[0]))
            m = re.fullmatch(r"/api/pairs/([A-Z]{6})/curve", p)
            if m:
                return self._json(svc.curve(m.group(1)))
            m = re.fullmatch(r"/api/pairs/([A-Z]{6})/default-events", p)
            if m:
                return self._json(svc.pair_default_events(m.group(1), parse_qs(url.query).get("cut", [None])[0]))
            m = re.fullmatch(r"/api/pairs/([A-Z]{6})/weights-export", p)
            if m:
                return self._json(svc.weights_status(m.group(1)))
            if p == "/api/event-defaults":
                return self._json(svc.event_defaults())
            m = re.fullmatch(r"/api/pairs/([A-Z]{6})/spread", p)
            if m:
                q = parse_qs(url.query)
                try:
                    return self._json(svc.spread_history(
                        m.group(1), q.get("ref", [""])[0], q.get("tenor", ["1M"])[0],
                        float(q.get("days", ["5"])[0])))
                except ValueError as e:
                    return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            p = urlparse(self.path).path
            try:
                body = self._body()
                if p == "/api/prefs":
                    return self._json(svc.save_prefs(body))
                if p == "/api/event-defaults":
                    return self._json(svc.save_event_defaults(body.get("rows") or [], body.get("expected_version")))
                if p in ("/api/calendar/preview", "/api/calendar/import"):
                    data = base64.b64decode(body["data"])
                    if p.endswith("preview"):
                        return self._json(svc.bloomberg_preview(data, body.get("name", "")))
                    return self._json(svc.bloomberg_import(data, body.get("name", ""), body.get("selection"),
                                                           body.get("expected_version")))
                m = re.fullmatch(r"/api/pairs/([A-Z]{6})/weights-export", p)
                if m:
                    return self._json(svc.send_weights(m.group(1), body.get("username", ""), body.get("password", "")))
                m = re.fullmatch(r"/api/pairs/([A-Z]{6})/curve", p)
                if m:
                    return self._json(svc.curve(m.group(1), body.get("state")))
                m = re.fullmatch(r"/api/pairs/([A-Z]{6})/testdates", p)
                if m:
                    return self._json(svc.test_dates(m.group(1), body.get("dates") or [], body.get("state")))
                if p == "/api/alerts/ack":
                    return self._json({"acked": svc.ack_alerts(body.get("keys"), body.get("pair"))})
                if p == "/api/pairs":
                    rec = svc.create_pair(body["pair"], body.get("mode", "marked"),
                                          body.get("bases"), body.get("key", False),
                                          body.get("group"))
                    return self._json(rec)
                m = re.fullmatch(r"/api/pairs/([A-Z]{6})/preview", p)
                if m:
                    return self._json(svc.preview(m.group(1), body["state"]))
                m = re.fullmatch(r"/api/pairs/([A-Z]{6})/dailies", p)
                if m:
                    return self._json(svc.dailies(m.group(1), body.get("state"), body.get("brand")))
                m = re.fullmatch(r"/api/pairs/([A-Z]{6})/publish", p)
                if m:
                    rec = svc.publish(m.group(1), body["state"], body.get("expected_version"),
                                      body.get("comment", ""))
                    return self._json(rec)
            except DefaultsConflict as e:
                return self._json({"error": str(e), "latest": e.latest}, HTTPStatus.CONFLICT)
            except ConflictError as e:
                return self._json({"error": str(e), "latest": e.latest}, HTTPStatus.CONFLICT)
            except LockTimeout as e:
                return self._json({"error": str(e)}, HTTPStatus.SERVICE_UNAVAILABLE)
            except (ValueError, KeyError, TypeError) as e:
                return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def _stream(self):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = svc.subscribe()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=15)
                        msg = f"data: {json.dumps(ev)}\n\n"
                    except queue.Empty:
                        msg = ": ping\n\n"
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                svc.unsubscribe(q)

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            return  # browser tab closed mid-request - not an error
        super().handle_error(request, client_address)


def serve(svc: Service, port: int) -> ThreadingHTTPServer:
    for p in range(port, port + 20):
        try:
            httpd = _Server(("127.0.0.1", p), make_handler(svc))
            httpd.daemon_threads = True
            return httpd
        except OSError:
            continue
    raise OSError(f"No free port in {port}-{port + 19}")
