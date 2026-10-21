"""Flat-file store on the shared drive. No database, no server.

Layout under the data root:

    pairs/<PAIR>/history.jsonl   one JSON line per version, append-only
    journal.jsonl                one line per published version (all pairs);
                                 every running app tails this to see changes
    locks/<name>.lock            short-lived exclusive locks
    calendar/holidays.csv        ccy,date
    calendar/events.csv          ccy,date,description  (manual events, e.g. elections)
    calendar/bloomberg/<CCY>.json  last Bloomberg ECO import per currency
    calendar/event_defaults.csv  ccy,event,weight,ticker  (relevant events + default weights)
    config.json                  export directories etc.

Writers take the pair's lock, check that the latest version is the one the
edit started from, then append. Lines are never rewritten, so the history
doubles as the audit trail.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import socket
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

LOCK_TIMEOUT = 15.0     # seconds to wait for a lock
LOCK_STALE = 30.0       # a lock older than this is considered abandoned


class ConflictError(Exception):
    def __init__(self, latest: dict):
        super().__init__(f"{latest['pair']} was changed to v{latest['version']} "
                         f"by {latest.get('user')}")
        self.latest = latest


class LockTimeout(Exception):
    pass


DEFAULT_CONFIG = {
    "fenics_dir": "exports/fenics",
    "frontarena_dir": "exports/frontarena",
    "dc_spread_dir": "exports/dc_spread",      # the macro's DC_SPRD folder
    "history_dir": "exports/history",          # the macro's History folder
    "stale_minutes_key": 120,
    "stale_minutes_other": 1440,
    # Fenics Market Data FIX feed (password: FMD_PASSWORD env var or a local password_file)
    "fmd": {
        "transport": "fix",            # "fix" or "webapi"
        "host": "uat-ny-feed.fenicsmd.com",
        "port": 8935,
        "tls": True,
        "sender_comp_id": "",
        "target_comp_id": "BGCREG",
        "session_qualifier": None,
        "heartbeat": 60,
        "product": "fenicsMdFxo",
        "filters": [],
        "password_env": "FMD_PASSWORD",
        "password_file": None,
        "webapi": {
            "base_url": "https://uat-webapi.fenicsmd.com",
            "token_url": "",            # Keycloak token endpoint, provided by FMD
            "client_id": "",
            "client_secret_env": "FMD_CLIENT_SECRET",
            "client_secret_file": None,
            "path": "/webapi/subscribe",
            "params": {"assetClass": "FX", "subAssetClass": "OPTIONS"},
            "conflation": 1,
            "snapshot_age": 2,
            "idle_timeout": 120,
            "ca_file": None,
        },
    },
    # event weights: Fenics XML poster and the Front Arena event files (as the Excel weight workbook)
    "weights_export": {
        "fenics_url": "",          # the pricing server (as in the Excel macro's login form); /xmlposter is appended
        "fenics_dir": "exports/weights/fenics",                   # copy of the last message (no password)
        "fa_fx_dir": "exports/weights/FA_FX",
        "fa_ws_dir": "exports/weights/FA_WS",
        # the macro wrote the PC's regional date format: yyyy-mm-dd on the desk PCs in Oct 2026, dd.mm.yyyy before
        "date_format": "%Y-%m-%d",
    },
    # option expiry cuts: an event released after the cut weighs on the next expiry
    "cuts": {"NYC": {"tz": "America/New_York", "time": "10:00"},
             "TOK": {"tz": "Asia/Tokyo", "time": "15:00"}},
    "calendar_tz": "Europe/Zurich",     # time zone of the Bloomberg terminal that exports the calendar
    "fmd_brand": "auto",
    "fmd_daily_basis": "calendar",   # FXO convention: "ND" = today + N calendar days, rolled to a good day
    "market_stale_minutes": 30,
    # Market mode: pairs with "Follow FMD" get FMD mids published automatically by the feed process
    "follow": {
        "interval_s": 2,                # how often the follower looks at the market
        "min_change": 0.05,             # publish when any mark moved at least this much (vol pts)
        "min_publish_interval_s": 15,   # per pair
        "max_jump": {"atm": 1.0, "rr25": 0.75, "rr10": 1.5, "bf25": 0.5, "bf10": 1.0},
        "jump_confirm_s": 60,           # a bigger move is held until it has lasted this long
    },
    # Alerts when the market moves away from the published (manual or derived) surface
    "alerts": {
        "enabled": True,
        "basis": "mid",                 # "mid": |mark - market mid| > tol; "range": outside bid/offer by > tol
        "tolerance": {"atm": 0.25, "rr25": 0.20, "rr10": 0.40, "bf25": 0.10, "bf10": 0.25},
        "tenor_scale": {"ON": 2.0, "1W": 1.5, "2W": 1.25},
        "confirm_s": 20,                # a breach must last this long before it alerts
        "clear_ratio": 0.8,             # cleared again below clear_ratio * tol
    },
}


class Store:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        for sub in ("pairs", "locks", "calendar"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.journal_path = self.root / "journal.jsonl"
        self.journal_path.touch(exist_ok=True)
        cfg_path = self.root / "config.json"
        if not cfg_path.exists():
            atomic_write(cfg_path, json.dumps(DEFAULT_CONFIG, indent=2))

    # ---- config -------------------------------------------------------
    def config(self) -> dict:
        path = self.root / "config.json"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        cached = getattr(self, "_cfg_cache", None)
        if cached is None or cached[0] != mtime:
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            try:
                _merge(cfg, json.loads(path.read_text()))
            except (OSError, ValueError):
                pass
            cached = self._cfg_cache = (mtime, json.dumps(cfg))
        return json.loads(cached[1])

    def path_from_config(self, key: str) -> Path:
        p = Path(self.config()[key])
        return p if p.is_absolute() else self.root / p

    # ---- locking ------------------------------------------------------
    @contextmanager
    def lock(self, name: str, owner: str = ""):
        path = self.root / "locks" / f"{name}.lock"
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as f:
                    f.write(f"{owner}@{socket.gethostname()} {time.time()}")
                break
            except FileExistsError:
                try:
                    if time.time() - path.stat().st_mtime > LOCK_STALE:
                        path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise LockTimeout(f"Could not lock {name}")
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    # ---- pairs --------------------------------------------------------
    def _history(self, pair: str) -> Path:
        return self.root / "pairs" / pair / "history.jsonl"

    def list_pairs(self) -> list[str]:
        return sorted(p.name for p in (self.root / "pairs").iterdir()
                      if (p / "history.jsonl").exists())

    def latest(self, pair: str) -> dict | None:
        path = self._history(pair)
        if not path.exists():
            return None
        line = _last_line(path)
        return json.loads(line) if line else None

    def history(self, pair: str, limit: int = 100) -> list[dict]:
        path = self._history(pair)
        if not path.exists():
            return []
        return [json.loads(l) for l in _tail_lines(path, limit)]

    def append(self, record: dict, expected_version: int | None) -> dict:
        """Append a new version. Caller must hold the pair lock."""
        pair = record["pair"]
        latest = self.latest(pair)
        current = latest["version"] if latest else 0
        if expected_version is not None and expected_version != current:
            raise ConflictError(latest)
        record = dict(record, version=current + 1)
        path = self._history(pair)
        path.parent.mkdir(parents=True, exist_ok=True)
        _append_line(path, json.dumps(record, separators=(",", ":")))
        return record

    # ---- journal ------------------------------------------------------
    def journal(self, entry: dict) -> None:
        with self.lock("journal"):
            _append_line(self.journal_path, json.dumps(entry, separators=(",", ":")))

    def journal_size(self) -> int:
        return self.journal_path.stat().st_size

    def read_journal_from(self, offset: int) -> tuple[list[dict], int]:
        with open(self.journal_path, "rb") as f:
            f.seek(offset)
            data = f.read()
        # only consume complete lines
        end = data.rfind(b"\n") + 1
        entries = [json.loads(l) for l in data[:end].splitlines() if l.strip()]
        return entries, offset + end

    # ---- calendar -----------------------------------------------------
    def load_calendar(self) -> tuple[dict[str, set], list[dict]]:
        """Holidays per currency, and all calendar events: the manual ones
        (events.csv) and every event of the Bloomberg imports. Only events on
        the list of relevant events get a weight (calendar.rule_weight)."""
        d = self.root / "calendar"
        files = [d / "holidays.csv", d / "events.csv"] + sorted((d / "bloomberg").glob("*.json"))
        key = []
        for f in files:
            try:
                key.append((f.name, f.stat().st_mtime))
            except OSError:
                pass
        cached = getattr(self, "_cal_cache", None)
        if cached and cached[0] == key:
            return {c: set(v) for c, v in cached[1].items()}, list(cached[2])
        holidays, events = self._read_calendar()
        self._cal_cache = (key, holidays, events)
        return {c: set(v) for c, v in holidays.items()}, list(events)

    def _read_calendar(self) -> tuple[dict[str, set], list[dict]]:
        holidays: dict[str, set] = {}
        events: list[dict] = []
        hp = self.root / "calendar" / "holidays.csv"
        if hp.exists():
            with open(hp, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    holidays.setdefault(row["ccy"].upper(), set()).add(
                        date.fromisoformat(row["date"]))
        ep = self.root / "calendar" / "events.csv"
        if ep.exists():
            with open(ep, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    events.append({"ccy": row["ccy"].upper(), "date": row["date"],
                                   "desc": row["description"], "source": "manual"})
        for p in sorted((self.root / "calendar" / "bloomberg").glob("*.json")):
            try:
                imp = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for e in imp.get("events", []):
                events.append(dict(e, desc=e["name"], source="bloomberg"))
        return holidays, events

    def bloomberg_imports(self) -> dict[str, dict]:
        """Per currency: when the last Bloomberg import was made, by whom, from which file."""
        out = {}
        for p in sorted((self.root / "calendar" / "bloomberg").glob("*.json")):
            try:
                imp = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out[p.stem] = {k: imp.get(k) for k in ("ts", "user", "file", "downloaded", "source_tz")}
            out[p.stem]["events"] = len(imp.get("events", []))
        return out

    def save_bloomberg(self, ccy: str, payload: dict) -> None:
        atomic_write(self.root / "calendar" / "bloomberg" / f"{ccy}.json",
                     json.dumps(payload, separators=(",", ":")))


    # ---- desk default weights for recurring events -----------------
    def _defaults_path(self) -> Path:
        return self.root / "calendar" / "event_defaults.csv"

    def load_event_defaults(self) -> dict:
        """{"rows": [{ccy, event, weight}], "version": content hash, "user", "ts"}"""
        try:
            text = self._defaults_path().read_text(encoding="utf-8")
        except OSError:
            text = ""
        rows = []
        for r in csv.DictReader(io.StringIO(text)):
            try:
                rows.append({"ccy": (r.get("ccy") or "").strip().upper(),
                             "event": (r.get("event") or "").strip(), "weight": float(r["weight"]),
                             "ticker": (r.get("ticker") or "").strip()})
            except (KeyError, TypeError, ValueError):
                continue
        meta = {}
        log = self.root / "calendar" / "event_defaults.log.jsonl"
        if log.exists():
            line = _last_line(log)
            meta = json.loads(line) if line else {}
        return {"rows": rows, "version": hashlib.sha1(text.encode()).hexdigest()[:12],
                "user": meta.get("user"), "ts": meta.get("ts")}

    def save_event_defaults(self, rows: list[dict], expected_version: str | None,
                            user: str, ts: str) -> dict:
        clean = []
        for r in rows:
            event = " ".join(str(r.get("event") or "").split())
            ticker = " ".join(str(r.get("ticker") or "").split())
            if not event and not ticker:
                continue
            ccy = str(r.get("ccy") or "").strip().upper()
            if ccy not in ("", "*") and (len(ccy) != 3 or not ccy.isalpha()):
                raise ValueError(f"Bad currency {ccy!r} for {event}")
            w = float(r.get("weight"))
            if not -1 < w <= 10:
                raise ValueError(f"Weight for {event} must be between -1 and 10")
            clean.append({"ccy": ccy, "event": event, "weight": w, "ticker": ticker})
        with self.lock("event-defaults", user):
            cur = self.load_event_defaults()
            if expected_version is not None and expected_version != cur["version"]:
                raise DefaultsConflict(cur)
            buf = io.StringIO()
            wr = csv.DictWriter(buf, ["ccy", "event", "weight", "ticker"], lineterminator="\n")
            wr.writeheader()
            wr.writerows(clean)
            atomic_write(self._defaults_path(), buf.getvalue())
            _append_line(self.root / "calendar" / "event_defaults.log.jsonl",
                         json.dumps({"ts": ts, "user": user, "rows": clean}, separators=(",", ":")))
        return self.load_event_defaults()


class DefaultsConflict(Exception):
    def __init__(self, latest: dict):
        super().__init__(f"The desk defaults were changed by {latest.get('user')} at {latest.get('ts')}")
        self.latest = latest


def _merge(base: dict, over: dict) -> None:
    """Recursive merge: partial sections in config.json keep their defaults."""
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def _append_line(path: Path, line: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def _last_line(path: Path) -> str:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        chunk = 8192
        buf = b""
        pos = size
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            lines = buf.rstrip(b"\n").split(b"\n")
            if len(lines) > 1 or pos == 0:
                return lines[-1].decode("utf-8")
    return ""


def _tail_lines(path: Path, n: int) -> list[str]:
    """Last n non-empty lines, newest first, without reading the whole file
    (market-mode pairs get many versions a day)."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos, buf = f.tell(), b""
        while pos > 0 and buf.count(b"\n") <= n:
            step = min(65536, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
    lines = [l for l in buf.split(b"\n") if l.strip()]
    if pos > 0:
        lines = lines[1:]            # first one may be cut
    return [l.decode("utf-8") for l in reversed(lines[-n:])]


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + rename so readers (e.g. Fenics) never see a
    half-written file. Retries briefly on Windows sharing violations."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)
    os.replace(tmp, path)
