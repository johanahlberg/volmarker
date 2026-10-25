"""The per-pair sheets of the desk's Excel weight workbook (Events USD.xlsm):
one sheet per pair, the pair in B2 ("EUR/USD"):

  B16:C23   "Weekday" / "Weight", then Monday..Sunday and their weights
  B25:C26   "<CCY> holiday" and the pair's holiday factor for that currency
  O..R, U..X  one table per currency (header row 2: <CCY>, Event, Weight, Override),
            one row per date: Weight = the Override if one is typed, else the
            sum of the desk weights of that date's events (G..I / K..M list
            every event with its desk weight)
  O165, U165  "<CCY> holidays": that currency's holidays (date, factor, override)

Dates are Excel serials and already the expiry date an event weighs on.
"""

from __future__ import annotations

import re
import zipfile
from datetime import date, timedelta
from xml.etree import ElementTree as ET

from .calendar import WEEKDAYS

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n



def read_cells(path_or_file, sheet_names: list[str] | None = None) -> dict[str, dict[tuple[int, int], str]]:
    """{sheet: {(row, col): value}} with the real row and column numbers (1-based)."""
    z = zipfile.ZipFile(path_or_file)
    strings = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_NS + "si"):
            strings.append("".join(t.text or "" for t in si.iter(_NS + "t")))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    out = {}
    for sh in ET.fromstring(z.read("xl/workbook.xml")).iter(_NS + "sheet"):
        name = sh.get("name")
        if sheet_names is not None and name not in sheet_names:
            continue
        target = rels[sh.get(_REL)].lstrip("/")
        cells = {}
        for c in ET.fromstring(z.read(target if target.startswith("xl/") else "xl/" + target)).iter(_NS + "c"):
            m = re.fullmatch(r"([A-Z]+)(\d+)", c.get("r") or "")
            v, t = c.find(_NS + "v"), c.get("t")
            if not m:
                continue
            if t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(_NS + "t"))
            elif v is not None and v.text is not None:
                val = strings[int(v.text)] if t == "s" else v.text
            else:
                continue
            cells[(int(m.group(2)), _col_index(m.group(1)))] = val
        out[name] = cells
    return out


def _serial(x: str | None) -> date | None:
    try:
        n = float(x)
    except (TypeError, ValueError):
        return None
    return date(1899, 12, 30) + timedelta(days=int(n)) if n > 0 else None


def _num(x: str | None) -> float | None:
    try:
        return round(float(x), 6)
    except (TypeError, ValueError):
        return None


def parse_pair_sheet(cells: dict[tuple[int, int], str]) -> dict | None:
    """A pair sheet's weights, or None if the sheet isn't one:
    {"pair", "weekdays": [7], "holiday": {ccy: factor},
     "dates": {ccy: [{"date", "desc", "weight", "override"}]},
     "holidays": {ccy: [date]}, "holiday_overrides": {ccy: [(date, value)]}}"""
    m = re.fullmatch(r"\s*([A-Z]{3})/([A-Z]{3})\s*", cells.get((2, 2)) or "")
    if not m:
        return None
    c1, c2 = m.groups()
    g = lambda r, c: (cells.get((r, c)) or "").strip()
    out = {"pair": c1 + c2, "ccys": (c1, c2), "weekdays": None, "holiday": {}, "dates": {},
           "holidays": {}, "holiday_overrides": {}}
    rows = sorted({r for r, _ in cells})
    for r in rows:
        if g(r, 2).lower() == "weekday":
            names = [g(r + 1 + i, 2) for i in range(7)]
            vals = [_num(cells.get((r + 1 + i, 3))) for i in range(7)]
            if names != WEEKDAYS or None in vals:
                raise ValueError(f"{c1}/{c2}: weekday table under row {r} isn't Monday..Sunday with weights")
            out["weekdays"] = vals
        for ccy in (c1, c2):
            if g(r, 2) == f"{ccy} holiday" and _num(cells.get((r, 3))) is not None:
                out["holiday"][ccy] = _num(cells.get((r, 3)))
    max_col = max(c for _, c in cells)
    # the full event lists (date, event, weight; no Override column): names per date
    names: dict[str, dict[date, list[str]]] = {}
    for c in range(1, max_col + 1):
        ccy = g(2, c)
        if ccy in (c1, c2) and g(2, c + 1) == "Event" and g(2, c + 2) == "Weight" and g(2, c + 3) != "Override":
            for r in rows:
                d = _serial(cells.get((r, c))) if r > 2 else None
                if d and g(r, c + 1):
                    names.setdefault(ccy, {}).setdefault(d, []).append(g(r, c + 1))
    # the per-date tables (date, event, weight, override) and the holiday tables below them
    for c in range(1, max_col + 1):
        ccy = g(2, c)
        if not (ccy in (c1, c2) and g(2, c + 1) == "Event" and g(2, c + 2) == "Weight" and g(2, c + 3) == "Override"):
            continue
        table, hol_row = [], None
        for r in rows:
            if r <= 2:
                continue
            if g(r, c) == f"{ccy} holidays":
                hol_row = r
                break
            d = _serial(cells.get((r, c)))
            w = _num(cells.get((r, c + 2)))
            if d and w is not None:
                listed = names.get(ccy, {}).get(d) or [g(r, c + 1)]
                table.append({"date": d.isoformat(), "desc": ", ".join(n for n in listed if n)[:80],
                              "weight": w, "override": _num(cells.get((r, c + 3)))})
        out["dates"][ccy] = table
        if hol_row:
            hol, ovr = [], []
            for r in rows:
                if r <= hol_row:
                    continue
                d = _serial(cells.get((r, c)))
                if not d:
                    if g(r, c) == "":
                        continue
                    break
                hol.append(d)
                if _num(cells.get((r, c + 3))) is not None:
                    ovr.append((d.isoformat(), _num(cells.get((r, c + 3)))))
            out["holidays"][ccy] = hol
            out["holiday_overrides"][ccy] = ovr
    if out["weekdays"] is None:
        raise ValueError(f"{c1}/{c2}: no Weekday / Weight table in column B")
    return out


def pair_events(sheet: dict, today: date, all_dates: bool = False) -> list[dict]:
    """The pair's own events for VolMarker, from today on.

    all_dates=False: only dates with an Override in Excel. A pair event replaces
    the desk defaults of its whole date, so on such a date the other currency's
    weight is taken over too (Excel overrides per currency).
    all_dates=True: every date with its Excel weight, an exact copy of the sheet."""
    start = today.isoformat()
    by_date: dict[str, dict[str, dict]] = {}
    for ccy, rows in sheet["dates"].items():
        for e in rows:
            if e["date"] >= start:
                by_date.setdefault(e["date"], {})[ccy] = e
    out = []
    for d, per in sorted(by_date.items()):
        if not all_dates and not any(e["override"] is not None for e in per.values()):
            continue
        for ccy in sheet["ccys"]:
            e = per.get(ccy)
            if e is not None:
                out.append({"date": d, "desc": e["desc"], "weight": e["weight"], "ccy": ccy})
    return out


def excel_day_events(sheet: dict, today: date) -> dict[str, float]:
    """Excel's event weight per date (both currencies added), from today on."""
    out: dict[str, float] = {}
    for rows in sheet["dates"].values():
        for e in rows:
            if e["date"] >= today.isoformat():
                out[e["date"]] = round(out.get(e["date"], 0.0) + e["weight"], 6)
    return out


# ---- the workbook's desk-wide sheets: one block of columns per currency, the currency in row 4
#   "Event weights"  event name | weight   (the relevant events and their default weight)
#   "Event dates"    date | event          (dates are already the expiry an event weighs on)
#   "Holidays"       date                  (one column per currency)

def _ccy_columns(cells: dict[tuple[int, int], str]) -> tuple[int, dict[int, str]]:
    """The header row (the first one naming USD) and {column: currency} in it."""
    rows = sorted({r for r, _ in cells})
    for r in rows:
        hdr = {c: v.strip() for (rr, c), v in cells.items() if rr == r and re.fullmatch(r"\s*[A-Z]{3}\s*", v or "")}
        if "USD" in hdr.values():
            return r, hdr
    raise ValueError("no currency header row (USD, EUR, ...)")


def parse_event_weights(cells: dict[tuple[int, int], str]) -> list[dict]:
    """[{ccy, event, weight, ticker: ""}] from the "Event weights" sheet."""
    hdr_row, hdr = _ccy_columns(cells)
    out = []
    for (r, c), name in sorted(cells.items()):
        ccy = hdr.get(c)
        w = _num(cells.get((r, c + 1)))
        if r > hdr_row and ccy and name.strip() and w is not None:
            out.append({"ccy": ccy, "event": " ".join(name.split()), "weight": w, "ticker": ""})
    return out


def parse_event_dates(cells: dict[tuple[int, int], str]) -> list[dict]:
    """[{ccy, date, desc}] from the "Event dates" sheet."""
    hdr_row, hdr = _ccy_columns(cells)
    out = []
    for (r, c), v in sorted(cells.items()):
        ccy, d = hdr.get(c), _serial(v)
        desc = " ".join((cells.get((r, c + 1)) or "").split())
        if r > hdr_row and ccy and d and desc:
            out.append({"ccy": ccy, "date": d.isoformat(), "desc": desc})
    return out


def parse_holidays(cells: dict[tuple[int, int], str]) -> dict[str, list[date]]:
    """{ccy: [date]} from the "Holidays" sheet."""
    hdr_row, hdr = _ccy_columns(cells)
    out: dict[str, list[date]] = {}
    for (r, c), v in sorted(cells.items()):
        d = _serial(v)
        if r > hdr_row and c in hdr and d:
            out.setdefault(hdr[c], []).append(d)
    return out
