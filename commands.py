from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from db import to_iso
from reminders import cancel_reminder, create_reminder, get_reminder, list_reminders
from utils import format_local, normalize_phone, parse_when


def build_help(timezone_name: str) -> str:
    return (
        "*How to send commands*\n"
        "Send one WhatsApp message to this number. Commands are not case-sensitive.\n\n"
        "*Available commands*\n"
        "- remind me in 10 minutes to call Ali\n"
        "- remind me in 2 hours to send the report\n"
        "- remind me tomorrow at 09:00 to submit the form\n"
        "- remind me at 2026-11-01 14:30 to attend the meeting\n"
        "- add goto get milk at 7:30\n"
        "- remind goto get milk at 7:30\n"
        "- list\n"
        "- cancel 12\n"
        "- help\n\n"
        "*Formatting*\n"
        "Use *bold* or _italic_ in reminder text. WhatsApp will render it.\n"
        f"_Times are interpreted in {timezone_name}._"
    )


def _schedule_reminder(
    connection: sqlite3.Connection,
    phone: str,
    message: str,
    due_at: datetime,
    config: dict,
    timezone_name: str,
) -> str:
    try:
        reminder_id = create_reminder(
            connection,
            phone,
            message,
            due_at,
            timezone_name,
            max_message_length=config["MAX_REMINDER_MESSAGE_LENGTH"],
        )
    except ValueError as exc:
        return str(exc)
    return (
        f"Reminder #{reminder_id} scheduled for "
        f"{format_local(to_iso(due_at), timezone_name)} ({timezone_name}): {message}"
    )


def _split_add_alias(text: str) -> tuple[str, str] | None:
    parts = re.split(r"\s+at\s+", text, flags=re.IGNORECASE)
    if len(parts) >= 2:
        when = parts[-1].strip()
        message = " at ".join(parts[:-1]).strip()
        day_match = re.search(r"\b(today|tomorrow)$", message, flags=re.IGNORECASE)
        if day_match:
            when = f"{day_match.group(1)} at {when}"
            message = message[: day_match.start()].strip()
        if message and when:
            return message, when

    match = re.fullmatch(
        r"(?P<message>.+?)\s+in\s+"
        r"(?P<when>\d+\s*(?:minutes?|mins?|m|hours?|hrs?|h|days?|d))",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group("message").strip(), f"in {match.group('when').strip()}"

    match = re.fullmatch(
        r"(?P<message>.+?)\s+"
        r"(?:(?P<day>today|tomorrow)\s+)?"
        r"(?P<when>\d{1,2}:\d{2}(?:\s*[ap]m)?)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        when = match.group("when").strip()
        if match.group("day"):
            when = f"{match.group('day')} {when}"
        return match.group("message").strip(), when

    return None


def handle_incoming_message(
    connection: sqlite3.Connection,
    phone: str,
    text: str,
    config: dict,
) -> str | None:
    normalized_phone = normalize_phone(phone)
    body = " ".join(str(text or "").split())
    if not normalized_phone or not body:
        return None
    if len(body) > 2000:
        return "That message is too long. Please send a shorter command."
    while len(body) >= 2 and body[0] == body[-1] and body[0] in "*_":
        body = body[1:-1].strip()
        if not body:
            return None

    timezone_name = config["APP_TIMEZONE"]
    lowered = body.lower()

    if lowered in {"help", "menu", "?", "commands"}:
        return build_help(timezone_name)

    if lowered in {"ping", "hello", "hi"}:
        return "Reminder bot is online. Reply 'help' for available commands."

    if lowered in {"list", "reminders", "my reminders"}:
        rows = list_reminders(
            connection, status="pending", phone=normalized_phone, limit=10
        )
        if not rows:
            return "You have no pending reminders."
        lines = ["Your pending reminders:"]
        for row in rows:
            lines.append(
                f"#{row['id']} - {format_local(row['due_at'], timezone_name)} - {row['message']}"
            )
        lines.append("Reply 'cancel <id>' to cancel one.")
        return "\n".join(lines)

    cancel_match = re.fullmatch(r"(?:cancel|delete)\s+#?(\d+)", body, flags=re.IGNORECASE)
    if cancel_match:
        reminder_id = int(cancel_match.group(1))
        reminder = get_reminder(connection, reminder_id)
        if reminder is None or reminder["phone"] != normalized_phone:
            return f"Reminder #{reminder_id} was not found."
        if cancel_reminder(connection, reminder_id, phone=normalized_phone):
            return f"Cancelled reminder #{reminder_id}."
        return (
            f"Reminder #{reminder_id} cannot be cancelled "
            f"(status: {reminder['status']})."
        )

    reminder_match = re.match(
        r"^remind\s+me\s+(?P<when>.+?)\s+to\s+(?P<message>.+)$",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if reminder_match:
        when_text = reminder_match.group("when").strip()
        message = reminder_match.group("message").strip()
        due_at = parse_when(when_text, timezone_name)
        if due_at is None:
            return (
                "I could not understand that time. Try: "
                "remind me in 30 minutes to call Ali, or "
                "remind me tomorrow at 09:00 to send the report."
            )
        return _schedule_reminder(
            connection,
            normalized_phone,
            message,
            due_at,
            config,
            timezone_name,
        )

    alias_match = re.match(
        r"^(?:add|remind)\s+(?P<rest>.+)$",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if alias_match:
        rest = alias_match.group("rest").strip()
        rest = re.sub(r"^me\s+", "", rest, flags=re.IGNORECASE)
        rest = re.sub(r"^to\s+", "", rest, flags=re.IGNORECASE)
        parsed = _split_add_alias(rest)
        if parsed is not None:
            message, when_text = parsed
            due_at = parse_when(when_text, timezone_name)
            if due_at is None:
                return (
                    "I could not understand that time. Try: "
                    "add goto get milk at 7:30, or "
                    "remind goto get milk tomorrow at 09:00."
                )
            return _schedule_reminder(
                connection,
                normalized_phone,
                message,
                due_at,
                config,
                timezone_name,
            )

    return "I did not understand that. Reply 'help' for available commands."
