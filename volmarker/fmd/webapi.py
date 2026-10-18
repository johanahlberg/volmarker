"""Fenics Market Data WebAPI client: Server-Sent Events (JSON) over HTTPS,
per "Fenics WebAPI how to guide" v2.0.

    OAuth2 client-credentials token (Keycloak) -> GET <base_url><path>?<filters>
    with Accept: text/event-stream -> "DataMessage" events: a full image per
    instrument first, then deltas with only the changed fields + sym.

Standard library only (urllib). Corporate proxies are picked up from the
HTTPS_PROXY environment variable; a TLS-inspecting proxy needs its CA in
ca_file. Each JSON message is converted to the same entry format as the
FIX feed, so the QuoteBook and everything after it is shared.
"""

from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

log = logging.getLogger("volmarker.fmd")

# WebAPI JSON field (lower-case) -> FIX tag used by QuoteBook
JSON_TAGS = {
    "sym": 48, "symbol": 48,
    "bid": 132, "ask": 133, "offer": 133, "mid": 631,
    "pricetype": 423, "price_type": 423,
    "tenor": 6217, "brand": 6960,
    "ccypair": 17001, "ccy1": 16999, "ccy2": 17000,
    "cutoff": 17033, "cut": 17033,
    "processtime": 60, "transacttime": 60,
}


def json_to_entry(obj: dict) -> dict[int, str] | None:
    entry = {279: "1"}
    for k, v in obj.items():
        tag = JSON_TAGS.get(k.lower())
        if tag and v is not None and v != "":
            entry[tag] = str(v)
    return entry if 48 in entry else None


class SSEParser:
    """Tolerant SSE line parser. Besides standard "data:" lines it accepts
    JSON continued over plain lines, as printed in the FMD guide."""

    def __init__(self):
        self.event, self.data = None, []

    def line(self, line: str) -> tuple[str | None, str] | None:
        line = line.rstrip("\r\n")
        if line == "":
            if not self.data:
                self.event = None
                return None
            out = (self.event, "\n".join(self.data))
            self.event, self.data = None, []
            return out
        if line.startswith(":"):
            return None                       # comment / keep-alive
        if line.startswith("event:"):
            self.event = line[6:].strip()
        elif line.startswith("data:"):
            self.data.append(line[5:].lstrip())
        elif line.startswith(("id:", "retry:")):
            pass
        else:
            self.data.append(line)            # continuation line
        return None


class WebApiSession:
    def __init__(self, base_url: str, token_url: str, client_id: str, client_secret: str,
                 path: str = "/webapi/subscribe", params: dict | None = None,
                 conflation: float = 1, snapshot_age: int = 2, idle_timeout: float = 120,
                 ca_file: str | None = None,
                 on_entries: Callable[[list[dict[int, str]]], None] = lambda e: None,
                 on_status: Callable[[dict], None] = lambda s: None):
        self.base_url, self.token_url = base_url.rstrip("/"), token_url
        self.client_id, self.client_secret = client_id, client_secret
        self.path, self.params = path, dict(params or {})
        self.conflation, self.snapshot_age, self.idle_timeout = conflation, snapshot_age, idle_timeout
        self.ctx = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
        self.on_entries, self.on_status = on_entries, on_status
        self.running = True
        self._token, self._token_exp = None, 0.0
        self._resp = None

    def _open(self, req, timeout):
        if req.full_url.startswith("https:"):
            return urllib.request.urlopen(req, timeout=timeout, context=self.ctx)
        return urllib.request.urlopen(req, timeout=timeout)

    def token(self) -> str:
        if self._token and time.time() < self._token_exp - 30:
            return self._token
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.client_id,
                                       "client_secret": self.client_secret}).encode()
        req = urllib.request.Request(self.token_url, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self._open(req, 20) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise ConnectionError(f"token request failed: HTTP {e.code}") from e
        self._token = data["access_token"]
        self._token_exp = time.time() + float(data.get("expires_in", 300))
        return self._token

    def url(self) -> str:
        q = dict(self.params)
        q.setdefault("_conflation", self.conflation)
        q.setdefault("_snapshot_age", self.snapshot_age)    # latest price even after a gap
        return f"{self.base_url}{self.path}?{urllib.parse.urlencode(q)}"

    def run_once(self) -> None:
        req = urllib.request.Request(self.url(), headers={
            "Authorization": f"Bearer {self.token()}", "Accept": "text/event-stream",
            "Cache-Control": "no-cache"})
        try:
            self._resp = self._open(req, self.idle_timeout)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                self._token = None                    # refresh on the next attempt
            raise ConnectionError(f"subscribe failed: HTTP {e.code}") from e
        log.info("FMD WebAPI connected (%s)", self.base_url)
        self.on_status({"connected": True, "last_error": None})
        parser = SSEParser()
        try:
            for raw in self._resp:
                if not self.running:
                    break
                ev = parser.line(raw.decode("utf-8", "replace"))
                if not ev:
                    continue
                name, text = ev
                if name not in (None, "DataMessage"):
                    log.debug("ignored SSE event %s", name)
                    continue
                try:
                    obj = json.loads(text)
                except ValueError:
                    log.warning("unparseable WebAPI message: %.200s", text)
                    continue
                items = obj if isinstance(obj, list) else [obj]
                entries = [e for e in (json_to_entry(o) for o in items if isinstance(o, dict)) if e]
                if entries:
                    self.on_entries(entries)
            if self.running:
                raise ConnectionError("WebAPI stream ended")
        finally:
            try:
                self._resp.close()
            except Exception:
                pass
            self._resp = None
            self.on_status({"connected": False})

    def run_forever(self) -> None:
        delays = [1, 2, 5, 10, 30, 60]
        failures = 0
        while self.running:
            started = time.monotonic()
            try:
                self.run_once()
            except (OSError, ConnectionError, ssl.SSLError, KeyError, ValueError) as e:
                log.warning("FMD WebAPI session ended: %s", e)
                self.on_status({"connected": False, "last_error": str(e)})
            if not self.running:
                break
            failures = 0 if time.monotonic() - started > 120 else failures + 1
            delay = delays[min(failures, len(delays) - 1)]
            log.info("reconnecting to FMD WebAPI in %ss", delay)
            time.sleep(delay)

    def stop(self) -> None:
        self.running = False
        if self._resp is not None:
            try:
                self._resp.close()
            except Exception:
                pass
