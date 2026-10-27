"""Fenics login for this VolMarker: FMD 1.0 vols polled from the XML poster.

The trader logs in from the app (Fenics button). User and password are kept
in memory only, for as long as this VolMarker runs, and are also used to
send event weights. While logged in, the app runs the same feed as
`python -m volmarker.fmd feed --transport xml`: it writes market/<PAIR>.json
and market/_status.json for every app, and publishes Follow FMD pairs.
Only one feed may run per data folder, so a second login elsewhere is refused
while this one is alive.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from .fmd.feed import FEED_STALE, Feed, xml_url
from .fmd.xmlposter import NoVols
from .store import Store

log = logging.getLogger("volmarker.fenics")

DEFAULT_SECONDS = 300
MIN_SECONDS = 10


class FenicsLogin:
    def __init__(self, store: Store):
        self.store = store
        self.lock = threading.Lock()
        self.user: str | None = None
        self.password: str | None = None
        self.seconds: float = DEFAULT_SECONDS
        self.feed: Feed | None = None
        self.instance: str | None = None          # our last feed, so logging in again needn't wait for it to go stale

    def credentials(self) -> tuple[str, str] | None:
        return (self.user, self.password) if self.feed and self.password else None

    def status(self) -> dict:
        cfg = self.store.config()["fmd"]
        out = {"logged_in": self.feed is not None, "user": self.user, "seconds": self.seconds,
               "url": xml_url(cfg, self.store)}
        if self.feed:
            st = self.feed.status
            out.update(connected=st["connected"], last_msg=st["last_msg"], last_error=st["last_error"],
                       no_vols=st.get("no_vols") or [])
        return out

    def _other_feed(self) -> str | None:
        try:
            cur = json.loads((self.store.root / "market" / "_status.json").read_text())
        except (OSError, ValueError):
            return None
        alive = cur.get("connected") and time.time() - cur.get("heartbeat", 0) < FEED_STALE
        return cur.get("instance") if alive and cur.get("instance") != self.instance else None

    def login(self, user: str, password: str, seconds: float | None = None) -> dict:
        user = (user or "").strip()
        if not user or not password:
            raise ValueError("Fenics user and password needed")
        seconds = self._check_seconds(seconds or self.seconds)
        cfg = self.store.config()["fmd"]
        if not xml_url(cfg, self.store):
            raise ValueError("No Fenics server set (config.json: fmd.xml.url or weights_export.fenics_url)")
        with self.lock:
            self._stop_feed()
            other = self._other_feed()
            if other:
                raise ValueError(f"Another market data feed is running ({other}); only one per data folder")
            cfg = dict(cfg, transport="xml")
            cfg["xml"] = dict(cfg["xml"], username=user, interval=seconds)
            feed = Feed(self.store, cfg, password, force=True)
            self._check(feed)
            self.user, self.password, self.seconds = user, password, seconds
            self.feed, self.instance = feed, feed.instance
            threading.Thread(target=feed.run, name="fenics-feed", daemon=True).start()
        log.info("Fenics: %s logged in, fetching every %g s", user, seconds)
        return self.status()

    @staticmethod
    def _check(feed: Feed) -> None:
        """One request before starting, so a wrong password is reported at once.
        Tries a few pairs; an answer without vols still means the login worked."""
        errors, sess = [], feed.session
        for pair in sess.pairs()[:3]:
            try:
                feed.on_entries(sess.fetch(pair))
                return
            except NoVols:
                sess.missing[pair] = time.monotonic() + sess.retry_missing
                return
            except (OSError, ValueError) as e:
                errors.append(str(e))
        if errors:
            raise ValueError(f"Fenics login failed: {errors[0]}")

    @staticmethod
    def _check_seconds(seconds) -> float:
        seconds = float(seconds)
        if not seconds >= MIN_SECONDS:
            raise ValueError(f"Fetch at most every {MIN_SECONDS} seconds")
        return seconds

    def set_seconds(self, seconds: float) -> dict:
        seconds = self._check_seconds(seconds)
        with self.lock:
            self.seconds = seconds
            if self.feed:
                self.feed.session.set_interval(seconds)
        return self.status()

    def _stop_feed(self) -> None:
        if self.feed:
            self.feed.session.stop()
            self.feed = None

    def logout(self) -> dict:
        with self.lock:
            self._stop_feed()
            self.user = self.password = None
        return self.status()
