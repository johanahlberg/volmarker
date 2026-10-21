"""Bloomberg economic calendar exports (ECO <GO>, saved as .xlsx).

Layout of the export: a header row with "Date Time", "Country Code", "Event",
"Relevance", "Ticker" (plus Period, Survey, Actual, ...), one row per release,
and a "Download time: ..." footer. Date Time is an Excel serial in the local
time of the Bloomberg terminal; a few rows only have a date ("10/27/2026") or
a date range ("10/26/2026-10/30/2026") because the time is not known yet.

Read with the standard library only (an .xlsx is a zip of XML files).
"""

from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

from .tz import localize

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

# Bloomberg country code -> currency (eurozone members all map to EUR)
COUNTRY_CCY = {
    "US": "USD", "EC": "EUR", "SZ": "CHF", "UK": "GBP", "JN": "JPY", "NO": "NOK", "SW": "SEK",
    "CA": "CAD", "AU": "AUD", "NZ": "NZD", "CH": "CNH", "MX": "MXN", "SA": "ZAR", "DE": "DKK",
    "PD": "PLN", "CZ": "CZK", "HU": "HUF", "TU": "TRY", "IS": "ILS", "RU": "RUB", "SI": "SGD",
    "HK": "HKD",
    **{c: "EUR" for c in ("GE", "FR", "IT", "SP", "NE", "BE", "AS", "FI", "IR", "PO", "GR",
                          "SO", "SV", "LN", "LV", "ES", "HR", "CY", "MT", "LX", "SK")},
}
# events of the currency's own country carry no prefix; others do ("GE IFO Business Climate")
HOME_COUNTRY = {"USD": "US", "EUR": "EC", "CHF": "SZ", "GBP": "UK", "JPY": "JN", "NOK": "NO",
                "SEK": "SW", "CAD": "CA", "AUD": "AU", "NZD": "NZ", "CNH": "CH", "MXN": "MX",
                "ZAR": "SA", "DKK": "DE", "PLN": "PD", "CZK": "CZ", "HUF": "HU", "TRY": "TU",
                "ILS": "IS", "RUB": "RU", "SGD": "SI", "HKD": "HK"}


def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group():
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx(data: bytes | str, sheet_name: str | None = None) -> list[list]:
    """Rows of a sheet (the first one by default) as lists of cell values (str, or None)."""
    import io
    try:
        z = zipfile.ZipFile(io.BytesIO(data) if isinstance(data, bytes) else data)
    except zipfile.BadZipFile:
        raise ValueError("Not an Excel .xlsx file (save the Bloomberg export as .xlsx)") from None
    strings = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_NS + "si"):
            strings.append("".join(t.text or "" for t in si.iter(_NS + "t")))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    sheets = list(wb.iter(_NS + "sheet"))
    pick = next((sh for sh in sheets if sh.get("name") == sheet_name), None) if sheet_name else sheets[0]
    if pick is None:
        raise ValueError(f"No sheet {sheet_name!r} in this workbook")
    target = rels[pick.get(_REL)].lstrip("/")
    sheet = ET.fromstring(z.read(target if target.startswith("xl/") else "xl/" + target))
    out = []
    for row in sheet.iter(_NS + "row"):
        cells: dict[int, str] = {}
        for c in row.iter(_NS + "c"):
            v, t = c.find(_NS + "v"), c.get("t")
            if t == "inlineStr":
                cells[_col(c.get("r"))] = "".join(x.text or "" for x in c.iter(_NS + "t"))
            elif v is not None:
                cells[_col(c.get("r"))] = strings[int(v.text)] if t == "s" else v.text
        out.append([cells.get(i) for i in range(max(cells) + 1)] if cells else [])
    return out


def _excel_time(serial: float) -> datetime:
    """Excel serial -> naive datetime, rounded to the minute (14:29:59.999 is 14:30)."""
    t = datetime(1899, 12, 30) + timedelta(days=serial)
    return (t + timedelta(seconds=30)).replace(second=0, microsecond=0)


def parse_eco(data: bytes | str, source_tz: str) -> dict:
    """{"events": [...], "skipped": [...], "downloaded": str|None, "assumed": {country: ccy}}

    Each event: ccy, country, event (Bloomberg name), name (desk name, with a
    country prefix outside the home country), ticker, relevance, date (local
    release date, ISO), time (local HH:MM or None), ts (UTC ISO or None)."""
    rows = read_xlsx(data)
    hdr_i = next((i for i, r in enumerate(rows) if r and "Date Time" in r and "Event" in r), None)
    if hdr_i is None:
        raise ValueError("Not a Bloomberg ECO export: no 'Date Time' / 'Event' header row")
    hdr = {name: i for i, name in enumerate(rows[hdr_i]) if name}
    def get(r, name):
        i = hdr.get(name)
        return (r[i] if i is not None and i < len(r) else None) or None
    body = rows[hdr_i + 1:]
    known = [COUNTRY_CCY[c] for c in (str(get(r, "Country Code") or "").strip() for r in body) if c in COUNTRY_CCY]
    main_ccy = max(set(known), key=known.count) if known else None
    events, skipped, downloaded, assumed = [], [], None, set()
    for r in body:
        when = get(r, "Date Time")
        if not when:
            continue
        if str(when).startswith("Download time"):
            downloaded = str(when).split(":", 1)[1].strip()
            continue
        country, name = (get(r, "Country Code") or "").strip(), (get(r, "Event") or "").strip()
        if not name:
            continue
        ccy = COUNTRY_CCY.get(country)
        if ccy is None:          # the export is per region: an unlisted country belongs to the file's currency
            if main_ccy is None:
                skipped.append({"why": f"unknown country code {country}", "event": name, "when": str(when)})
                continue
            ccy = main_ccy
            assumed.add(country)
        ts = local_time = None
        try:
            local = _excel_time(float(when))
            d, local_time = local.date(), local.strftime("%H:%M")
            ts = localize(local, source_tz).isoformat(timespec="minutes")
        except ValueError:
            m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(when).strip())
            if not m:            # a date range: the release day is not known yet
                skipped.append({"why": "no release date yet", "event": f"{country} {name}", "when": str(when)})
                continue
            d = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        rel = get(r, "Relevance")
        events.append({
            "ccy": ccy, "country": country, "event": name,
            "name": name if country == HOME_COUNTRY.get(ccy) else f"{country} {name}",
            "ticker": (get(r, "Ticker") or "").strip(), "relevance": round(float(rel), 1) if rel else None,
            "date": d.isoformat(), "time": local_time, "ts": ts,
        })
    return {"events": events, "skipped": skipped, "downloaded": downloaded,
            "assumed": {c: main_ccy for c in sorted(assumed)}}
