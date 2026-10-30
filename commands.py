from __future__ import annotations

import re
import sqlite3

from db import to_iso
from reminders import cancel_reminder, create_reminder, get_reminder, list_reminders
from utils import format_local, normalize_phone, parse_when


def build_help(timezone_name: str) -> str:
    return (
        "Available commands:\n"
        "- remind me in 10 minutes to call Ali\n"
        "- remind me in 2 hours to send the report\n"
        "- remind me tomorrow at 09:00 to submit the form\n"
        "- remind me at 2026-11-01 14:30 to attend the meeting\n"
        "- list\n"
        "- cancel 12\n"
        f"- help\n\nTimes are interpreted in {timezone_name}."
    )


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
        try:
            reminder_id = create_reminder(
                connection,
                normalized_phone,
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

    return "I did not understand that. Reply 'help' for available commands."
