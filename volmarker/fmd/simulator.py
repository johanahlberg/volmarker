"""A stand-in FMD FIX server for development and demos (plain TCP, no TLS).

It accepts a logon, a MarketDataRequest, then streams
MarketDataIncrementalRefresh (X) messages shaped like the spec: a full
image first (all static fields), then price-only updates keyed by
SecurityID. Levels are seeded from the marks in the data folder plus a
random offset, then random-walk.
"""

from __future__ import annotations

import logging
import random
import socket
import threading
import time

from datetime import date

from ..calendar import daily_dates, interpolate_atm, with_defaults
from ..conventions import TENORS, split_pair
from ..store import Store
from ..surface import resolve
from .fix import FixParser, encode, first, fix_time

log = logging.getLogger("volmarker.fmd.sim")

SPREADS = {"DNS": 0.30, "ATMF": 0.30, "RR25": 0.30, "RR10": 0.50, "S25": 0.20, "S10": 0.40}
FIELD = {"DNS": "atm", "ATMF": "atm", "RR25": "rr25", "RR10": "rr10", "S25": "bf25", "S10": "bf10"}
BRANDS = ["FMD", "BGC", "GFI"]


class Instrument:
    def __init__(self, pair, tenor, ptype, brand, mid):
        self.pair, self.tenor, self.ptype, self.brand, self.mid = pair, tenor, ptype, brand, mid
        self.sid = f"FMD.FX.OPT.{pair}.{tenor}.{ptype}.{brand}"
        self.spread = SPREADS[ptype] * (1.0 if brand == "FMD" else 1.3)

    def entry(self, action: str, full: bool) -> list[tuple[int, object]]:
        bid, offer = self.mid - self.spread / 2, self.mid + self.spread / 2
        e = [(279, action), (48, self.sid), (22, 101),
             (132, f"{bid:.4f}"), (133, f"{offer:.4f}"), (631, f"{self.mid:.4f}"), (60, fix_time())]
        if full:
            c1, c2 = split_pair(self.pair)
            e += [(423, self.ptype), (1938, "FX"), (6217, self.tenor), (6960, self.brand),
                  (16958, "OPTIONS"), (16999, c1), (17000, c2), (17001, self.pair), (17033, "NY")]
        return e


def build_instruments(store: Store, seed: int | None = None) -> list[Instrument]:
    rnd = random.Random(seed)
    out = []
    for pair in store.list_pairs():
        rec = store.latest(pair)
        res = resolve(rec)
        for t in TENORS:
            mid = res[t]["mid"]
            if mid["atm"] is None:
                continue
            for ptype, field in FIELD.items():
                base = mid[field] + (0.02 if ptype == "ATMF" else 0.0)
                off = rnd.gauss(0, 0.15 if field == "atm" else 0.08)
                for brand in BRANDS[: 1 + rnd.randint(0, 2)]:
                    out.append(Instrument(pair, t, ptype, brand, base + off + rnd.gauss(0, 0.03)))
        out += daily_instruments(store, pair, rec, res, rnd)
    return out


def daily_instruments(store: Store, pair: str, rec: dict, res: dict, rnd: random.Random) -> list:
    """1D..14D dailies (FMD composite only). The market prices more on event
    days than the desk's weights do (+0.4, and 0.9 for calendar events the
    pair has no weight for), so a calibration has something to find."""
    holidays, cal = store.load_calendar()
    cfg = store.config()
    cut = (cfg.get("cuts") or {}).get(rec.get("cut") or "NYC")
    w = with_defaults(pair, rec.get("weights"), cal, store.load_event_defaults()["rows"], holidays, cut)
    events = [dict(e, weight=float(e["weight"]) + 0.4) for e in w["events"]]
    have = {e["date"] for e in events}
    events += [{"date": e["date"], "weight": 0.9} for e in cal      # manual events nobody weighted
               if e.get("source") == "manual" and e["ccy"] in split_pair(pair) and e["date"] not in have]
    w["events"] = events
    today = date.today()
    dd = daily_dates(pair, today, list(range(1, 15)), holidays,
                     store.config().get("fmd_daily_basis", "calendar"))
    atm = {t: res[t]["mid"]["atm"] for t in TENORS}
    vols = interpolate_atm(pair, atm, today, list(dd.values()), w, holidays)
    off = rnd.gauss(0, 0.1)
    out = []
    for n, d in dd.items():
        if d not in vols:
            continue
        for ptype in ("DNS", "ATMF"):
            out.append(Instrument(pair, f"{n}D", ptype, "FMD", vols[d] + off + (0.02 if ptype == "ATMF" else 0)))
        out.append(Instrument(pair, f"{n}D", "RR25", "FMD", res["ON"]["mid"]["rr25"]))
        out.append(Instrument(pair, f"{n}D", "S25", "FMD", res["ON"]["mid"]["bf25"]))
    return out


def serve_client(conn: socket.socket, store: Store, password: str | None, tick: float) -> None:
    parser, seq = FixParser(), 0
    sender = "BGCREG"
    client = None

    def send(mt, body=()):
        nonlocal seq
        seq += 1
        conn.sendall(encode(mt, body, sender, client or "CLIENT", seq))

    conn.settimeout(0.2)
    subscribed, last_tick, last_hb = False, 0.0, time.monotonic()
    instruments: list[Instrument] = []
    try:
        while True:
            try:
                data = conn.recv(65536)
                if not data:
                    return
                msgs = parser.feed(data)
            except socket.timeout:
                msgs = []
            for msg in msgs:
                mt = first(msg, 35)
                if mt == "A":
                    client = first(msg, 49)
                    if password and first(msg, 554) != password:
                        send("5", [(98, 0), (58, "Invalid password")])
                        return
                    send("A", [(98, 0), (108, first(msg, 108, "60")), (1137, 9)])
                    log.info("simulator: %s logged on", client)
                elif mt == "V":
                    instruments = build_instruments(store)
                    for i in range(0, len(instruments), 20):     # full image
                        chunk = instruments[i:i + 20]
                        body = [(268, len(chunk))]
                        for ins in chunk:
                            body += ins.entry("0", full=True)
                        send("X", body)
                    subscribed = True
                    log.info("simulator: subscribed, %d instruments", len(instruments))
                elif mt == "1":
                    send("0", [(112, first(msg, 112, ""))])
                elif mt == "5":
                    send("5", [(98, 0)])
                    return
            now = time.monotonic()
            if subscribed and instruments and now - last_tick > tick:
                last_tick = now
                pillars = [i for i in instruments if i.tenor in TENORS]
                moved = random.sample(pillars, k=min(len(pillars), random.randint(1, 6)))
                body = [(268, len(moved))]
                for ins in moved:
                    ins.mid += random.gauss(0, 0.02)
                    body += ins.entry("1", full=False)
                send("X", body)
            if now - last_hb > 30:
                last_hb = now
                send("0")
    except OSError:
        pass
    finally:
        conn.close()


def run(store: Store, port: int = 8935, password: str | None = None, tick: float = 0.5) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen()
    log.info("FMD simulator listening on 127.0.0.1:%d", port)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=serve_client, args=(conn, store, password, tick), daemon=True).start()


# ---- WebAPI (SSE) simulator -------------------------------------------------

def _json_full(ins: Instrument) -> dict:
    c1, c2 = split_pair(ins.pair)
    bid, ask = ins.mid - ins.spread / 2, ins.mid + ins.spread / 2
    return {"bid": f"{bid:.4f}", "ask": f"{ask:.4f}", "mid": f"{ins.mid:.4f}",
            "assetClass": "FX", "subAssetClass": "OPTIONS", "ccy1": c1, "ccy2": c2,
            "ccyPair": ins.pair, "tenor": ins.tenor, "priceType": ins.ptype, "brand": ins.brand,
            "cutOff": "NY", "sym": ins.sid, "processTime": _iso_now()}


def _json_delta(ins: Instrument) -> dict:
    bid, ask = ins.mid - ins.spread / 2, ins.mid + ins.spread / 2
    return {"ask": f"{ask:.4f}", "mid": f"{ins.mid:.4f}", "bid": f"{bid:.4f}",
            "sym": ins.sid, "processTime": _iso_now()}


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_webapi_handler(store: Store, secret: str | None, tick: float):
    import json as _json
    from http.server import BaseHTTPRequestHandler

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send_json(self, code, obj):
            body = _json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            form = dict(p.split("=", 1) for p in self.rfile.read(n).decode().split("&") if "=" in p)
            if secret and form.get("client_secret") != secret:
                return self._send_json(401, {"error": "invalid_client"})
            self._send_json(200, {"access_token": "simtoken", "expires_in": 300, "token_type": "Bearer"})

        def do_GET(self):
            if self.headers.get("Authorization") != "Bearer simtoken":
                return self._send_json(401, {"error": "unauthorized"})
            snapshot_only = self.path.startswith("/webapi/snapshot")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            instruments = build_instruments(store)

            def emit(obj):
                self.wfile.write(f"event:DataMessage\ndata:{_json.dumps(obj)}\n\n".encode())

            try:
                for ins in instruments:
                    emit(_json_full(ins))
                self.wfile.flush()
                if snapshot_only:
                    return
                pillars = [i for i in instruments if i.tenor in TENORS]
                last_ka = time.monotonic()
                while True:
                    time.sleep(tick)
                    for ins in random.sample(pillars, k=min(len(pillars), random.randint(1, 6))):
                        ins.mid += random.gauss(0, 0.02)
                        emit(_json_delta(ins))
                    if time.monotonic() - last_ka > 15:
                        self.wfile.write(b": keep-alive\n\n")
                        last_ka = time.monotonic()
                    self.wfile.flush()
            except OSError:
                pass
            self.close_connection = True

    return H


def run_webapi(store: Store, port: int = 8936, secret: str | None = None, tick: float = 0.5):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_webapi_handler(store, secret, tick))
    srv.daemon_threads = True
    log.info("FMD WebAPI simulator on http://127.0.0.1:%d (token: POST /token)", port)
    srv.serve_forever()


# ---- Fenics XML poster (FMD 1.0) simulator ----------------------------------

UNIT_WORDS = {"D": "Day", "W": "Week", "M": "Month", "Y": "Year"}
XML_TYPES = {"DNS": ("ATM", ""), "RR25": ("RR", "0.25"), "RR10": ("RR", "0.10"), "S25": ("S", "0.25"), "S10": ("S", "0.10")}


def _maturity(tenor: str) -> str:
    n, unit = (1, "D") if tenor == "ON" else (int(tenor[:-1]), tenor[-1])
    return f"{n} {UNIT_WORDS[unit]}{'s' if n > 1 else ''}"


def get_vols_answer(instruments: list[Instrument], pair: str) -> str:
    """A get_vols answer shaped as the Excel query reads it: body/data/node/node[field...]."""
    from xml.sax.saxutils import quoteattr
    nodes = []
    for i, ins in enumerate(x for x in instruments if x.pair == pair and x.ptype in XML_TYPES and x.tenor in TENORS):
        vtype, delta = XML_TYPES[ins.ptype]
        vol = f"{ins.mid - ins.spread / 2:.4f}/{ins.mid + ins.spread / 2:.4f}"
        nodes.append(f'<node name="{i}"><field name="Maturity" value={quoteattr(_maturity(ins.tenor))}/>'
                     f'<field name="VolType" value="{vtype}"/><field name="PctDelta" value="{delta}"/>'
                     f'<field name="Volity" value="{vol}"/></node>')
    c1, c2 = split_pair(pair)
    return (f'<?xml version="1.0" encoding="UTF-8"?><gfi_message version="2.0"><header><status>OK</status></header>'
            f'<body><data name="vols" format="NAME_VALUE"><node name="{c1}/{c2}">{"".join(nodes)}</node></data></body></gfi_message>')


def make_xml_handler(store: Store, password: str | None):
    import urllib.parse
    import xml.etree.ElementTree as ET
    from http.server import BaseHTTPRequestHandler
    instruments = [i for i in build_instruments(store) if i.brand == "FMD"]
    lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, text: str, code: int = 200):
            body = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(n).decode())
            try:
                msg = ET.fromstring(form["xml"][0])
                f = {x.get("name"): x.get("value") for x in msg.iter("field")}
                pair = (f["Currency"] + f["CtrCcy"]).upper()
            except (KeyError, ET.ParseError):
                return self._send("<gfi_message><body><error>Error: bad request</error></body></gfi_message>")
            if password and msg.findtext("header/password") != password:
                return self._send("<gfi_message><body><error>Error: invalid username or password</error></body></gfi_message>")
            with lock:
                for ins in instruments:
                    if ins.pair == pair:
                        ins.mid += random.gauss(0, 0.02)
                self._send(get_vols_answer(instruments, pair))

    return H


def run_xml(store: Store, port: int = 8937, password: str | None = None):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_xml_handler(store, password))
    srv.daemon_threads = True
    log.info("Fenics XML poster simulator on http://127.0.0.1:%d/xmlposter", port)
    srv.serve_forever()
