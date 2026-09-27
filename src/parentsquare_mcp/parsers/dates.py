"""Turn ParentSquare's date strings into real datetimes.

ParentSquare shows dates in three shapes:

* Feed posts (and comments) carry ``data-timestamp="2026-09-14 19:13:59+00:00"``,
  which is exact. ``parse_timestamp`` reads it.
* Chat day headers ("Thu, Sep 10") and notice dates ("Tuesday, Sep 8 at 8:59 PM")
  have no year. They are always in the past and always name the weekday, so the
  year is the most recent one in which that month and day fell on that weekday:
  ``infer_date``. The weekday pattern repeats every 5 to 11 years, so the answer
  is unambiguous for anything the site still shows (notices: 3 weeks).
* The conversation list says "Thu 9/10/26, 8:29 am", with a two-digit year.

Clock times are the account's local time. The zone comes from the school's
``/api/v2/schools/{id}`` record as a Rails name ("Eastern Time (US & Canada)");
``resolve_tz`` maps it to a ``ZoneInfo``. Without a zone the functions return
naive datetimes holding the local wall-clock time, never a guessed zone.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ActiveSupport::TimeZone names for the zones US schools use, plus UTC.
RAILS_TIME_ZONES = {
    "Eastern Time (US & Canada)": "America/New_York",
    "Central Time (US & Canada)": "America/Chicago",
    "Mountain Time (US & Canada)": "America/Denver",
    "Pacific Time (US & Canada)": "America/Los_Angeles",
    "Arizona": "America/Phoenix",
    "Alaska": "America/Juneau",
    "Hawaii": "Pacific/Honolulu",
    "Indiana (East)": "America/Indiana/Indianapolis",
    "Atlantic Time (Canada)": "America/Halifax",
    "Newfoundland": "America/St_Johns",
    "Saskatchewan": "America/Regina",
    "Puerto Rico": "America/Puerto_Rico",
    "Guam": "Pacific/Guam",
    "American Samoa": "Pacific/Pago_Pago",
    "UTC": "UTC",
}

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_WEEKDAYS = {d: i for i, d in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])}

_CLOCK_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?", re.I)
_MONTH_DAY_RE = re.compile(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?")
_WEEKDAY_RE = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", re.I)
_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\b")


def resolve_tz(name: str | None) -> tzinfo | None:
    """A ``ZoneInfo`` for a Rails zone name or an IANA name; None if unknown."""
    if not name:
        return None
    iana = RAILS_TIME_ZONES.get(name.strip(), name.strip())
    try:
        return ZoneInfo(iana)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse ``data-timestamp`` ("2026-09-14 19:13:59+00:00") into an aware datetime."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_clock(text: str | None) -> time | None:
    """"6:38 am", "3:10 PM" -> ``time``."""
    m = _CLOCK_RE.search(text or "")
    if not m:
        return None
    hour, minute = int(m.group(1)) % 12, int(m.group(2))
    if m.group(3).lower() == "p":
        hour += 12
    return time(hour, minute)


def _now_in(tz: tzinfo | None, now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now
    return now.astimezone(tz) if tz else now


def infer_date(month: int, day: int, weekday: int | None, today: date, max_years: int = 12) -> date | None:
    """The latest date on or before *today* with this month, day and weekday.

    *weekday* is Monday=0. Without a weekday it is the latest such date not in
    the future. A day of slack allows for the viewer's clock running ahead of
    the account's zone.
    """
    limit = today + timedelta(days=1)
    for year in range(limit.year, limit.year - max_years, -1):
        try:
            candidate = date(year, month, day)
        except ValueError:  # Feb 29 in a non-leap year
            continue
        if candidate > limit:
            continue
        if weekday is None or candidate.weekday() == weekday:
            return candidate
    return None


def parse_day(text: str | None, tz: tzinfo | None = None, now: datetime | None = None) -> date | None:
    """A day heading such as "Thu, Sep 10", "Today", "Yesterday" or "Sep 10, 2025"."""
    if not text:
        return None
    today = _now_in(tz, now).date()
    lowered = text.strip().lower()
    if lowered.startswith("today"):
        return today
    if lowered.startswith("yesterday"):
        return today - timedelta(days=1)
    m = _MONTH_DAY_RE.search(text)
    if not m or m.group(1).lower()[:3] not in _MONTHS:
        return None
    month, day = _MONTHS[m.group(1).lower()[:3]], int(m.group(2))
    if m.group(3):
        try:
            return date(int(m.group(3)), month, day)
        except ValueError:
            return None
    wd = _WEEKDAY_RE.search(text)
    weekday = _WEEKDAYS[wd.group(1).lower()[:3]] if wd else None
    return infer_date(month, day, weekday, today)


def _combine(day: date, clock: time, tz: tzinfo | None) -> datetime:
    naive = datetime.combine(day, clock)
    return naive.replace(tzinfo=tz) if tz else naive


def parse_day_and_time(day_text: str | None, time_text: str | None,
                       tz: tzinfo | None = None, now: datetime | None = None) -> datetime | None:
    """Chat messages: a day heading plus a clock time -> datetime in *tz*."""
    day = parse_day(day_text, tz, now)
    clock = parse_clock(time_text)
    if day is None or clock is None:
        return None
    return _combine(day, clock, tz)


def parse_display_datetime(text: str | None, tz: tzinfo | None = None,
                           now: datetime | None = None) -> datetime | None:
    """"Tuesday, Sep 8 at 8:59 PM" (notices, polls) -> datetime in *tz*."""
    if not text:
        return None
    return parse_day_and_time(text, text, tz, now)


def parse_numeric_datetime(text: str | None, tz: tzinfo | None = None) -> datetime | None:
    """"Thu 9/10/26, 8:29 am" (the conversation list) -> datetime in *tz*."""
    m = _NUMERIC_DATE_RE.search(text or "")
    clock = parse_clock(text)
    if not m or clock is None:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    try:
        return _combine(date(year, month, day), clock, tz)
    except ValueError:
        return None


def parse_google_calendar_dates(value: str | None) -> tuple[datetime | None, datetime | None]:
    """A Google Calendar ``dates=20260924T223000Z/20260925T000000Z`` pair -> (start, end).

    Values ending in Z are UTC. All-day values ("20260924/20260925") come back as
    midnight UTC of those days; a value without Z is left naive.
    """
    if not value:
        return None, None

    def one(part: str) -> datetime | None:
        part = part.strip()
        for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
            try:
                parsed = datetime.strptime(part, fmt)
            except ValueError:
                continue
            return parsed.replace(tzinfo=timezone.utc) if fmt != "%Y%m%dT%H%M%S" else parsed
        return None

    start, _, end = value.partition("/")
    return one(start), (one(end) if end else None)
