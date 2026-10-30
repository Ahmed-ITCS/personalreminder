from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from utils import format_local, normalize_phone, parse_when, phone_from_chat_id, to_utc


def _now():
    return datetime(2026, 10, 30, 12, 0, tzinfo=ZoneInfo("Asia/Karachi"))


def test_normalize_phone_variants():
    assert normalize_phone("+92 300 1234567") == "923001234567"
    assert normalize_phone("03001234567") == "923001234567"
    assert normalize_phone("0092 300 1234567") == "923001234567"
    assert normalize_phone("923001234567@c.us") == "923001234567"
    assert normalize_phone("") == ""
    assert phone_from_chat_id("923001234567:12@c.us") == "923001234567"


def test_parse_relative_times():
    now = _now()

    due = parse_when("in 10 minutes", "Asia/Karachi", now=now)
    assert due == (now + timedelta(minutes=10)).astimezone(ZoneInfo("UTC"))

    due = parse_when("in 2 hours", "Asia/Karachi", now=now)
    assert due == (now + timedelta(hours=2)).astimezone(ZoneInfo("UTC"))

    due = parse_when("in 3 days", "Asia/Karachi", now=now)
    assert due == (now + timedelta(days=3)).astimezone(ZoneInfo("UTC"))


def test_parse_tomorrow_and_absolute_times():
    now = _now()

    due = parse_when("tomorrow at 09:00", "Asia/Karachi", now=now)
    expected = to_utc(datetime(2026, 10, 31, 9, 0), "Asia/Karachi")
    assert due == expected

    due = parse_when("at 2026-11-01 14:30", "Asia/Karachi", now=now)
    expected = to_utc(datetime(2026, 11, 1, 14, 30), "Asia/Karachi")
    assert due == expected


def test_parse_rejects_bad_input():
    assert parse_when("sometime soon", "Asia/Karachi", now=_now()) is None


def test_format_local_uses_asia_karachi():
    iso = "2026-10-30T07:00:00Z"
    assert format_local(iso, "Asia/Karachi") == "30 Oct 2026, 12:00"
