"""Flat-file store on the shared drive. No database, no server.

Layout under the data root:

    pairs/<PAIR>/history.jsonl   one JSON line per version, append-only
    pairs/<PAIR>/archive/YYYY-MM.jsonl.gz  older versions by month (UTC), moved
                                 there by archive_pair; read as part of the history
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
doubles as the audit trail. Archiving moves whole old months out of
history.jsonl into compressed month files and never drops a version: they are
written (merged by version) before history.jsonl is shortened, so a crash in
between leaves a version in both places, which readers skip, never in neither.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import logging
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from .derived import upgrade_corr

LOCK_TIMEOUT = 15.0     # seconds to wait for a lock
LOCK_STALE = 30.0       # a lock seen unchanged this long is considered abandoned
CLOCK_WARN_S = 5.0      # warn when this PC's clock is further than this off the file server's
LOCK_STALE_MTIME = 300.0  # ... or whose file is this old by the share's clock (another PC's
                          # clock may be off from the file server's by more than LOCK_STALE)


class ConflictError(Exception):
    def __init__(self, latest: dict):
        super().__init__(f"{latest['pair']} was changed to v{latest['version']} "
                         f"by {latest.get('user')}")
        self.latest = latest


class LockTimeout(Exception):
    pass


log = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "fenics_dir": "exports/fenics",
    "frontarena_dir": "exports/frontarena",
    "dc_spread_dir": "exports/dc_spread",      # the macro's DC_SPRD folder
    "history_dir": "exports/history",          # the macro's History folder: a dated copy per manual publish ("" = none)
    "excel_dir": "",                           # where Excel writes the files of pairs marked in Excel (empty: frontarena_dir)
    "frontarena_ccy": {"CNH": "CNY"},          # currencies Front Arena knows under another code (its files only, not Fenics)
    "archive_keep_days": 90,                   # history.jsonl keeps at least this many days; older whole months go to pairs/<PAIR>/archive/
    "stale_minutes_key": 120,
    "stale_minutes_other": 1440,
    # Fenics Market Data FIX feed (password: FMD_PASSWORD env var or a local password_file)
    "fmd": {
        "transport": "fix",            # "fix", "webapi" or "xml" (FMD 1.0 from the Fenics XML poster)
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
        # FMD 1.0 as fed into Fenics, polled like the Excel query fetchFenicsVols (RATE_FETCH get_vols)
        "xml": {
            "url": "",                  # the pricing server, /xmlposter is appended; empty = weights_export.fenics_url
            "username": "",
            "password_env": "FENICS_PASSWORD",
            "password_file": None,
            "scenario": "fenicsVols",
            "interval": 300,            # seconds between rounds over all pairs (the app's Fenics login sets its own)
            "pairs": [],                # empty = every pair in VolMarker
            "brand": "FMD1",            # the brand its quotes show under
            "atm_type": "DNS",          # convention of Fenics' ATM
            "timeout": 20,
            "scale": 100,               # Fenics answers in decimals (0.0605); x100 gives vol points, as the workbook
            "retry_missing": 3600,      # a pair Fenics has no vols for is asked again after this many seconds
            "raw_dir": "",              # e.g. "exports/fenics_vols": keep the last answer per pair (<PAIR>.xml) to debug
        },
    },
    # Bloomberg (volmarker/bbgfeed.py): started from the app's Bloomberg button on a PC with a
    # Terminal and the blpapi package; fetches only the pairs whose Setup prefers Bloomberg and
    # writes them to market/bbg/ for every app. A run stops itself at the next stop_at time
    # (calendar_tz) to keep within the Terminal's download limits; anyone can start it again.
    "bbg": {
        "source": "BGN",               # pricing source in the tickers (BGN, BVOL, CMPN, a broker's code)
        "pair_source": {},             # per pair, e.g. {"USDPLN": "BVOL"}
        "tickers": {                   # {pair}: the standard pair (CHFNOK for an inverse NOKCHF), {delta}: 25, 10
            "atm": "{pair}V{tenor} {source} Curncy",
            "rr": "{pair}{delta}R{tenor} {source} Curncy",
            "bf": "{pair}{delta}B{tenor} {source} Curncy",
        },
        "fields": {"bid": "PX_BID", "offer": "PX_ASK", "mid": "PX_MID"},
        "atm_type": "DNS",             # convention of Bloomberg's ATM
        "batch": 200,                  # tickers per request
        "stop_at": ["18:00", "00:00"],  # a run stops at the next of these (calendar_tz); [] = never
        "quiet_hours": ["18:00", "07:00"],  # Bloomberg not running then is expected: no follow alert
    },
    # event weights: Fenics XML poster and the Front Arena event files (as the Excel weight workbook)
    "weights_export": {
        "fenics_url": "",          # the pricing server (as in the Excel macro's login form); /xmlposter is appended
        "fenics_dir": "exports/weights/fenics",                   # copy of the last message (no password)
        "fa_fx_dir": "exports/weights/FA_FX",
        "fa_ws_dir": "exports/weights/FA_WS",
        # the macro wrote the PC's regional date format: yyyy-mm-dd on the desk PCs in Oct 2026, dd.mm.yyyy before
        "date_format": "%Y-%m-%d",
        # Fenics' event calendar is per currency: on a pair whose cut is not NYC, an event the cut
        # moves to another expiry is filed under the pair's other currency (false: as it is)
        "cut_events_other_ccy": True,
    },
    # derived pairs and pairs moving with their parent, after an FMD-driven (or catch-up) move:
    # republish only when an ATM moves at least min_change (or, on a cross whose ATMs stay put,
    # the implied correlation at least min_corr_change), at most every min_interval_s
    "propagation": {"min_change": 0.005, "min_corr_change": 0.005, "min_interval_s": 5},
    # option expiry cuts: an event released after the cut weighs on the next expiry
    # ("times" overrides the time on some weekdays)
    "cuts": {"NYC": {"tz": "America/New_York", "time": "10:00"},
             "TOK": {"tz": "Asia/Tokyo", "time": "15:00"},
             "NY930": {"tz": "America/New_York", "time": "09:30"},
             "LDN": {"tz": "Europe/London", "time": "12:00"},
             "MEX": {"tz": "America/New_York", "time": "12:30"},
             "PTAX": {"tz": "America/Sao_Paulo", "time": "13:15"},
             "WAR": {"tz": "Europe/Warsaw", "time": "11:00"},
             "BUD": {"tz": "Europe/Budapest", "time": "12:00"},
             "TLV": {"tz": "Asia/Jerusalem", "time": "15:00", "times": {"Fri": "12:00"}}},
    "calendar_tz": "Europe/Zurich",     # time zone of the Bloomberg terminal that exports the calendar
    "fmd_brand": "auto",
    "fmd_daily_basis": "calendar",   # FXO convention: "ND" = today + N calendar days, rolled to a good day
    "market_stale_minutes": 30,
    # FMD history for analysis: the leader appends the quotes that changed since its last round
    # to <dir>/YYYY-MM.jsonl.gz (UTC month), every interval_min (0 = off); brands: which to keep
    "market_record": {"interval_min": 5, "brands": ["FMD", "FMD1", "BBG"], "dir": "market_history"},
    # tables for analysis (volmarker/analysis.py): the leader appends each finished day ("" = off)
    "analysis_dir": "analysis",
    # Market mode: pairs with "Follow market" get the mids of their market source (FMD or
    # Bloomberg) published automatically by a feed process
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
        self.config_error: str | None = None     # why config.json was not (fully) used, shown in the app
        for sub in ("pairs", "locks", "calendar"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self._lock_seen: dict[Path, tuple[str, float]] = {}   # lock file -> (content, first seen)
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
            error = None
            try:
                own = json.loads(path.read_text(encoding="utf-8-sig"))
                unknown = sorted(set(own) - set(DEFAULT_CONFIG)) if isinstance(own, dict) else []
                _merge(cfg, own)
                if unknown:
                    error = f"config.json: unknown setting(s) {', '.join(unknown)}, ignored"
            except OSError:
                pass
            except ValueError as e:
                # the usual cause: a Windows path with single backslashes ("S:\Matrix\...")
                error = (f"config.json could not be read ({e}), so every setting is at its default. "
                         "Write Windows paths with double backslashes (\"S:\\\\Matrix\\\\IXCR_IHD\") "
                         "or forward slashes (\"S:/Matrix/IXCR_IHD\")")
            if error and error != self.config_error:
                log.warning("%s", error)
            self.config_error = error
            cached = self._cfg_cache = (mtime, json.dumps(cfg))
        return json.loads(cached[1])

    def path_from_config(self, key: str) -> Path:
        return self.path_from_config_value(self.config()[key])

    def path_from_config_value(self, value: str) -> Path:
        """A folder from config.json: absolute, or relative to the data folder."""
        p = Path(value)
        return p if p.is_absolute() else self.root / p

    # ---- locking ------------------------------------------------------
    @contextmanager
    def lock(self, name: str, owner: str = ""):
        path = self.root / "locks" / f"{name}.lock"
        mine = f"{owner}@{socket.gethostname()} {time.time()} {uuid.uuid4().hex}"
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as f:
                    f.write(mine)
                break
            except FileExistsError:
                if self._lock_stale(path):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if time.monotonic() > deadline:
                    raise LockTimeout(f"Could not lock {name}")
                time.sleep(0.05)
        try:
            yield
        finally:
            try:                         # only our own: a lock taken over as stale is someone else's now
                if path.read_text() == mine:
                    path.unlink()
            except OSError:
                pass

    def clock_offset(self, max_age: float = 300.0) -> float | None:
        """Seconds this PC's clock is behind the file server's (negative: ahead), from
        the time the server gives a file just written; None if it can't be told.
        Measured at most every max_age seconds; logs a warning when it is large."""
        cached = getattr(self, "_clock", None)
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1]
        probe = self.root / "locks" / f".clock-{socket.gethostname()}-{os.getpid()}.tmp"
        try:
            probe.parent.mkdir(parents=True, exist_ok=True)
            before = time.time()
            probe.write_text("clock")
            off = probe.stat().st_mtime - (before + time.time()) / 2
            probe.unlink()
        except OSError:
            return cached[1] if cached else None
        off = round(off, 1)
        if abs(off) > CLOCK_WARN_S and (not cached or abs(cached[1]) <= CLOCK_WARN_S):
            log.warning("This PC's clock is %.0f s %s the file server's: set the PC's time "
                        "(VolMarker copes, but times shown may be off)", abs(off), "behind" if off > 0 else "ahead of")
        self._clock = (time.monotonic(), off)
        return off

    def _lock_stale(self, path: Path) -> bool:
        """Abandoned (its holder crashed): unchanged for LOCK_STALE seconds by this
        PC's own clock, or very old by the share's. Not judged from the file time
        alone, which comes from the file server's clock, not this PC's."""
        try:
            content, mtime = path.read_text(), path.stat().st_mtime
        except OSError:
            return False                 # gone, or being written: try again
        if time.time() - mtime > LOCK_STALE_MTIME:
            return True
        now = time.monotonic()
        seen = self._lock_seen.get(path)
        if seen is None or seen[0] != content:
            self._lock_seen[path] = (content, now)
            if len(self._lock_seen) > 1000:
                self._lock_seen.clear()
            return False
        if now - seen[1] > LOCK_STALE:
            self._lock_seen.pop(path, None)
            return True
        return False

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
        return upgrade_corr(json.loads(line)) if line else None

    def history(self, pair: str, limit: int = 100) -> list[dict]:
        """The last limit versions, newest first (from the archive too when needed)."""
        path = self._history(pair)
        if not path.exists():
            return []
        out = [upgrade_corr(json.loads(l)) for l in _tail_lines(path, limit)]
        if len(out) < limit:
            seen = {r["version"] for r in out}
            for _, f in reversed(self._archive_months(pair)):
                for line in reversed(_read_gz_lines(f)):
                    rec = json.loads(line)
                    if rec["version"] not in seen:
                        seen.add(rec["version"])
                        out.append(upgrade_corr(rec))
                        if len(out) >= limit:
                            return out
        return out

    def as_of(self, pair: str, before: datetime) -> dict | None:
        """The last version published before a moment (e.g. the previous day's EOD)."""
        path = self._history(pair)
        if not path.exists():
            return None
        for line in _reverse_lines(path):
            rec = upgrade_corr(json.loads(line))
            if datetime.fromisoformat(rec["ts"]) < before:
                return rec
        month = _utc_month(before)
        for m, f in reversed(self._archive_months(pair)):
            if m > month:
                continue
            for line in reversed(_read_gz_lines(f)):
                rec = json.loads(line)
                if datetime.fromisoformat(rec["ts"]) < before:
                    return upgrade_corr(rec)
        return None

    # ---- archive ------------------------------------------------------
    def _archive_months(self, pair: str) -> list[tuple[str, Path]]:
        """(YYYY-MM, file) of the pair's archive, oldest first."""
        d = self.root / "pairs" / pair / "archive"
        if not d.is_dir():
            return []
        return sorted((f.name[:7], f) for f in d.glob("????-??.jsonl.gz"))

    def archive_due(self, pair: str, cutoff: datetime) -> bool:
        """Whether the pair's oldest version in history.jsonl is before the cutoff (reads one line)."""
        path = self._history(pair)
        try:
            with open(path, "rb") as f:
                first = f.readline()
        except OSError:
            return False
        return bool(first.strip()) and datetime.fromisoformat(json.loads(first)["ts"]) < cutoff

    def archive_pair(self, pair: str, cutoff: datetime) -> int:
        """Move the versions before the cutoff to the monthly archive files.
        history.jsonl always keeps the latest version. Caller must hold the pair lock.
        Returns the number of versions moved."""
        path = self._history(pair)
        if not path.exists():
            return 0
        lines = [l for l in path.read_text(encoding="utf-8").split("\n") if l.strip()]
        n = 0
        while n < len(lines) - 1 and datetime.fromisoformat(json.loads(lines[n])["ts"]) < cutoff:
            n += 1
        if not n:
            return 0
        by_month: dict[str, list[str]] = {}
        for l in lines[:n]:
            by_month.setdefault(_utc_month(datetime.fromisoformat(json.loads(l)["ts"])), []).append(l)
        d = path.parent / "archive"
        d.mkdir(exist_ok=True)
        for month, new in by_month.items():
            f = d / f"{month}.jsonl.gz"
            merged = {json.loads(l)["version"]: l for l in (_read_gz_lines(f) if f.exists() else [])}
            merged.update((json.loads(l)["version"], l) for l in new)
            tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
            with gzip.open(tmp, "wt", encoding="utf-8") as g:
                g.write("".join(merged[v] + "\n" for v in sorted(merged)))
            with open(tmp, "rb") as g:                 # read back before history.jsonl lets go of them
                if len(_gz_lines(g.read())) != len(merged):
                    raise OSError(f"archive {f} did not read back")
            os.replace(tmp, f)
        atomic_write(path, "".join(l + "\n" for l in lines[n:]))
        return n

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
        entries = []
        for l in data[:end].splitlines():
            if l.strip():
                try:
                    entries.append(json.loads(l))
                except ValueError:       # a damaged line must not stall the journal for good
                    log.warning("journal: skipped an unreadable line at offset %d", offset)
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
            try:
                w = float(r.get("weight"))
            except (TypeError, ValueError):
                raise ValueError(f"{event or ticker}: type a weight") from None
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
    """The last complete line. A last piece without its newline is normally a line
    another PC is still appending (on a share it can show up in parts): skipped
    unless it is whole JSON (a file written without a final newline)."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        buf = b""
        while pos > 0:
            step = min(8192, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            lines = buf.split(b"\n")
            tail = lines[-1]
            if tail.strip() and _parses(tail):
                return tail.decode("utf-8")
            done = lines[:-1] if pos == 0 else lines[1:-1]    # lines[0] may be cut off
            for l in reversed(done):
                if l.strip():
                    return l.decode("utf-8")
    return ""


def _parses(line: bytes) -> bool:
    try:
        json.loads(line)
        return True
    except ValueError:
        return False


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


def _utc_month(t: datetime) -> str:
    return (t.astimezone(timezone.utc) if t.tzinfo else t).strftime("%Y-%m")


def _gz_lines(data: bytes) -> list[str]:
    return [l for l in gzip.decompress(data).decode("utf-8").split("\n") if l.strip()]


def _read_gz_lines(path: Path) -> list[str]:
    """An archive month's versions, oldest first."""
    return _gz_lines(path.read_bytes())


def _reverse_lines(path: Path):
    """Non-empty lines from the end of the file backwards, read in chunks."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos, rest = f.tell(), b""
        while pos > 0:
            step = min(65536, pos)
            pos -= step
            f.seek(pos)
            lines = (f.read(step) + rest).split(b"\n")
            rest = lines.pop(0)              # may be cut: completed by the next chunk
            for l in reversed(lines):
                if l.strip():
                    yield l.decode("utf-8")
        if rest.strip():
            yield rest.decode("utf-8")


def write_shared(path: Path, text: str) -> None:
    """A small file every app polls (market data, feed and follower status),
    rewritten every second or so. Replaced atomically when possible; but
    Windows refuses to replace a file another PC on the share has open
    ("Access is denied"), and with every app reading it that is common, so it
    is then written in place. A reader that catches it half-written gets
    invalid JSON and simply reads it again on its next poll."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def atomic_write(path: Path, text: str, fsync: bool = True) -> None:
    """Write via a temp file + rename so readers (e.g. Fenics) never see a
    half-written file. Retries briefly on Windows sharing violations.
    fsync=False skips the flush to disk (export files: the rename alone keeps
    readers safe, and they are rewritten on the next publish anyway)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        if fsync:
            os.fsync(f.fileno())
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)
    os.replace(tmp, path)
