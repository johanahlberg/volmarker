"""The desk's vol history as flat tables for analysis (Excel, Python, R).

Layout under the analysis folder (config analysis_dir, default "analysis" in the data folder):

    marks/YYYY-MM.csv.gz          every snapshot of the month: one row per snapshot and tenor
    marks_eod/YYYY-MM.csv.gz      each pair's last snapshot of every day
    atm_eod_wide/YYYY-MM.csv.gz   the end-of-day ATM mid, one column per pair and tenor
    market/YYYY-MM/YYYY-MM-DD.csv.gz   the recorded FMD quotes of the day (changes only;
                                  the day starts with a full set)
    _state.json                   the last day exported, and the month files' sizes

Days and months are local days in calendar_tz. Sources, which are only read:

  * every VolMarker version of every pair, archive included;
  * the dated copies of the Fenics file from before VolMarker (<PAIR>_yyyymmdd_hhmmss.csv in
    history_dir, as the Excel macro wrote them), used only from before the pair's first
    VolMarker version. Their time is the PC's wall clock, read in calendar_tz;
  * the FMD quotes the leader records (Service.record_market).

backfill() writes everything up to a day, whole months at a time, and is what
scripts/export_history.py runs once. After that, the leader runs daily() each morning:
it appends each finished day to its month's files (a new gzip member, so nothing
written is read or rewritten), rebuilds the month's small wide table, and writes the
day's market file. The month files' sizes are recorded before each append, so a run
cut off halfway is cut back and redone by the next one.

Marks are mids: ATM bid/offer = mid -/+ spread / 2, without skew (the Excel-era files had
none, so their ATM mid is the middle of the file's bid and offer). RRs are call - put in
the pair's own terms: an inverse pair's Fenics RRs are flipped back by its current
Inverse setting.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .calendar import tenor_dates
from .conventions import TENORS
from .exports import FEN_TENORS
from .store import Store, _read_gz_lines, _reverse_lines, atomic_write
from .tz import localize, to_local

log = logging.getLogger(__name__)

MARK_COLS = ["time_utc", "time_local", "date", "pair", "tenor", "expiry", "atm_mid", "atm_bid", "atm_offer",
             "spread", "rr25", "rr10", "bf25", "bf10", "source", "user", "version"]
MARKET_COLS = ["time_utc", "time_local", "date", "pair", "tenor", "mark", "ptype", "brand",
               "bid", "offer", "mid", "quote_time", "full_set"]
FILE_RE = re.compile(r"^([A-Z]{6})_(\d{8})_(\d{6})\.csv$", re.I)
FEN_TENOR = {v: k for k, v in FEN_TENORS.items()}
FEN_MARK = {"s25": "bf25", "rr25": "rr25", "s10": "bf10", "rr10": "rr10"}
BACKFILL_HOURS = 12          # a back-fill younger than this holds off the daily run


def _num(x: str) -> float | None:
    x = x.strip()
    return float(x) if x else None


def _r(x) -> str:
    return "" if x is None else format(round(x, 6), ".10g")


def read_fenics_copy(text: str, inverse: bool) -> dict[str, dict]:
    """A Fenics vol file back into marks per tenor (mids; RRs in the pair's own terms)."""
    out: dict[str, dict] = {}
    for line in text.splitlines():
        f = line.strip().split(",")
        if len(f) < 7 or f[0] != "vol" or f[4] not in FEN_TENOR:
            continue
        t, bid, ask = FEN_TENOR[f[4]], _num(f[5]), _num(f[6])
        row = out.setdefault(t, {})
        if f[1] == "atm":
            if bid is not None and ask is not None:
                row.update(atm_mid=(bid + ask) / 2, atm_bid=bid, atm_offer=ask, spread=ask - bid)
        elif f[1] in FEN_MARK:
            v = bid if ask is None else ask if bid is None else (bid + ask) / 2
            if v is not None and inverse and f[1].startswith("rr"):
                v = -v
            row[FEN_MARK[f[1]]] = v
    return out


def version_marks(rec: dict) -> dict[str, dict]:
    out = {}
    for t in TENORS:
        r = rec["tenors"].get(t) or {}
        atm, spread = r.get("atm"), r.get("spread") or 0
        rr10 = r.get("rr10") if r.get("rr10") is not None else (r.get("rr25") or 0) * (r.get("rr10f") or 0)
        bf10 = r.get("bf10") if r.get("bf10") is not None else (r.get("bf25") or 0) * (r.get("bf10f") or 0)
        out[t] = {"atm_mid": atm, "atm_bid": None if atm is None else atm - spread / 2,
                  "atm_offer": None if atm is None else atm + spread / 2, "spread": spread,
                  "rr25": r.get("rr25"), "rr10": rr10, "bf25": r.get("bf25"), "bf10": bf10}
    return out


def _csv(rows: list[list], header: list[str] | None) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    if header:
        w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _read_csv_gz(path: Path) -> list[list[str]]:
    """All rows (header included) of a gzip CSV; a member cut off at the end is dropped."""
    out, buf = [], b""
    with gzip.open(path, "rb") as g:
        try:
            while True:
                chunk = g.read(1 << 20)
                if not chunk:
                    break
                buf += chunk
        except (OSError, EOFError):
            buf = buf[:buf.rfind(b"\n") + 1]
    return list(csv.reader(io.StringIO(buf.decode("utf-8"))))


class Exporter:
    def __init__(self, store: Store, out: Path | None = None, zone: str | None = None,
                 legacy: Path | None = None):
        cfg = store.config()
        self.store = store
        self.zone = zone or cfg.get("calendar_tz") or "Europe/Zurich"
        self.out = out or store.path_from_config_value(cfg.get("analysis_dir") or "analysis")
        if legacy is None and cfg.get("history_dir"):
            legacy = store.path_from_config("history_dir")
        self.legacy = legacy
        self.holidays, _ = store.load_calendar()
        self._exp: dict[tuple[str, date], dict] = {}
        self._first: dict[str, datetime | None] = {}
        self._legacy_idx: dict[str, list[tuple[datetime, Path]]] | None = None
        self.unreadable = 0

    # ---- sources ------------------------------------------------------
    def first_version(self, pair: str) -> datetime | None:
        """When the pair's VolMarker history starts (its first version)."""
        if pair not in self._first:
            line = None
            months = self.store._archive_months(pair)
            if months:
                lines = _read_gz_lines(months[0][1])
                line = lines[0] if lines else None
            if line is None:
                path = self.store._history(pair)
                try:
                    with open(path, "rb") as f:
                        line = f.readline().decode("utf-8") or None
                except OSError:
                    line = None
            self._first[pair] = datetime.fromisoformat(json.loads(line)["ts"]) if line else None
        return self._first[pair]

    def legacy_files(self) -> dict[str, list[tuple[datetime, Path]]]:
        """The dated Fenics copies per pair, by time (names only; read when used)."""
        if self._legacy_idx is None:
            idx: dict[str, list] = {}
            if self.legacy and self.legacy.is_dir():
                for e in os.scandir(self.legacy):
                    m = FILE_RE.match(e.name)
                    if m:
                        utc = localize(datetime.strptime(m.group(2) + m.group(3), "%Y%m%d%H%M%S"), self.zone)
                        idx.setdefault(m.group(1).upper(), []).append((utc, Path(e.path)))
            for v in idx.values():
                v.sort()
            self._legacy_idx = idx
        return self._legacy_idx

    def versions(self, pair: str, a: datetime | None, b: datetime) -> list[dict]:
        """The pair's VolMarker versions with a <= ts < b, oldest first, read from the
        end of the history backwards (so a recent day reads only its own versions)."""
        out, seen = [], set()
        sources = [_reverse_lines(self.store._history(pair))] if self.store._history(pair).exists() else []
        sources += [reversed(_read_gz_lines(f)) for _, f in reversed(self.store._archive_months(pair))]
        for lines in sources:
            for line in lines:
                rec = json.loads(line)
                ts = datetime.fromisoformat(rec["ts"])
                if a is not None and ts < a:
                    return sorted(out, key=lambda r: r["version"])
                if ts < b and rec["version"] not in seen:
                    seen.add(rec["version"])
                    out.append(rec)
        return sorted(out, key=lambda r: r["version"])

    def snapshots(self, pair: str, a: datetime | None, b: datetime):
        """(utc, marks, source, user, version) of the pair with a <= time < b, by time."""
        latest = self.store.latest(pair)
        inverse = bool(latest and latest.get("inverse"))
        first = self.first_version(pair)
        out = []
        for utc, path in self.legacy_files().get(pair, []):
            if (a is not None and utc < a) or utc >= b or (first is not None and utc >= first):
                continue
            try:
                marks = read_fenics_copy(path.read_text(encoding="utf-8-sig", errors="replace"), inverse)
            except OSError:
                marks = {}
            if marks:
                out.append((utc, marks, "excel-file", "", ""))
            else:
                self.unreadable += 1
                log.warning("analysis: %s unreadable, left out", path.name)
        for rec in self.versions(pair, a, b):
            out.append((datetime.fromisoformat(rec["ts"]), version_marks(rec), "volmarker:" + (rec.get("source") or ""),
                        rec.get("user") or "", str(rec["version"])))
        out.sort(key=lambda s: s[0])
        return out

    def pairs(self) -> list[str]:
        return sorted(set(self.store.list_pairs()) | set(self.legacy_files()))

    # ---- rows ---------------------------------------------------------
    def rows(self, pair: str, utc: datetime, marks: dict, source: str, user: str, version: str) -> tuple[date, list]:
        local = to_local(utc, self.zone)
        d = local.date()
        k = (pair, d)
        if k not in self._exp:
            try:
                self._exp[k] = tenor_dates(pair, d, self.holidays)
            except Exception:          # a pair the date conventions don't know
                self._exp[k] = {}
        exp = self._exp[k]
        out = []
        for t in TENORS:
            m = marks.get(t)
            if not m:
                continue
            e = exp.get(t)
            out.append([utc.strftime("%Y-%m-%d %H:%M:%S"), local.strftime("%Y-%m-%d %H:%M:%S"), d.isoformat(), pair, t,
                        e.isoformat() if e else "", _r(m.get("atm_mid")), _r(m.get("atm_bid")), _r(m.get("atm_offer")),
                        _r(m.get("spread")), _r(m.get("rr25")), _r(m.get("rr10")), _r(m.get("bf25")), _r(m.get("bf10")),
                        source, user, version])
        return d, out

    def day_start(self, d: date) -> datetime:
        return localize(datetime(d.year, d.month, d.day), self.zone)

    # ---- files --------------------------------------------------------
    def path(self, kind: str, month: str) -> Path:
        return self.out / kind / f"{month}.csv.gz"

    def write_wide(self, month: str) -> None:
        """The month's end-of-day ATMs side by side, from its marks_eod file."""
        src = self.path("marks_eod", month)
        if not src.exists():
            return
        rows = _read_csv_gz(src)[1:]
        pairs = sorted({r[3] for r in rows})
        cols = [f"{p} {t}" for p in pairs for t in TENORS]
        days: dict[str, dict] = {}
        for r in rows:
            days.setdefault(r[2], {})[f"{r[3]} {r[4]}"] = r[6]
        data = _csv([[d] + [days[d].get(c, "") for c in cols] for d in sorted(days)], ["date"] + cols)
        _atomic_bytes(self.path("atm_eod_wide", month), gzip.compress(data))

    def market_days(self, days: list[date]) -> int:
        """Write each day's recorded FMD quotes to market/YYYY-MM/YYYY-MM-DD.csv.gz."""
        cfg = self.store.config().get("market_record") or {}
        src = self.store.path_from_config_value(cfg.get("dir") or "market_history")
        if not days or not src.is_dir():
            return 0
        want = set(days)
        a, b = self.day_start(min(days)), self.day_start(max(days) + timedelta(days=1))
        months = sorted({f"{a:%Y-%m}", f"{b - timedelta(seconds=1):%Y-%m}"} |
                        {f"{self.day_start(d):%Y-%m}" for d in days})
        per_day: dict[date, list] = {d: [] for d in days}
        for m in months:
            f = src / f"{m}.jsonl.gz"
            if not f.exists():
                continue
            for row in _recorded_rounds(f):
                utc = datetime.fromisoformat(row["t"])
                if not a <= utc < b:
                    continue
                local = to_local(utc, self.zone)
                if local.date() not in want:
                    continue
                for tenor, mark, ptype, brand, bid, offer, mid, ts in row["q"]:
                    per_day[local.date()].append([
                        utc.strftime("%Y-%m-%d %H:%M:%S"), local.strftime("%Y-%m-%d %H:%M:%S"), local.date().isoformat(),
                        row["p"], tenor, mark, ptype, brand, _r(bid), _r(offer), _r(mid), ts or "", int(bool(row.get("full")))])
        n = 0
        for d, rows in per_day.items():
            if rows:
                _atomic_bytes(self.out / "market" / f"{d:%Y-%m}" / f"{d}.csv.gz", gzip.compress(_csv(rows, MARKET_COLS)))
                n += len(rows)
        return n

    # ---- state --------------------------------------------------------
    def state(self) -> dict:
        try:
            return json.loads((self.out / "_state.json").read_text())
        except (OSError, ValueError):
            return {}

    def save_state(self, st: dict) -> None:
        atomic_write(self.out / "_state.json", json.dumps(st, indent=1))

    # ---- back-fill ----------------------------------------------------
    def backfill(self, end: date, progress=print) -> dict:
        """Everything up to and including the local day `end`, written as whole month
        files (each month rewritten), plus the market day files. Run once, by hand."""
        self.out.mkdir(parents=True, exist_ok=True)
        st = self.state()
        self.save_state(dict(st, backfill_running=time.time()))
        b = self.day_start(end + timedelta(days=1))
        tmp: dict[tuple[str, str], gzip.GzipFile] = {}

        def writer(kind: str, month: str):
            k = (kind, month)
            if k not in tmp:
                p = self.path(kind, month)
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp[k] = gzip.open(p.with_name(f".{p.name}.tmp"), "wb")
                tmp[k].write(_csv([], MARK_COLS))
            return tmp[k]

        counts = {"pairs": 0, "snapshots": 0, "excel_files": 0}
        try:
            for pair in self.pairs():
                snaps = self.snapshots(pair, None, b)
                eod: dict[date, list] = {}
                for utc, marks, source, user, version in snaps:
                    d, rows = self.rows(pair, utc, marks, source, user, version)
                    writer("marks", f"{d:%Y-%m}").write(_csv(rows, None))
                    eod[d] = rows
                    counts["snapshots"] += 1
                    counts["excel_files"] += source == "excel-file"
                for d in sorted(eod):
                    writer("marks_eod", f"{d:%Y-%m}").write(_csv(eod[d], None))
                counts["pairs"] += 1
                progress(f"{pair}: {len(snaps)} snapshots")
        finally:
            for g in tmp.values():
                g.close()
        for (kind, month), g in tmp.items():
            p = self.path(kind, month)
            os.replace(p.with_name(f".{p.name}.tmp"), p)
        months = sorted({m for _, m in tmp})
        for m in months:
            self.write_wide(m)
        first_day = min((datetime.strptime(m, "%Y-%m").date() for m in months), default=end)
        days = [first_day + timedelta(days=i) for i in range((end - first_day).days + 1)]
        counts["market_quotes"] = self.market_days(days)
        cur = f"{end:%Y-%m}"
        self.save_state({"through": end.isoformat(), "month": cur, "sizes": self._sizes(cur)})
        counts["months"] = len(months)
        counts["unreadable"] = self.unreadable
        return counts

    def _sizes(self, month: str) -> dict:
        return {k: self.path(k, month).stat().st_size for k in ("marks", "marks_eod") if self.path(k, month).exists()}

    # ---- daily --------------------------------------------------------
    def daily(self, today: date | None = None) -> list[date]:
        """Each finished day since the last export (normally yesterday), appended to its
        month's files. Returns the days exported."""
        today = today or to_local(datetime.now(timezone.utc), self.zone).date()
        st = self.state()
        if st.get("backfill_running") and time.time() - st["backfill_running"] < BACKFILL_HOURS * 3600:
            return []
        through = date.fromisoformat(st["through"]) if st.get("through") else today - timedelta(days=2)
        done = []
        d = through + timedelta(days=1)
        while d < today:
            self.export_day(d, st)
            done.append(d)
            d += timedelta(days=1)
        return done

    def export_day(self, d: date, st: dict) -> None:
        month = f"{d:%Y-%m}"
        if st.get("month") and st["month"] != month:      # a new month: its files start empty
            st["sizes"] = {}
        sizes = st.get("sizes") if st.get("month") else None    # None: no state, cut nothing
        for kind in ("marks", "marks_eod"):   # cut back what an interrupted run left
            p = self.path(kind, month)
            if sizes is not None and p.exists() and p.stat().st_size != sizes.get(kind, 0):
                with open(p, "r+b") as f:
                    f.truncate(sizes.get(kind, 0))
        a, b = self.day_start(d), self.day_start(d + timedelta(days=1))
        marks, eod = [], []
        for pair in self.pairs():
            last = None
            for snap in self.snapshots(pair, a, b):
                _, rows = self.rows(pair, *snap)
                marks += rows
                last = rows
            if last:
                eod += last
        for kind, rows in (("marks", marks), ("marks_eod", eod)):
            p = self.path(kind, month)
            p.parent.mkdir(parents=True, exist_ok=True)
            new = not p.exists() or p.stat().st_size == 0
            if rows or new:
                with open(p, "ab") as f:      # a gzip member per day
                    f.write(gzip.compress(_csv(rows, MARK_COLS if new else None)))
                    f.flush()
                    os.fsync(f.fileno())
        self.write_wide(month)
        self.market_days([d])
        st.update(through=d.isoformat(), month=month, sizes=self._sizes(month))
        st.pop("backfill_running", None)
        self.save_state(st)


def _recorded_rounds(path: Path):
    """The recorder's month file, line by line; a round cut off at the end is skipped."""
    with gzip.open(path, "rt", encoding="utf-8") as g:
        try:
            for line in g:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
        except (OSError, EOFError):
            return


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
