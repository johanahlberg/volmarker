"""Time zones for event release times and option expiry cuts.

Uses zoneinfo when the system has a tz database. Windows Python usually has
none (it needs the separate tzdata package, and we install nothing), so the
zones the desk needs fall back to built-in daylight-saving rules.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo

try:
    from zoneinfo import ZoneInfo
except ImportError:          # Python < 3.9
    ZoneInfo = None


def _nth_sunday(year: int, month: int, n: int) -> date:
    """n-th Sunday of the month; n = -1 for the last one."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(6 - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - 6) % 7)


class _RuleZone(tzinfo):
    """Standard offset plus a DST rule: 'eu' (last Sunday of March to last
    Sunday of October, switching at 01:00 UTC), 'us' (2nd Sunday of March to
    1st Sunday of November, 02:00 local), 'il' (Friday before the last Sunday
    of March, 02:00 local, to the last Sunday of October, 02:00 local), 'au' (1st Sunday of October to 1st
    Sunday of April, 02:00 local standard time) or None."""

    def __init__(self, name: str, std_hours: float, rule: str | None):
        self.name, self.std, self.rule = name, timedelta(hours=std_hours), rule

    def _dst_utc_window(self, year: int) -> tuple[datetime, datetime] | None:
        if self.rule == "eu":
            return (datetime.combine(_nth_sunday(year, 3, -1), time(1)),
                    datetime.combine(_nth_sunday(year, 10, -1), time(1)))
        if self.rule == "us":
            return (datetime.combine(_nth_sunday(year, 3, 2), time(2)) - self.std,
                    datetime.combine(_nth_sunday(year, 11, 1), time(2)) - self.std - timedelta(hours=1))
        if self.rule == "il":
            return (datetime.combine(_nth_sunday(year, 3, -1) - timedelta(days=2), time(2)) - self.std,
                    datetime.combine(_nth_sunday(year, 10, -1), time(2)) - self.std - timedelta(hours=1))
        return None

    def _is_dst_utc(self, utc_naive: datetime) -> bool:
        if self.rule == "au":       # southern hemisphere: DST spans the new year
            start = datetime.combine(_nth_sunday(utc_naive.year, 10, 1), time(2)) - self.std
            end = datetime.combine(_nth_sunday(utc_naive.year, 4, 1), time(3)) - self.std - timedelta(hours=1)
            return utc_naive >= start or utc_naive < end
        w = self._dst_utc_window(utc_naive.year)
        return bool(w) and w[0] <= utc_naive < w[1]

    def utcoffset(self, dt):
        if dt is None:
            return self.std
        # local wall time -> guess UTC with the standard offset, then check
        utc_guess = dt.replace(tzinfo=None) - self.std
        return self.std + (timedelta(hours=1) if self._is_dst_utc(utc_guess) else timedelta(0))

    def dst(self, dt):
        return self.utcoffset(dt) - self.std

    def tzname(self, dt):
        return self.name

    def fromutc(self, dt):
        naive = dt.replace(tzinfo=None)
        off = self.std + (timedelta(hours=1) if self._is_dst_utc(naive) else timedelta(0))
        return (naive + off).replace(tzinfo=self)


FALLBACK = {
    "Europe/Zurich": (1, "eu"), "Europe/Berlin": (1, "eu"), "Europe/Paris": (1, "eu"),
    "Europe/Stockholm": (1, "eu"), "Europe/Oslo": (1, "eu"), "Europe/Warsaw": (1, "eu"),
    "Europe/Budapest": (1, "eu"),
    "Europe/London": (0, "eu"), "Europe/Moscow": (3, None), "Europe/Istanbul": (3, None),
    "America/New_York": (-5, "us"), "America/Toronto": (-5, "us"), "America/Mexico_City": (-6, None),
    "America/Sao_Paulo": (-3, None),
    "Asia/Tokyo": (9, None), "Asia/Singapore": (8, None), "Asia/Hong_Kong": (8, None),
    "Asia/Shanghai": (8, None), "Asia/Jerusalem": (2, "il"),
    "Asia/Tel_Aviv": (2, "il"),           # old name of Asia/Jerusalem, still accepted by the tz database
    "Australia/Sydney": (10, "au"), "Africa/Johannesburg": (2, None), "UTC": (0, None),
}
_cache: dict[str, tzinfo] = {}


def get_zone(name: str) -> tzinfo:
    if name not in _cache:
        z = None
        if ZoneInfo is not None:
            try:
                z = ZoneInfo(name)
            except Exception:
                z = None
        if z is None:
            if name not in FALLBACK:
                raise ValueError(f"Unknown time zone {name} (no tz database on this PC; "
                                 f"known: {', '.join(sorted(FALLBACK))})")
            z = _RuleZone(name, *FALLBACK[name])
        _cache[name] = z
    return _cache[name]


def localize(naive: datetime, zone: str) -> datetime:
    """Wall-clock time in `zone` -> aware UTC datetime."""
    return naive.replace(tzinfo=get_zone(zone)).astimezone(timezone.utc)


def to_local(utc: datetime, zone: str) -> datetime:
    return utc.astimezone(get_zone(zone))
