"""Event weights to Fenics (XML poster) and Front Arena (event files).

A port of the desk's Excel weight workbook (WeightExporter / LoadWeights):

  Fenics       one gfi_message per pair, action load_events / WEIGHT_FEED,
               posted as  xml=<url-encoded message>  to <server>/xmlposter.
               It carries the 7 weekday weights, the holiday base weight of
               each currency (FirstCcy = ccy2, SecondCcy = ccy1) and the
               events. Fenics computes  weekday x holidays + events,  so an
               event on a holiday of its own currency is sent as a Holiday
               weight  h + w / (weekday x other holiday)  instead.
  Front Arena  FA_FX/<CCY1><CCY2>_events.txt   ccy,date,name,weight  per expiry date
               FA_WS/<CCY1><CCY2>_events.txt   the same moved to the business day
               before, weight (w + base) / base with base = holiday factor.

Event weights come from the pair's effective weights (desk defaults plus the
pair's own events); events with weight 0 are left out.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from decimal import Decimal
from xml.sax.saxutils import escape, quoteattr

from .calendar import WEEKDAYS
from .conventions import split_pair


def _name(ev: dict) -> str:
    """The day's main event, as the Excel workbook names it: no currency prefix, no "+2"."""
    desc, ccy = ((ev.get("parts") or [{}])[0].get("desc") or ev.get("desc") or ""), ev.get("ccy") or ""
    desc = re.sub(r" \+\d+$", "", desc)
    return desc[len(ccy) + 1:] if ccy and desc.startswith(ccy + " ") else desc


def _events(pair: str, weights: dict, today: date) -> list[dict]:
    c1, _ = split_pair(pair)
    out = []
    for e in weights["events"]:
        d = date.fromisoformat(e["date"])
        if d > today and abs(float(e["weight"])) > 1e-12:
            out.append({"date": d, "ccy": e.get("ccy") or c1, "name": _name(e), "weight": float(e["weight"])})
    return sorted(out, key=lambda e: e["date"])


def fenics_items(pair: str, weights: dict, holidays: dict[str, set], today: date) -> list[dict]:
    """Events as Fenics wants them: plain events, or holiday weights on a
    holiday of the event's own currency."""
    c1, c2 = split_pair(pair)
    hol = {c1: weights["holiday1"], c2: weights["holiday2"]}
    out = []
    for e in _events(pair, weights, today):
        c, other = e["ccy"], (c2 if e["ccy"] == c1 else c1)
        wd = weights["weekdays"][e["date"].weekday()]
        h_other = hol[other] if e["date"] in holidays.get(other, ()) else 1.0
        if e["date"] in holidays.get(c, ()) and wd * h_other > 0:
            out.append(dict(e, type="Holiday", value=hol[c] + e["weight"] / (wd * h_other)))
        else:
            out.append(dict(e, type="Event", value=e["weight"]))
    return out


def fenics_xml(pair: str, weights: dict, holidays: dict[str, set], today: date,
               user: str, password: str, now: datetime | None = None) -> str:
    c1, c2 = split_pair(pair)
    label = f"{c1}/{c2}"                 # node names as the workbook writes them ("EUR/USD Weights")
    now = (now or datetime.now()).astimezone()
    # like VBA Format(x, "0.0000000000000000"): 15 significant digits, so 1.4 stays 1.4000000000000000
    v = lambda x: str(Decimal(f"{x:.15g}").quantize(Decimal("1e-16")))
    L = ['<?xml version="1.0" encoding="UTF-8"?>', '<gfi_message version="2.0">', " <header>",
         "  <transactionId>10001</transactionId>",
         f"  <timestamp>{now.isoformat(timespec='seconds')}</timestamp>",
         f"  <username>{escape(user)}</username>", f"  <password>{escape(password)}</password>",
         " </header>", " <body>",
         '  <action name = "load_events" function = "WEIGHT_FEED" version = "1.0">',
         '   <option name = "data" ref = "data1"/>', "  </action>",
         '  <data name = "data1" format = "NAME_VALUE">',
         f'   <node name = "{label} Weights">',
         f'    <field name = "Currency" value = "{c1}"/>', f'    <field name = "CtrCcy" value = "{c2}"/>']
    for day, w in zip(WEEKDAYS, weights["weekdays"]):
        L += [f'    <node name = "Add weekday weight {day} for {label}">',
              '     <field name = "EventType" value = "Default"/>',
              f'     <field name = "EventDesc" value = "{day}"/>',
              f'     <field name = "Value" value = "{v(w)}"/>', "    </node>"]
    for which, ccy, w in (("FirstCcy", c2, weights["holiday2"]), ("SecondCcy", c1, weights["holiday1"])):
        L += [f'    <node name = "Add holiday base weight {which} for {label}">',
              f'     <field name = "Country" value = "{ccy}"/>',
              '     <field name = "EventType" value = "Default"/>',
              f'     <field name = "EventDesc" value = "{which}"/>',
              f'     <field name = "Value" value = "{v(w)}"/>', "    </node>"]
    for e in fenics_items(pair, weights, holidays, today):
        what = "Add event weight on holiday" if e["type"] == "Holiday" else "Add event weight"
        node = f'{what} {e["date"].isoformat()} {e["ccy"]} {e["name"]} to {label}'
        L += [f"    <node name = {quoteattr(node)}>",
              f'     <field name = "Country" value = "{e["ccy"]}"/>',
              f'     <field name = "Date" value = "{e["date"].strftime("%m/%d/%Y")}"/>',
              f'     <field name = "EventType" value = "{e["type"]}"/>',
              f'     <field name = "EventDesc" value = {quoteattr(e["name"])}/>',
              f'     <field name = "Value" value = "{v(e["value"])}"/>', "    </node>"]
    L += ["   </node>", "  </data>", " </body>", "</gfi_message>"]
    return "\r\n".join(L) + "\r\n"


def frontarena_lines(pair: str, weights: dict, holidays: dict[str, set], today: date,
                     date_format: str) -> tuple[list[str], list[str]]:
    """(FA_FX lines, FA_WS lines)"""
    c1, c2 = split_pair(pair)
    evs = _events(pair, weights, today)
    fx = [f'{e["ccy"]},{e["date"].strftime(date_format)},{e["name"]},{e["weight"]:g}' for e in evs]
    ws: dict[date, dict] = {}
    for e in evs:
        d = e["date"] - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        if d < today:
            continue
        x = ws.get(d)
        if x is None:
            ws[d] = {"ccy": e["ccy"], "name": f'{e["ccy"]} {e["name"]}', "weight": e["weight"]}
        else:                    # as in the macro: renamed when the new event outweighs the running total
            if x["weight"] < e["weight"]:
                x.update(ccy=e["ccy"], name=f'{e["ccy"]} {e["name"]}')
            x["weight"] += e["weight"]
    out_ws = []
    for d, x in sorted(ws.items()):
        base = 1.0
        for i, c in enumerate((c1, c2), start=1):
            if d in holidays.get(c, ()):
                base *= weights[f"holiday{i}"]
        w = (x["weight"] + base) / base if base else x["weight"]
        out_ws.append(f'{x["ccy"]},{d.strftime(date_format)},{x["name"]},{w:g}')
    return fx, out_ws


def fingerprint(pair: str, weights: dict, holidays: dict[str, set], today: date) -> str:
    """Changes whenever what would be sent changes (weights, defaults, calendar)."""
    items = [(e["date"].isoformat(), e["ccy"], e["type"], round(e["value"], 10))
             for e in fenics_items(pair, weights, holidays, today)]
    blob = json.dumps([weights["weekdays"], weights["holiday1"], weights["holiday2"], items])
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def post(url: str, xml: str, timeout: float = 20) -> str:
    """POST the message the way the Excel macro does: form field xml=..."""
    body = ("xml=" + urllib.parse.quote_plus(xml)).encode("ascii")
    req = urllib.request.Request(url.rstrip("/") + "/xmlposter", data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")
