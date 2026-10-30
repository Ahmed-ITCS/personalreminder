from __future__ import annotations

import socket
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

from flask import current_app

from db import get_db, to_iso, utc_now
from ultramsg import UltraMsgError, send_message
from utils import normalize_phone


class ConfigurationError(RuntimeError):
    pass


def _validate_reminder(
    phone: str,
    message: str,
    due_at: datetime,
    max_message_length: int,
    require_future: bool = True,
) -> tuple[str, str, datetime]:
    normalized_phone = normalize_phone(phone)
    if not normalized_phone:
        raise ValueError("A valid phone number is required")
    if not 8 <= len(normalized_phone) <= 15:
        raise ValueError("Phone number must contain 8 to 15 digits including country code")

    cleaned_message = " ".join(str(message or "").split())
    if not cleaned_message:
        raise ValueError("A reminder message is required")
    if len(cleaned_message) > max_message_length:
        raise ValueError(
            f"Reminder message must be at most {max_message_length} characters"
        )

    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=UTC)
    due_at = due_at.astimezone(UTC)
    if require_future and due_at <= utc_now() - timedelta(minutes=1):
        raise ValueError("Reminder time must be in the future")

    return normalized_phone, cleaned_message, due_at


def create_reminder(
    connection: sqlite3.Connection,
    phone: str,
    message: str,
    due_at: datetime,
    timezone_name: str,
    max_message_length: int = 1000,
) -> int:
    normalized_phone, cleaned_message, due_utc = _validate_reminder(
        phone, message, due_at, max_message_length
    )
    now_iso = to_iso(utc_now())
    due_iso = to_iso(due_utc)
    cursor = connection.execute(
        """
        INSERT INTO reminders (
            phone, message, due_at, next_attempt_at, timezone, status,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            normalized_phone,
            cleaned_message,
            due_iso,
            due_iso,
            timezone_name,
            now_iso,
            now_iso,
        ),
    )
    return int(cursor.lastrowid)


def get_reminder(connection: sqlite3.Connection, reminder_id: int):
    return connection.execute(
        "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
    ).fetchone()


def list_reminders(
    connection: sqlite3.Connection,
    status: str | None = None,
    phone: str | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[object] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if phone:
        clauses.append("phone = ?")
        params.append(normalize_phone(phone))

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    return connection.execute(
        f"SELECT * FROM reminders {where} ORDER BY created_at DESC, id DESC LIMIT ?",
        params,
    ).fetchall()


def count_by_status(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute(
        "SELECT status, COUNT(*) AS total FROM reminders GROUP BY status"
    ).fetchall()
    return {row["status"]: int(row["total"]) for row in rows}


def cancel_reminder(
    connection: sqlite3.Connection, reminder_id: int, phone: str | None = None
) -> bool:
    reminder = get_reminder(connection, reminder_id)
    if reminder is None:
        return False
    if phone is not None and reminder["phone"] != normalize_phone(phone):
        return False

    cursor = connection.execute(
        """
        UPDATE reminders
        SET status = 'cancelled', locked_at = NULL, locked_by = NULL, updated_at = ?
        WHERE id = ? AND status IN ('pending', 'failed')
        """,
        (to_iso(utc_now()), reminder_id),
    )
    return cursor.rowcount > 0


def retry_reminder(connection: sqlite3.Connection, reminder_id: int) -> bool:
    now_iso = to_iso(utc_now())
    cursor = connection.execute(
        """
        UPDATE reminders
        SET status = 'pending', attempts = 0, last_error = NULL,
            next_attempt_at = ?, locked_at = NULL, locked_by = NULL, updated_at = ?
        WHERE id = ? AND status = 'failed'
        """,
        (now_iso, now_iso, reminder_id),
    )
    return cursor.rowcount > 0


def recover_stale_reminders(
    connection: sqlite3.Connection, stale_after_seconds: int
) -> int:
    cutoff = to_iso(utc_now() - timedelta(seconds=stale_after_seconds))
    cursor = connection.execute(
        """
        UPDATE reminders
        SET status = 'pending', locked_at = NULL, locked_by = NULL, updated_at = ?
        WHERE status = 'processing' AND locked_at IS NOT NULL AND locked_at < ?
        """,
        (to_iso(utc_now()), cutoff),
    )
    return cursor.rowcount


def claim_due_reminders(
    connection: sqlite3.Connection, limit: int, worker_id: str
) -> list[dict]:
    """Atomically claim due reminders so overlapping workers cannot send twice."""
    now_iso = to_iso(utc_now())
    connection.execute("BEGIN IMMEDIATE")
    try:
        due_rows = connection.execute(
            """
            SELECT * FROM reminders
            WHERE status = 'pending' AND next_attempt_at <= ?
            ORDER BY next_attempt_at ASC, id ASC
            LIMIT ?
            """,
            (now_iso, max(1, int(limit))),
        ).fetchall()

        claimed: list[dict] = []
        for row in due_rows:
            cursor = connection.execute(
                """
                UPDATE reminders
                SET status = 'processing', locked_at = ?, locked_by = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now_iso, worker_id, now_iso, row["id"]),
            )
            if cursor.rowcount:
                item = dict(row)
                item.update(
                    status="processing",
                    locked_at=now_iso,
                    locked_by=worker_id,
                    updated_at=now_iso,
                )
                claimed.append(item)
        connection.execute("COMMIT")
        return claimed
    except Exception:
        connection.execute("ROLLBACK")
        raise


def mark_sent(
    connection: sqlite3.Connection, reminder_id: int, worker_id: str
) -> bool:
    now_iso = to_iso(utc_now())
    cursor = connection.execute(
        """
        UPDATE reminders
        SET status = 'sent', sent_at = ?, updated_at = ?,
            last_error = NULL, locked_at = NULL, locked_by = NULL
        WHERE id = ? AND status = 'processing' AND locked_by = ?
        """,
        (now_iso, now_iso, reminder_id, worker_id),
    )
    return cursor.rowcount > 0


def mark_failed(
    connection: sqlite3.Connection,
    reminder: dict,
    error: str,
    worker_id: str,
    max_attempts: int,
    retry_base_seconds: int,
) -> bool:
    attempts = int(reminder.get("attempts", 0)) + 1
    now = utc_now()
    now_iso = to_iso(now)

    if attempts >= max_attempts:
        status = "failed"
        next_attempt = reminder["next_attempt_at"]
    else:
        status = "pending"
        backoff = retry_base_seconds * (2 ** (attempts - 1))
        backoff = min(backoff, 6 * 60 * 60)
        next_attempt = to_iso(now + timedelta(seconds=backoff))

    cursor = connection.execute(
        """
        UPDATE reminders
        SET status = ?, attempts = ?, last_error = ?, next_attempt_at = ?,
            locked_at = NULL, locked_by = NULL, updated_at = ?
        WHERE id = ? AND status = 'processing' AND locked_by = ?
        """,
        (
            status,
            attempts,
            str(error)[:1000],
            next_attempt,
            now_iso,
            reminder["id"],
            worker_id,
        ),
    )
    return cursor.rowcount > 0


def due_count(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS total FROM reminders
        WHERE status = 'pending' AND next_attempt_at <= ?
        """,
        (to_iso(utc_now()),),
    ).fetchone()
    return int(row["total"]) if row else 0


def process_due_reminders(
    limit: int | None = None,
    dry_run: bool = False,
    worker_id: str | None = None,
) -> dict:
    """Send due reminders. Must be called inside a Flask application context."""
    config = current_app.config
    db = get_db()
    poll_limit = int(limit or config["REMINDER_POLL_LIMIT"])
    worker = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:10]}"

    if dry_run:
        rows = db.execute(
            """
            SELECT id, phone, message, due_at, attempts
            FROM reminders
            WHERE status = 'pending' AND next_attempt_at <= ?
            ORDER BY next_attempt_at ASC, id ASC
            LIMIT ?
            """,
            (to_iso(utc_now()), poll_limit),
        ).fetchall()
        return {
            "dry_run": True,
            "due": len(rows),
            "reminders": [dict(row) for row in rows],
        }

    recovered = recover_stale_reminders(db, config["REMINDER_STALE_SECONDS"])

    summary = {
        "worker": worker,
        "recovered": recovered,
        "claimed": 0,
        "sent": 0,
        "failed": 0,
        "details": [],
    }

    if due_count(db) == 0:
        return summary

    instance_id = (config.get("ULTRAMSG_INSTANCE_ID") or "").strip()
    token = (config.get("ULTRAMSG_TOKEN") or "").strip()
    if not instance_id or not token:
        raise ConfigurationError(
            "ULTRAMSG_INSTANCE_ID and ULTRAMSG_TOKEN must be configured before sending"
        )

    claimed = claim_due_reminders(db, poll_limit, worker)
    summary["claimed"] = len(claimed)

    for reminder in claimed:
        try:
            send_message(reminder["phone"], reminder["message"], config)
        except UltraMsgError as exc:
            marked = mark_failed(
                db,
                reminder,
                str(exc),
                worker,
                config["REMINDER_MAX_ATTEMPTS"],
                config["REMINDER_RETRY_BASE_SECONDS"],
            )
            summary["failed"] += 1
            summary["details"].append(
                {"id": reminder["id"], "status": "failed" if marked else "skipped"}
            )
            current_app.logger.warning(
                "Reminder %s failed (attempt %s): %s",
                reminder["id"],
                int(reminder.get("attempts", 0)) + 1,
                exc,
            )
        except Exception as exc:
            marked = mark_failed(
                db,
                reminder,
                f"Unexpected error: {exc}",
                worker,
                config["REMINDER_MAX_ATTEMPTS"],
                config["REMINDER_RETRY_BASE_SECONDS"],
            )
            summary["failed"] += 1
            summary["details"].append(
                {"id": reminder["id"], "status": "failed" if marked else "skipped"}
            )
            current_app.logger.exception(
                "Unexpected error sending reminder %s", reminder["id"]
            )
        else:
            marked = mark_sent(db, reminder["id"], worker)
            summary["sent"] += 1
            summary["details"].append(
                {"id": reminder["id"], "status": "sent" if marked else "skipped"}
            )

    return summary
