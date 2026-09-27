"""W13: ParentSquare's date strings become real datetimes, year included."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from parentsquare_mcp.parsers.dates import (
    infer_date,
    parse_clock,
    parse_day,
    parse_day_and_time,
    parse_display_datetime,
    parse_google_calendar_dates,
    parse_numeric_datetime,
    parse_timestamp,
    resolve_tz,
)

EASTERN = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc)


def test_data_timestamp():
    assert parse_timestamp("2026-09-14 19:13:59+00:00") == datetime(2026, 9, 14, 19, 13, 59, tzinfo=timezone.utc)
    assert parse_timestamp("2026-09-14T19:13:59Z") == datetime(2026, 9, 14, 19, 13, 59, tzinfo=timezone.utc)
    assert parse_timestamp("2 days ago") is None
    assert parse_timestamp("") is None


@pytest.mark.parametrize("text,expected", [
    ("6:38 am", time(6, 38)), ("8:29 AM", time(8, 29)), ("3:10 pm", time(15, 10)),
    ("12:05 am", time(0, 5)), ("12:30 PM", time(12, 30)), ("at 8:59 P.M.", time(20, 59)), ("noon", None),
])
def test_clock(text, expected):
    assert parse_clock(text) == expected


def test_year_is_inferred_from_the_weekday():
    today = date(2026, 9, 27)
    assert infer_date(9, 10, 3, today) == date(2026, 9, 10)     # Thu, Sep 10 -> this year
    assert infer_date(9, 10, 2, today) == date(2025, 9, 10)     # Wed, Sep 10 -> last year
    assert infer_date(12, 24, None, today) == date(2025, 12, 24)  # never in the future
    assert infer_date(2, 29, None, today) == date(2024, 2, 29)  # leap day


@pytest.mark.parametrize("text,expected", [
    ("Thu, Sep 10", date(2026, 9, 10)),
    ("Mon, Aug 24", date(2026, 8, 24)),
    ("Monday, September 14", date(2026, 9, 14)),
    ("Sep 10, 2024", date(2024, 9, 10)),
    ("Today", date(2026, 9, 27)),
    ("Yesterday", date(2026, 9, 26)),
    ("no date here", None),
])
def test_day_headings(text, expected):
    assert parse_day(text, EASTERN, NOW) == expected


def test_today_follows_the_accounts_zone():
    # 02:00 UTC on the 28th is still the 27th in New York
    assert parse_day("Today", EASTERN, datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)) == date(2026, 9, 27)


def test_chat_message_time():
    assert parse_day_and_time("Thu, Sep 10", "6:38 am", EASTERN, NOW) == datetime(2026, 9, 10, 6, 38, tzinfo=EASTERN)
    naive = parse_day_and_time("Thu, Sep 10", "6:38 am", None, NOW)
    assert naive == datetime(2026, 9, 10, 6, 38) and naive.tzinfo is None


def test_notice_date():
    assert parse_display_datetime("Tuesday, Sep 8 at 8:59 PM", EASTERN, NOW) == datetime(2026, 9, 8, 20, 59, tzinfo=EASTERN)


def test_conversation_list_date():
    assert parse_numeric_datetime("Thu 9/10/26, 8:29 am", EASTERN) == datetime(2026, 9, 10, 8, 29, tzinfo=EASTERN)
    assert parse_numeric_datetime("9/10/2026 8:29 pm", None) == datetime(2026, 9, 10, 20, 29)


def test_google_calendar_dates():
    start, end = parse_google_calendar_dates("20260924T223000Z/20260925T000000Z")
    assert start == datetime(2026, 9, 24, 22, 30, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
    assert parse_google_calendar_dates("20260924/20260925")[0] == datetime(2026, 9, 24, tzinfo=timezone.utc)
    assert parse_google_calendar_dates("") == (None, None)


def test_rails_zone_names():
    assert resolve_tz("Eastern Time (US & Canada)") == EASTERN
    assert resolve_tz("Pacific Time (US & Canada)") == ZoneInfo("America/Los_Angeles")
    assert resolve_tz("America/Chicago") == ZoneInfo("America/Chicago")
    assert resolve_tz("Nowhere Standard Time") is None
    assert resolve_tz(None) is None
