from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app import create_app
from db import get_db, to_iso, utc_now
from reminders import create_reminder
from utils import format_local


def _future_iso(hours: int = 2) -> str:
    return to_iso(utc_now() + timedelta(hours=hours))


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["timezone"] == "Asia/Karachi"
    assert payload["database_writable"] is True


def test_create_and_list_reminder_api(client, app):
    response = client.post(
        "/api/reminders",
        json={
            "phone": "+92 300 1234567",
            "message": "Call Ali about the project",
            "due_at": _future_iso(),
        },
    )
    assert response.status_code == 201, response.get_data(as_text=True)
    reminder = response.get_json()["reminder"]
    assert reminder["phone"] == "923001234567"
    assert reminder["status"] == "pending"

    listing = client.get("/api/reminders")
    assert listing.status_code == 200
    reminders = listing.get_json()["reminders"]
    assert len(reminders) == 1
    assert reminders[0]["id"] == reminder["id"]


def test_create_reminder_rejects_past_due_date(client):
    response = client.post(
        "/api/reminders",
        json={
            "phone": "923001234567",
            "message": "Too late",
            "due_at": to_iso(utc_now() - timedelta(hours=1)),
        },
    )
    assert response.status_code == 400
    assert "future" in response.get_json()["error"]


def test_cancel_reminder(client, app):
    with app.app_context():
        reminder_id = create_reminder(
            get_db(),
            "923001234567",
            "Cancel me",
            utc_now() + timedelta(hours=1),
            "Asia/Karachi",
        )

    response = client.post(f"/reminders/{reminder_id}/cancel", follow_redirects=True)
    assert response.status_code == 200

    with app.app_context():
        row = get_db().execute(
            "SELECT status FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
    assert row["status"] == "cancelled"


def test_webhook_creates_reminder_and_replies(client, app, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.send_message",
        lambda phone, body, config: sent.append((phone, body)) or {"sent": True},
    )

    payload = {
        "event_type": "message_received",
        "data": {
            "id": "message-1",
            "from": "923001234567@c.us",
            "type": "chat",
            "body": "remind me in 15 minutes to call Ali",
        },
    }
    response = client.post(
        "/webhook/ultramsg?token=hook-token", json=payload
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["reply_sent"] is True
    assert sent and sent[0][0] == "923001234567"
    assert "Reminder #" in sent[0][1]

    with app.app_context():
        rows = get_db().execute("SELECT * FROM reminders").fetchall()
    assert len(rows) == 1
    assert rows[0]["message"] == "call Ali"

    duplicate = client.post(
        "/webhook/ultramsg?token=hook-token", json=payload
    )
    assert duplicate.status_code == 200
    assert duplicate.get_json()["status"] == "duplicate"

    with app.app_context():
        count = get_db().execute("SELECT COUNT(*) AS n FROM reminders").fetchone()["n"]
    assert count == 1


def test_webhook_rejects_invalid_token(client):
    response = client.post(
        "/webhook/ultramsg?token=wrong",
        json={"event_type": "message_received", "data": {}},
    )
    assert response.status_code == 401


def test_task_endpoint_requires_token(client):
    assert client.post("/tasks/run-reminders").status_code == 401
    assert (
        client.post(
            "/tasks/run-reminders", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_task_endpoint_sends_due_reminder(client, app, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "reminders.send_message",
        lambda phone, body, config: sent.append((phone, body)) or {"sent": True},
    )

    with app.app_context():
        reminder_id = create_reminder(
            get_db(),
            "923001234567",
            "Send the report",
            utc_now() + timedelta(hours=1),
            "Asia/Karachi",
        )
        past = to_iso(utc_now() - timedelta(minutes=1))
        get_db().execute(
            "UPDATE reminders SET due_at = ?, next_attempt_at = ? WHERE id = ?",
            (past, past, reminder_id),
        )

    response = client.post(
        "/tasks/run-reminders",
        headers={"Authorization": "Bearer task-token"},
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    summary = response.get_json()
    assert summary["claimed"] == 1
    assert summary["sent"] == 1
    assert sent == [("923001234567", "Send the report")]

    second = client.post(
        "/tasks/run-reminders",
        headers={"Authorization": "Bearer task-token"},
    )
    assert second.get_json()["claimed"] == 0

    with app.app_context():
        row = get_db().execute(
            "SELECT status, sent_at FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
    assert row["status"] == "sent"
    assert row["sent_at"]


def test_task_endpoint_retries_failed_delivery(client, app, monkeypatch):
    def fail(phone, body, config):
        from ultramsg import UltraMsgError

        raise UltraMsgError("simulated failure")

    monkeypatch.setattr("reminders.send_message", fail)

    with app.app_context():
        reminder_id = create_reminder(
            get_db(),
            "923001234567",
            "Will fail",
            utc_now() + timedelta(hours=1),
            "Asia/Karachi",
        )
        past = to_iso(utc_now() - timedelta(minutes=1))
        get_db().execute(
            "UPDATE reminders SET due_at = ?, next_attempt_at = ? WHERE id = ?",
            (past, past, reminder_id),
        )

    response = client.post(
        "/tasks/run-reminders",
        headers={"Authorization": "Bearer task-token"},
    )
    assert response.status_code == 200
    assert response.get_json()["failed"] == 1

    with app.app_context():
        row = get_db().execute(
            "SELECT status, attempts, next_attempt_at, last_error FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert "simulated failure" in row["last_error"]


def test_database_path_is_absolute(client, app):
    with app.app_context():
        response = client.get("/healthz")
    assert response.get_json()["database"].startswith("/")

    assert app.config["DATA_DIR"].startswith("/")
    assert app.config["DATABASE_PATH"].startswith("/")


def test_unknown_webhook_event_is_acknowledged(client):
    response = client.post(
        "/webhook/ultramsg?token=hook-token",
        json={"event_type": "message_ack", "data": {"id": "x"}},
    )
    assert response.status_code == 200
    assert response.get_json()["status"] == "ignored"


def test_webhook_form_encoded_data_is_supported(client, app, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.send_message",
        lambda phone, body, config: sent.append((phone, body)) or {"sent": True},
    )
    payload = json.dumps(
        {
            "event_type": "message_received",
            "data": {
                "id": "form-message-1",
                "from": "923001234567@c.us",
                "type": "chat",
                "body": "list",
            },
        }
    )
    response = client.post(
        "/webhook/ultramsg?token=hook-token",
        data={"data": payload},
    )
    assert response.status_code == 200
    assert sent
    assert "no pending reminders" in sent[0][1].lower()


def test_relative_data_dir_override_does_not_double_paths():
    import shutil
    from pathlib import Path

    data_dir = Path(__file__).resolve().parent.parent / "test-local-data"
    shutil.rmtree(data_dir, ignore_errors=True)
    try:
        application = create_app(
            {
                "TESTING": True,
                "DATA_DIR": "test-local-data",
                "SECRET_KEY": "test",
                "DASHBOARD_PASSWORD": "",
                "ULTRAMSG_WEBHOOK_TOKEN": "x",
                "REMINDER_TASK_TOKEN": "y",
            }
        )
        assert application.config["DATA_DIR"] == str(data_dir.resolve())
        assert application.config["DATABASE_PATH"] == str(
            (data_dir / "reminders.sqlite3").resolve()
        )
        assert "test-local-data/test-local-data" not in application.config[
            "DATABASE_PATH"
        ]
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_task_endpoint_validates_limit(client):
    for value in ("abc", "0", "-1"):
        response = client.post(
            f"/tasks/run-reminders?limit={value}",
            headers={"Authorization": "Bearer task-token"},
        )
        assert response.status_code == 400, value


def test_webhook_failure_releases_deduplication_event(client, app, monkeypatch):
    payload = {
        "event_type": "message_received",
        "data": {
            "id": "retryable-message-1",
            "from": "923001234567@c.us",
            "type": "chat",
            "body": "help",
        },
    }

    def fail(*args, **kwargs):
        raise RuntimeError("simulated handler failure")

    monkeypatch.setattr("app.handle_incoming_message", fail)
    with pytest.raises(RuntimeError):
        client.post("/webhook/ultramsg?token=hook-token", json=payload)

    with app.app_context():
        count = get_db().execute(
            "SELECT COUNT(*) AS n FROM webhook_events"
        ).fetchone()["n"]
    assert count == 0

    sent = []
    monkeypatch.setattr(
        "app.handle_incoming_message", lambda *args, **kwargs: "Recovered"
    )
    monkeypatch.setattr(
        "app.send_message",
        lambda phone, body, config: sent.append((phone, body)) or {"sent": True},
    )
    retry = client.post("/webhook/ultramsg?token=hook-token", json=payload)
    assert retry.status_code == 200
    assert retry.get_json()["status"] == "ok"
    assert sent == [("923001234567", "Recovered")]


def test_webhook_add_and_remind_aliases(client, app, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.send_message",
        lambda phone, body, config: sent.append((phone, body)) or {"sent": True},
    )

    messages = [
        ("alias-1", "add goto get milk at 7:30 pm", "goto get milk"),
        ("alias-2", "remind me to buy milk at 7:30 pm", "buy milk"),
    ]
    for message_id, body, expected in messages:
        response = client.post(
            "/webhook/ultramsg?token=hook-token",
            json={
                "event_type": "message_received",
                "data": {
                    "id": message_id,
                    "from": "923001234567@c.us",
                    "type": "chat",
                    "body": body,
                },
            },
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        assert response.get_json()["reply_sent"] is True

    with app.app_context():
        rows = get_db().execute(
            "SELECT message, due_at FROM reminders ORDER BY id"
        ).fetchall()
    assert [row["message"] for row in rows] == ["goto get milk", "buy milk"]
    assert all(
        "19:30" in format_local(row["due_at"], "Asia/Karachi") for row in rows
    )
    assert len(sent) == 2


def test_formatted_commands_are_recognized(client, app, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.send_message",
        lambda phone, body, config: sent.append((phone, body)) or {"sent": True},
    )

    for message_id, body in [("format-1", "*help*"), ("format-2", "_list_")]:
        response = client.post(
            "/webhook/ultramsg?token=hook-token",
            json={
                "event_type": "message_received",
                "data": {
                    "id": message_id,
                    "from": "923001234567@c.us",
                    "type": "chat",
                    "body": body,
                },
            },
        )
        assert response.status_code == 200, response.get_data(as_text=True)

    assert "How to send commands" in sent[0][1]
    assert "Formatting" in sent[0][1]
    assert "no pending reminders" in sent[1][1].lower()
