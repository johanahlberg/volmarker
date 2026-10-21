"""Create a demo data folder with made-up surfaces (NOT market data).

    python scripts/seed_demo.py demo_data
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from volmarker.conventions import TENORS  # noqa: E402
from volmarker.service import Service  # noqa: E402
from volmarker.store import Store  # noqa: E402
from volmarker.surface import calibrate  # noqa: E402

# made-up ATM curves (ON ... 5Y) and 25D RR/BF levels
MARKED = {
    "EURUSD": ([6.2, 6.6, 6.8, 7.0, 7.1, 7.2, 7.4, 7.55, 7.7, 7.9, 8.0, 8.1, 8.2], 0.15, 0.18, True),
    "USDCHF": ([6.9, 7.2, 7.4, 7.6, 7.7, 7.8, 7.95, 8.05, 8.15, 8.3, 8.4, 8.45, 8.5], -0.45, 0.20, True),
    "USDJPY": ([9.5, 9.8, 9.9, 10.1, 10.2, 10.3, 10.5, 10.6, 10.7, 10.9, 11.0, 11.1, 11.2], -1.2, 0.30, True),
    "GBPUSD": ([6.8, 7.1, 7.3, 7.5, 7.6, 7.7, 7.9, 8.0, 8.1, 8.3, 8.4, 8.5, 8.6], -0.30, 0.20, True),
    "AUDUSD": ([8.9, 9.3, 9.5, 9.7, 9.8, 9.9, 10.1, 10.2, 10.3, 10.5, 10.6, 10.7, 10.8], -0.90, 0.25, True),
    "EURSEK": ([6.1, 6.4, 6.6, 6.8, 6.9, 7.0, 7.2, 7.3, 7.4, 7.6, 7.7, 7.8, 7.9], 0.35, 0.20, False),
    "EURNOK": ([7.0, 7.3, 7.5, 7.7, 7.8, 7.9, 8.1, 8.2, 8.3, 8.5, 8.6, 8.7, 8.8], 0.60, 0.25, False),
}
# derived: (bases, target ATMs to calibrate to, rr25, bf25)
DERIVED = {
    "EURCHF": (["EURUSD", "USDCHF"], [4.6, 4.9, 5.0, 5.1, 5.2, 5.3, 5.5, 5.6, 5.7, 5.9, 6.0, 6.1, 6.2], -0.6, 0.20),
    "AUDCHF": (["AUDUSD", "USDCHF"], [9.0, 9.4, 9.6, 9.8, 9.9, 10.0, 10.2, 10.3, 10.4, 10.6, 10.7, 10.8, 10.9], -1.1, 0.30),
    "NOKSEK": (["EURSEK", "EURNOK"], [5.0, 5.3, 5.5, 5.7, 5.8, 5.9, 6.1, 6.2, 6.3, 6.5, 6.6, 6.7, 6.8], -0.2, 0.15),
}

# sidebar groups (a pair setting)
GROUPS = {"EURUSD": "EUR & CHF", "USDCHF": "EUR & CHF", "EURCHF": "EUR & CHF",
          "EURSEK": "Scandies", "EURNOK": "Scandies", "NOKSEK": "Scandies",
          "GBPUSD": "GBP", "USDJPY": "Asia & commodity", "AUDUSD": "Asia & commodity",
          "AUDCHF": "Asia & commodity"}

HOLIDAYS = """ccy,date
USD,2026-11-26
USD,2026-12-25
EUR,2026-12-25
CHF,2026-12-25
USD,2027-01-01
EUR,2027-01-01
CHF,2027-01-01
"""
# manual events (no release time) are dated by the option expiry they weigh on
EVENTS = """ccy,date,description
USD,2026-10-29,FOMC Rate Decision (Upper Bound)
EUR,2026-10-29,ECB Deposit Facility Rate
USD,2026-11-03,US midterm elections
USD,2026-11-06,Change in Nonfarm Payrolls
GBP,2026-11-05,Bank of England Bank Rate
NOK,2026-11-05,Norges Bank Deposit Rates
SEK,2026-11-05,Riksbank Policy Rate
USD,2026-11-12,CPI YoY
USD,2026-12-04,Change in Nonfarm Payrolls
USD,2026-12-10,FOMC Rate Decision (Upper Bound)
USD,2026-12-10,CPI YoY
CHF,2026-12-10,SNB Policy Rate
EUR,2026-12-17,ECB Deposit Facility Rate
GBP,2026-12-17,Bank of England Bank Rate
"""
# desk default weights for recurring events (matched on the description)
EVENT_DEFAULTS = """ccy,event,weight,ticker
USD,FOMC Rate Decision (Upper Bound),0.8,FDTR Index
USD,Change in Nonfarm Payrolls,0.5,NFP TCH Index
USD,CPI YoY,0.4,CPI YOY Index
EUR,ECB Deposit Facility Rate,0.6,
CHF,SNB Policy Rate,0.6,
GBP,Bank of England Bank Rate,0.6,
SEK,Riksbank Policy Rate,0.4,
NOK,Norges Bank Deposit Rates,0.4,
"""
# one-off events are typed on the pair; recurring ones come from the defaults
USD_EVENTS = [{"date": "2026-11-03", "desc": "US midterm elections", "weight": 1.0, "ccy": "USD"}]


def main(root: str) -> None:
    store = Store(root)
    (store.root / "calendar" / "holidays.csv").write_text(HOLIDAYS)
    (store.root / "calendar" / "events.csv").write_text(EVENTS)
    (store.root / "calendar" / "event_defaults.csv").write_text(EVENT_DEFAULTS)
    svc = Service(store, "demo")

    for pair, (atms, rr, bf, key) in MARKED.items():
        rec = svc.create_pair(pair, "marked", None, key, GROUPS.get(pair))
        for t, atm in zip(TENORS, atms):
            rec["tenors"][t].update(atm=atm, rr25=rr, bf25=bf,
                                    spread=0.3 if key else 0.6)
        if "USD" in pair:
            rec["weights"] = {"weekdays": [1.05, 1, 1, 1, 1, 0, 0], "holiday1": 0.4,
                              "holiday2": 0.4, "events": USD_EVENTS}
        svc.publish(pair, rec, rec["version"], "demo marks")

    for pair, (bases, atms, rr, bf) in DERIVED.items():
        rec = svc.create_pair(pair, "derived", bases, False, GROUPS.get(pair))
        for t in TENORS:
            rec["tenors"][t].update(rr25=rr, bf25=bf, spread=0.6)
        rec = calibrate(rec, {b: store.latest(b) for b in bases}, dict(zip(TENORS, atms)))
        svc.publish(pair, rec, 1, "calibrated")
    print(f"Demo data written to {store.root.resolve()}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo_data")
