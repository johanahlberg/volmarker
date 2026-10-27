"""The feed process: one FMD session for the whole desk (FIX, WebAPI, or
FMD 1.0 polled from the Fenics XML poster).

Writes to the shared data folder:

    market/<PAIR>.json   normalised broker quotes for the pair (rewritten at
                         most once per flush interval, only when changed)
    market/_status.json  connection state + heartbeat, read by every app
    market/_follow.json  market-mode follower state (see volmarker/follow.py)

Only one feed may run per data folder - a second one refuses to start while
the first one's heartbeat is fresh.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..store import Store, atomic_write
from .book import QuoteBook
from .fix import FixSession
from .webapi import WebApiSession
from .xmlposter import XmlPosterSession

log = logging.getLogger("volmarker.fmd")

FLUSH_SECONDS = 1.0
FEED_STALE = 15.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_secret(env: str | None, file: str | None, what: str) -> str:
    """Secrets are never stored in the shared folder: env var, or a file local to this PC."""
    val = os.environ.get(env) if env else None
    if val:
        return val
    if file:
        p = Path(os.path.expandvars(os.path.expanduser(file)))
        if p.exists():
            return p.read_text().strip()
    raise SystemExit(f"No {what}: set {env} or the matching *_file setting in config.json")


def xml_url(cfg: dict, store: Store) -> str:
    """The Fenics pricing server; by default the one event weights are sent to."""
    return cfg["xml"].get("url") or store.config()["weights_export"].get("fenics_url") or ""


def read_password(cfg: dict) -> str:
    if cfg.get("transport") == "xml":
        x = cfg["xml"]
        return read_secret(x.get("password_env") or "FENICS_PASSWORD", x.get("password_file"), "Fenics password")
    if cfg.get("transport") == "webapi":
        w = cfg["webapi"]
        return read_secret(w.get("client_secret_env") or "FMD_CLIENT_SECRET",
                           w.get("client_secret_file"), "FMD WebAPI client secret")
    return read_secret(cfg.get("password_env") or "FMD_PASSWORD", cfg.get("password_file"),
                       "FMD FIX password")


class Feed:
    def __init__(self, store: Store, cfg: dict, password: str, force: bool = False,
                 follow: bool = True):
        self.store, self.cfg = store, cfg
        self.follow = follow
        self.dir = store.root / "market"
        self.dir.mkdir(exist_ok=True)
        self.book = QuoteBook()
        self.lock = threading.Lock()
        self.dirty: set[str] = set()
        self.instance = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        transport = cfg.get("transport", "fix")
        host = (cfg["webapi"]["base_url"] if transport == "webapi" else xml_url(cfg, store) if transport == "xml"
                else f"{cfg['host']}:{cfg['port']}")
        self.status = {"instance": self.instance, "transport": transport, "host": host,
                       "connected": False, "since": None, "last_msg": None, "msgs": 0,
                       "entries": 0, "last_error": None, "started": now_iso()}
        if not force:
            self._check_single_instance()
        if transport == "xml":
            x = cfg["xml"]
            self.session = XmlPosterSession(
                host, x["username"], password, pairs=lambda: x.get("pairs") or store.list_pairs(),
                scenario=x.get("scenario", "fenicsVols"), interval=x.get("interval", 60),
                brand=x.get("brand", "FMD1"), atm_ptype=x.get("atm_type", "DNS"), timeout=x.get("timeout", 20),
                on_entries=self.on_entries, on_status=self.on_status)
            return
        if transport == "webapi":
            w = cfg["webapi"]
            self.session = WebApiSession(
                w["base_url"], w["token_url"], w["client_id"], password, path=w.get("path", "/webapi/subscribe"),
                params=w.get("params"), conflation=w.get("conflation", 1),
                snapshot_age=w.get("snapshot_age", 2), idle_timeout=w.get("idle_timeout", 120),
                ca_file=w.get("ca_file"), on_entries=self.on_entries, on_status=self.on_status)
            return
        filters = [(int(t), v) for t, v in cfg.get("filters", [])]
        if cfg.get("product"):
            filters.insert(0, (10460, cfg["product"]))
        self.session = FixSession(
            cfg["host"], int(cfg["port"]), cfg["sender_comp_id"], password,
            target=cfg.get("target_comp_id", "BGCREG"), heartbeat=int(cfg.get("heartbeat", 60)),
            use_tls=bool(cfg.get("tls", True)), session_qualifier=cfg.get("session_qualifier"),
            subscription=filters, on_entries=self.on_entries, on_status=self.on_status)

    def _check_single_instance(self) -> None:
        try:
            cur = json.loads((self.dir / "_status.json").read_text())
        except (OSError, ValueError):
            return
        if time.time() - cur.get("heartbeat", 0) < FEED_STALE:
            raise SystemExit(f"Another FMD feed is running ({cur.get('instance')}). "
                             "Only one feed per data folder; use --force to override.")

    def on_entries(self, entries: list[dict]) -> None:
        with self.lock:
            for e in entries:
                pair = self.book.apply(e)
                if pair:
                    self.dirty.add(pair)
            self.status["msgs"] += 1
            self.status["entries"] += len(entries)
            self.status["last_msg"] = now_iso()

    def on_status(self, s: dict) -> None:
        with self.lock:
            if s.get("connected") and not self.status["connected"]:
                self.status["since"] = now_iso()
            self.status.update(s)

    def flush(self) -> None:
        with self.lock:
            dirty, self.dirty = self.dirty, set()
            snaps = {p: self.book.pair_quotes(p) for p in dirty}
            status = dict(self.status, heartbeat=time.time(), pairs=len(
                {q.get("pair") for q in self.book.by_id.values()}))
        for pair, quotes in snaps.items():
            atomic_write(self.dir / f"{pair}.json",
                         json.dumps({"pair": pair, "updated": now_iso(), "quotes": quotes}))
        atomic_write(self.dir / "_status.json", json.dumps(status))

    def run(self) -> None:
        stop = threading.Event()

        def flusher():
            while not stop.wait(FLUSH_SECONDS):
                try:
                    self.flush()
                except OSError as e:
                    log.warning("could not write market data: %s", e)

        threading.Thread(target=flusher, daemon=True).start()
        if self.follow:
            # market mode: this process is the one that publishes FMD levels for "Follow FMD" pairs
            from ..follow import Follower
            from ..service import Service
            fsvc = Service(self.store, "FMD")
            fsvc.start_propagation()          # follower publishes don't wait for the crosses to be recomputed
            follower = Follower(fsvc)
            threading.Thread(target=follower.run_forever, args=(stop,), daemon=True).start()
        try:
            self.session.run_forever()
        finally:
            stop.set()
            self.session.stop()
            with self.lock:
                self.status.update(connected=False, last_error="feed stopped")
            self.flush()
