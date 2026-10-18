"""Minimal FIXT 1.1 / FIX 5.0 SP2 codec and market-data session client,
per the "Fenics Market Data FIX Feed Spec - fenicsMdFxo" (Jan 2026).

Standard library only (socket + ssl). The session:

    connect (TLS 1.2) -> Logon (A) -> MarketDataRequest (V) -> receive
    MarketDataIncrementalRefresh (X), answer TestRequest (1) with Heartbeat (0),
    send our own Heartbeats, reconnect with back-off on any failure.

FMD does not describe resend/recovery, so after a disconnect we simply log
on again with sequence number 1 and re-subscribe.
"""

from __future__ import annotations

import logging
import socket
import ssl
import time
from datetime import datetime, timezone
from typing import Callable, Iterable

log = logging.getLogger("volmarker.fmd")

SOH = "\x01"
BEGIN = "FIXT.1.1"

# tags
MSG_TYPE, SENDER, TARGET, SEQ, SENDING_TIME = 35, 49, 56, 34, 52
TEXT, TEST_REQ_ID = 58, 112
NO_MD_ENTRIES, MD_UPDATE_ACTION = 268, 279
HEADER_TAGS = {8, 9, 35, 49, 56, 34, 52, 1128, 43, 97, 122}


def fix_time(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y%m%d-%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def encode(msg_type: str, body: Iterable[tuple[int, object]], sender: str, target: str,
           seq: int, sending_time: str | None = None) -> bytes:
    fields = [(MSG_TYPE, msg_type), (SENDER, sender), (TARGET, target), (SEQ, seq),
              (SENDING_TIME, sending_time or fix_time())] + list(body)
    payload = "".join(f"{t}={v}{SOH}" for t, v in fields)
    head = f"8={BEGIN}{SOH}9={len(payload.encode())}{SOH}"
    msg = (head + payload).encode()
    return msg + f"10={sum(msg) % 256:03d}{SOH}".encode()


class FixParser:
    """Incremental parser: feed bytes, get complete messages as (tag, value) lists."""

    def __init__(self):
        self.buf = b""

    def feed(self, data: bytes) -> list[list[tuple[int, str]]]:
        self.buf += data
        out = []
        while True:
            start = self.buf.find(b"8=")
            if start < 0:
                self.buf = b""
                return out
            if start:
                self.buf = self.buf[start:]
            p9 = self.buf.find(b"\x019=")
            if p9 < 0:
                return out
            end9 = self.buf.find(b"\x01", p9 + 1)
            if end9 < 0:
                return out
            try:
                body_len = int(self.buf[p9 + 3:end9])
            except ValueError:          # garbage - resync on the next "8="
                self.buf = self.buf[2:]
                continue
            body_end = end9 + 1 + body_len
            trailer_end = self.buf.find(b"\x01", body_end)
            if trailer_end < 0:
                return out
            raw = self.buf[:trailer_end + 1]
            self.buf = self.buf[trailer_end + 1:]
            if not raw[body_end:].startswith(b"10="):
                log.warning("FIX message with bad body length dropped")
                continue
            if int(raw[body_end + 3:-1]) != sum(raw[:body_end]) % 256:
                log.warning("FIX checksum mismatch - message dropped")
                continue
            out.append(parse_fields(raw))


def parse_fields(raw: bytes) -> list[tuple[int, str]]:
    fields = []
    for part in raw.decode("utf-8", "replace").split(SOH):
        if "=" in part:
            t, v = part.split("=", 1)
            if t.isdigit():
                fields.append((int(t), v))
    return fields


def first(fields: list[tuple[int, str]], tag: int, default=None):
    for t, v in fields:
        if t == tag:
            return v
    return default


def md_entries(fields: list[tuple[int, str]]) -> list[dict[int, str]]:
    """Split a MarketDataIncrementalRefresh (X) into its repeating-group
    entries. Each entry starts at MDUpdateAction (279)."""
    entries, cur, in_group = [], None, False
    for t, v in fields:
        if t == NO_MD_ENTRIES:
            in_group = True
            continue
        if not in_group or t == 10:
            continue
        if t == MD_UPDATE_ACTION:
            cur = {t: v}
            entries.append(cur)
        elif cur is not None:
            cur[t] = v
    return entries


class FixSession:
    """Market-data initiator with heartbeats and automatic reconnect."""

    def __init__(self, host: str, port: int, sender: str, password: str,
                 target: str = "BGCREG", heartbeat: int = 60, use_tls: bool = True,
                 session_qualifier: str | None = None,
                 subscription: Iterable[tuple[int, object]] = (),
                 on_entries: Callable[[list[dict[int, str]]], None] = lambda e: None,
                 on_status: Callable[[dict], None] = lambda s: None):
        self.host, self.port, self.sender, self.password = host, port, sender, password
        self.target, self.heartbeat, self.use_tls = target, heartbeat, use_tls
        self.session_qualifier = session_qualifier
        self.subscription = list(subscription)
        self.on_entries, self.on_status = on_entries, on_status
        self.sock: socket.socket | None = None
        self.out_seq = 0
        self.in_seq = 0
        self.running = True

    # ---- plumbing -------------------------------------------------------
    def _send(self, msg_type: str, body: Iterable[tuple[int, object]] = ()) -> None:
        self.out_seq += 1
        data = encode(msg_type, body, self.sender, self.target, self.out_seq)
        self.sock.sendall(data)
        self.last_sent = time.monotonic()

    def _connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=15)
        if self.use_tls:
            ctx = ssl.create_default_context()
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
        else:
            self.sock = raw
        self.sock.settimeout(1.0)
        self.out_seq = self.in_seq = 0
        self.parser = FixParser()
        self.last_sent = self.last_recv = time.monotonic()
        self.test_req_sent = None

    def _close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _recv(self) -> list[list[tuple[int, str]]]:
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return []
        if not data:
            raise ConnectionError("connection closed by FMD")
        self.last_recv = time.monotonic()
        return self.parser.feed(data)

    # ---- session --------------------------------------------------------
    def logon(self) -> None:
        body = [(98, 0), (108, self.heartbeat), (554, self.password), (1137, 9)]
        if self.session_qualifier:
            body.append((9012, self.session_qualifier))
        self._send("A", body)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            for msg in self._recv():
                mt = first(msg, MSG_TYPE)
                if mt == "A":
                    return
                if mt == "5":
                    raise ConnectionError(f"logon rejected: {first(msg, TEXT, 'no reason given')}")
        raise ConnectionError("no logon response within 20s")

    def subscribe(self) -> None:
        body = [(262, f"volmarker-{int(time.time())}"), (263, 1), (146, 1), (55, "*")]
        body += self.subscription
        self._send("V", body)

    def _handle(self, msg: list[tuple[int, str]]) -> None:
        mt = first(msg, MSG_TYPE)
        seq = int(first(msg, SEQ, "0") or 0)
        if self.in_seq and seq and seq != self.in_seq + 1:
            log.warning("FMD sequence gap: expected %s, got %s", self.in_seq + 1, seq)
        self.in_seq = seq or self.in_seq
        if mt == "X":
            self.on_entries(md_entries(msg))
        elif mt == "1":
            self._send("0", [(TEST_REQ_ID, first(msg, TEST_REQ_ID, ""))])
        elif mt == "0":
            self.test_req_sent = None
        elif mt == "5":
            raise ConnectionError(f"logout from FMD: {first(msg, TEXT, '')}")
        elif mt == "3":
            log.error("FMD rejected a message: %s", first(msg, TEXT, msg))
            self.on_status({"last_error": f"Reject: {first(msg, TEXT, '')}"})

    def _keepalive(self) -> None:
        now = time.monotonic()
        if now - self.last_sent >= self.heartbeat:
            self._send("0")
        silent = now - self.last_recv
        if self.test_req_sent is None and silent > self.heartbeat + 10:
            self.test_req_sent = now
            self._send("1", [(TEST_REQ_ID, f"TR{int(now)}")])
        elif self.test_req_sent is not None and now - self.test_req_sent > 20:
            raise ConnectionError(f"no data from FMD for {silent:.0f}s")

    def run_once(self) -> None:
        """Connect, log on, subscribe and pump messages until failure/stop."""
        self._connect()
        try:
            self.logon()
            log.info("FMD logon OK (%s:%s as %s)", self.host, self.port, self.sender)
            self.on_status({"connected": True, "last_error": None})
            self.subscribe()
            while self.running:
                for msg in self._recv():
                    self._handle(msg)
                self._keepalive()
            try:
                self._send("5")
            except OSError:
                pass
        finally:
            self._close()
            self.on_status({"connected": False})

    def run_forever(self) -> None:
        delays = [1, 2, 5, 10, 30, 60]
        failures = 0
        while self.running:
            started = time.monotonic()
            try:
                self.run_once()
            except (OSError, ConnectionError, ssl.SSLError) as e:
                log.warning("FMD session ended: %s", e)
                self.on_status({"connected": False, "last_error": str(e)})
            if not self.running:
                break
            failures = 0 if time.monotonic() - started > 120 else failures + 1
            delay = delays[min(failures, len(delays) - 1)]
            log.info("reconnecting to FMD in %ss", delay)
            time.sleep(delay)

    def stop(self) -> None:
        self.running = False
