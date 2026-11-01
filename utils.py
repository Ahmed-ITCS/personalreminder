from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

MAX_PHONE_DIGITS = 15


def is_group_chat_id(value: object) -> bool:
    """Return True when a WhatsApp chat id refers to a group, not a person."""
    raw = str(value or "").strip()
    if not raw:
        return False
    if "@g.us" in raw.lower():
        return True
    local = raw.split("@", 1)[0].split(":", 1)[0]
    digits = re.sub(r"\D", "", local)
    return len(digits) > MAX_PHONE_DIGITS


def normalize_phone(value: object, default_country_code: str = "92") -> str:
    """Return digits only, converting local numbers that start with 0."""
    if value is None:
        return ""
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return ""
    digits = digits.removeprefix("00")
    country_code = re.sub(r"\D", "", default_country_code or "")
    if country_code and digits.startswith("0") and len(digits) <= 12:
        digits = country_code + digits[1:]
    return digits


def phone_from_chat_id(value: object, default_country_code: str = "92") -> str:
    chat_id = str(value or "").split("@", 1)[0].split(":", 1)[0]
    return normalize_phone(chat_id, default_country_code)


def local_now(timezone_name: str) -> datetime:
    return datetime.now(ZoneInfo(timezone_name))


def to_utc(naive_local: datetime, timezone_name: str) -> datetime:
    tz = ZoneInfo(timezone_name)
    if naive_local.tzinfo is None:
        aware = naive_local.replace(tzinfo=tz)
    else:
        aware = naive_local.astimezone(tz)
    return aware.astimezone(UTC)


def parse_local_datetime(
    text: str, timezone_name: str
) -> datetime | None:
    """Parse a date/time entered in the configured local timezone."""
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%d/%m/%Y %H:%M",
        "%d-%m-%Y %H:%M",
    ):
        try:
            return to_utc(datetime.strptime(cleaned, fmt), timezone_name)
        except ValueError:
            continue
    return None


def parse_when(
    text: str, timezone_name: str, now: datetime | None = None
) -> datetime | None:
    """Parse reminder commands such as 'in 10 minutes' or 'tomorrow at 09:00'."""
    cleaned = " ".join(text.strip().split())
    current = now or local_now(timezone_name)
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo(timezone_name))
    current = current.astimezone(ZoneInfo(timezone_name))

    relative = re.fullmatch(
        r"in\s+(\d+)\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d)",
        cleaned,
        flags=re.IGNORECASE,
    )
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        if unit.startswith("m"):
            delta = timedelta(minutes=amount)
        elif unit.startswith("h"):
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(days=amount)
        return (current + delta).astimezone(UTC)

    lowered = cleaned.lower()
    base_date = None
    rest = cleaned
    if lowered.startswith("tomorrow"):
        base_date = (current + timedelta(days=1)).date()
        rest = cleaned[len("tomorrow") :].strip()
    elif lowered.startswith("today"):
        base_date = current.date()
        rest = cleaned[len("today") :].strip()

    rest = re.sub(r"^on\s+", "", rest, flags=re.IGNORECASE)
    rest = re.sub(r"^at\s+", "", rest, flags=re.IGNORECASE)
    rest = re.sub(r"\s+at\s+", " ", rest, flags=re.IGNORECASE).strip()

    if base_date is not None:
        for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I %p"):
            try:
                candidate = rest.upper() if "%p" in fmt else rest
                parsed_time = datetime.strptime(candidate, fmt).time()
                return to_utc(datetime.combine(base_date, parsed_time), timezone_name)
            except ValueError:
                continue
        return None

    time_only = _parse_time_only(rest, timezone_name, current)
    if time_only is not None:
        return time_only

    return parse_local_datetime(rest, timezone_name)


def _parse_time_only(
    text: str, timezone_name: str, now: datetime
) -> datetime | None:
    """Parse a clock time and use today, or tomorrow if it has already passed."""
    tz = ZoneInfo(timezone_name)
    current = now.astimezone(tz)
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I %p"):
        try:
            candidate = text.upper() if "%p" in fmt else text
            parsed_time = datetime.strptime(candidate, fmt).time()
        except ValueError:
            continue
        due = datetime.combine(current.date(), parsed_time, tzinfo=tz)
        if due <= current:
            due += timedelta(days=1)
        return due.astimezone(UTC)
    return None


def format_local(
    iso_value: str | None, timezone_name: str, fmt: str = "%d %b %Y, %H:%M"
) -> str:
    if not iso_value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(iso_value))
    except ValueError:
        return str(iso_value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(ZoneInfo(timezone_name)).strftime(fmt)
