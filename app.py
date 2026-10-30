from __future__ import annotations

import hmac
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import click
from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from commands import handle_incoming_message
from config import build_config
from db import get_db, initialize_database, to_iso, utc_now
from db import init_app as init_db_app
from reminders import (
    ConfigurationError,
    cancel_reminder,
    count_by_status,
    create_reminder,
    due_count,
    list_reminders,
    process_due_reminders,
    retry_reminder,
)
from ultramsg import UltraMsgError, send_message
from utils import (
    format_local,
    parse_local_datetime,
    phone_from_chat_id,
    to_utc,
)

PUBLIC_ENDPOINTS = {
    "static",
    "healthz",
    "webhook_ultramsg",
    "run_reminders_task",
}

VALID_STATUSES = {"pending", "processing", "sent", "failed", "cancelled"}


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_config(config: dict, overrides: dict | None) -> dict:
    data_dir = Path(config["DATA_DIR"]).expanduser()
    if not data_dir.is_absolute():
        data_dir = Path(__file__).resolve().parent / data_dir
    data_dir = data_dir.resolve()
    config["DATA_DIR"] = str(data_dir)

    if overrides and "DATA_DIR" in overrides and "DATABASE_PATH" not in overrides:
        database_path = data_dir / "reminders.sqlite3"
    else:
        database_path = Path(config["DATABASE_PATH"]).expanduser()
        if not database_path.is_absolute():
            database_path = data_dir / database_path
    config["DATABASE_PATH"] = str(database_path.resolve())
    return config


def create_app(overrides: dict | None = None) -> Flask:
    app = Flask(__name__)
    config = build_config()
    if overrides:
        config.update(overrides)
    app.config.from_mapping(_normalize_config(config, overrides))

    data_dir = Path(app.config["DATA_DIR"])
    database_path = Path(app.config["DATABASE_PATH"])
    data_dir.mkdir(parents=True, exist_ok=True)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    if app.config["TRUST_PROXY"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    initialize_database(database_path)
    init_db_app(app)

    if str(app.config["SECRET_KEY"]).startswith("dev-only-"):
        app.logger.warning("SECRET_KEY is using the development default")
    if not app.config["ULTRAMSG_WEBHOOK_TOKEN"]:
        app.logger.warning("ULTRAMSG_WEBHOOK_TOKEN is empty; webhook requests are unauthenticated")
    if not app.config["REMINDER_TASK_TOKEN"]:
        app.logger.warning("REMINDER_TASK_TOKEN is empty; /tasks/run-reminders is disabled")
    if not app.config["ULTRAMSG_INSTANCE_ID"] or not app.config["ULTRAMSG_TOKEN"]:
        app.logger.warning("UltraMsg credentials are not configured; outbound sends will fail")

    register_routes(app)
    register_cli(app)

    @app.template_filter("localtime")
    def localtime_filter(value: str | None) -> str:
        return format_local(value, app.config["APP_TIMEZONE"])

    return app


def _check_basic_auth(app: Flask) -> Response | None:
    password = app.config["DASHBOARD_PASSWORD"]
    if not password:
        return None

    auth = request.authorization
    username_ok = auth is not None and hmac.compare_digest(
        str(auth.username or ""), str(app.config["DASHBOARD_USERNAME"])
    )
    password_ok = auth is not None and hmac.compare_digest(
        str(auth.password or ""), str(password)
    )
    if username_ok and password_ok:
        return None
    return Response(
        "Authentication required",
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="Reminder admin"'},
    )


def _webhook_token_valid(app: Flask) -> bool:
    expected = app.config["ULTRAMSG_WEBHOOK_TOKEN"]
    if not expected:
        return True
    supplied = (
        request.args.get("token")
        or request.headers.get("X-UltraMsg-Token")
        or ""
    )
    return hmac.compare_digest(str(supplied), str(expected))


def _task_token_valid(app: Flask) -> bool:
    expected = app.config["REMINDER_TASK_TOKEN"]
    if not expected:
        return False
    supplied = (
        request.headers.get("X-Task-Token")
        or request.args.get("token")
        or ""
    )
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    return bool(supplied) and hmac.compare_digest(str(supplied), str(expected))


def _parse_webhook_payload(payload: object) -> dict | None:
    if not isinstance(payload, dict):
        return None

    data = payload.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    if not isinstance(data, dict):
        data = payload

    event_type = str(
        payload.get("event_type") or data.get("event_type") or ""
    ).lower()
    if event_type and event_type not in {"message_received", "message"}:
        return None

    if _truthy(data.get("fromMe") or data.get("from_me")):
        return None

    sender_raw = data.get("from") or data.get("author") or data.get("chatId")
    if not sender_raw or "@g.us" in str(sender_raw):
        return None

    message_type = str(
        data.get("type") or data.get("message_type") or "chat"
    ).lower()
    if message_type not in {"chat", "text", "message"}:
        return None

    body = data.get("body") or data.get("text") or data.get("message")
    if isinstance(body, dict):
        body = body.get("body") or body.get("text")
    if not isinstance(body, str) or not body.strip():
        return None

    event_id = data.get("id") or data.get("message_id") or payload.get("id")

    return {
        "event_type": event_type or "message_received",
        "event_id": str(event_id) if event_id else "",
        "sender": str(sender_raw),
        "body": body,
    }


def _record_webhook_event(connection: sqlite3.Connection, event_id: str) -> bool:
    if not event_id:
        return True
    try:
        connection.execute(
            "INSERT INTO webhook_events (event_id, received_at) VALUES (?, ?)",
            (event_id, to_iso(utc_now())),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def _serialize_reminder(row: sqlite3.Row, timezone_name: str) -> dict:
    return {
        "id": row["id"],
        "phone": row["phone"],
        "message": row["message"],
        "status": row["status"],
        "due_at": row["due_at"],
        "due_at_local": format_local(row["due_at"], timezone_name),
        "next_attempt_at": row["next_attempt_at"],
        "timezone": row["timezone"],
        "attempts": row["attempts"],
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "sent_at": row["sent_at"],
    }


def _api_due_at(value: object, timezone_name: str) -> datetime:
    if value is None or str(value).strip() == "":
        raise ValueError("due_at is required")
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        local = parse_local_datetime(text, timezone_name)
        if local is None:
            raise ValueError(
                "due_at must be ISO 8601 with an offset or a local "
                "date/time such as 2026-11-01 14:30"
            ) from None
        return local
    if parsed.tzinfo is None:
        return to_utc(parsed, timezone_name)
    return parsed.astimezone(UTC)


def register_routes(app: Flask) -> None:
    @app.before_request
    def force_https():
        if not app.config["FORCE_HTTPS"]:
            return None
        if request.endpoint in {"healthz", "static"}:
            return None
        if request.scheme == "https":
            return None
        return redirect(request.url.replace("http://", "https://", 1), code=308)

    @app.before_request
    def dashboard_auth():
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        return _check_basic_auth(app)

    @app.get("/")
    def index():
        connection = get_db()
        reminders = list_reminders(connection, limit=100)
        counts = count_by_status(connection)
        base_url = app.config["APP_BASE_URL"] or request.url_root.rstrip("/")
        return render_template(
            "index.html",
            reminders=reminders,
            counts=counts,
            total=sum(counts.values()),
            timezone=app.config["APP_TIMEZONE"],
            base_url=base_url,
        )

    @app.post("/reminders")
    def create_reminder_route():
        phone = request.form.get("phone", "")
        message = request.form.get("message", "")
        due_text = request.form.get("due_at", "")
        timezone_name = app.config["APP_TIMEZONE"]

        try:
            if not due_text:
                raise ValueError("A due date and time is required")
            due_local = datetime.strptime(due_text, "%Y-%m-%dT%H:%M")
            due_at = to_utc(due_local, timezone_name)
            reminder_id = create_reminder(
                get_db(),
                phone,
                message,
                due_at,
                timezone_name,
                max_message_length=app.config["MAX_REMINDER_MESSAGE_LENGTH"],
            )
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash(f"Reminder #{reminder_id} scheduled.", "success")
        return redirect(url_for("index"))

    @app.post("/reminders/<int:reminder_id>/cancel")
    def cancel_reminder_route(reminder_id: int):
        if cancel_reminder(get_db(), reminder_id):
            flash(f"Reminder #{reminder_id} cancelled.", "success")
        else:
            flash(f"Reminder #{reminder_id} could not be cancelled.", "error")
        return redirect(url_for("index"))

    @app.post("/reminders/<int:reminder_id>/retry")
    def retry_reminder_route(reminder_id: int):
        if retry_reminder(get_db(), reminder_id):
            flash(f"Reminder #{reminder_id} queued for retry.", "success")
        else:
            flash(f"Reminder #{reminder_id} could not be retried.", "error")
        return redirect(url_for("index"))

    @app.get("/api/reminders")
    def api_list_reminders():
        status = request.args.get("status")
        if status and status not in VALID_STATUSES:
            return jsonify({"error": "unknown status"}), 400
        try:
            limit = int(request.args.get("limit", "50"))
        except ValueError:
            return jsonify({"error": "limit must be an integer"}), 400
        timezone_name = app.config["APP_TIMEZONE"]
        rows = list_reminders(get_db(), status=status, limit=limit)
        return jsonify(
            {"reminders": [_serialize_reminder(row, timezone_name) for row in rows]}
        )

    @app.post("/api/reminders")
    def api_create_reminder():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "JSON body is required"}), 400

        timezone_name = app.config["APP_TIMEZONE"]
        try:
            due_at = _api_due_at(payload.get("due_at"), timezone_name)
            reminder_id = create_reminder(
                get_db(),
                str(payload.get("phone", "")),
                str(payload.get("message", "")),
                due_at,
                timezone_name,
                max_message_length=app.config["MAX_REMINDER_MESSAGE_LENGTH"],
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        row = get_db().execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
        return (
            jsonify({"reminder": _serialize_reminder(row, timezone_name)}),
            201,
        )

    @app.route("/webhook/ultramsg", methods=["GET", "POST"])
    def webhook_ultramsg():
        if request.method == "GET":
            return jsonify({"status": "ready", "endpoint": "/webhook/ultramsg"})

        if not _webhook_token_valid(app):
            return jsonify({"error": "invalid webhook token"}), 401

        payload = request.get_json(silent=True)
        if payload is None:
            raw_data = request.form.get("data")
            if raw_data:
                try:
                    payload = json.loads(raw_data)
                except ValueError:
                    payload = request.form.to_dict(flat=True)
            else:
                payload = request.form.to_dict(flat=True)

        message = _parse_webhook_payload(payload)
        if message is None:
            return jsonify({"status": "ignored"})

        event_id = message["event_id"]
        if event_id:
            event_id = f"{app.config['ULTRAMSG_INSTANCE_ID']}:{event_id}"

        connection = get_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if not _record_webhook_event(connection, event_id):
                connection.rollback()
                return jsonify({"status": "duplicate"})
            sender = phone_from_chat_id(
                message["sender"], app.config["DEFAULT_COUNTRY_CODE"]
            )
            reply = handle_incoming_message(
                connection, sender, message["body"], app.config
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        reply_sent = False
        if reply:
            try:
                send_message(sender, reply, app.config)
                reply_sent = True
            except UltraMsgError as exc:
                app.logger.error("Could not send webhook reply to %s: %s", sender, exc)

        return jsonify({"status": "ok", "reply_sent": reply_sent})

    @app.post("/tasks/run-reminders")
    def run_reminders_task():
        if not app.config["REMINDER_TASK_TOKEN"]:
            return jsonify({"error": "task endpoint is disabled"}), 503
        if not _task_token_valid(app):
            return jsonify({"error": "unauthorized"}), 401
        limit = request.args.get("limit")
        if limit is not None:
            try:
                limit = int(limit)
            except ValueError:
                return jsonify({"error": "limit must be an integer"}), 400
            if limit < 1:
                return jsonify({"error": "limit must be at least 1"}), 400
        try:
            summary = process_due_reminders(limit=limit)
        except ConfigurationError as exc:
            return jsonify({"error": str(exc)}), 503
        except sqlite3.Error as exc:
            app.logger.exception("Database error while processing reminders")
            return jsonify({"error": f"database error: {exc}"}), 500
        return jsonify(summary)

    @app.get("/healthz")
    def healthz():
        try:
            connection = get_db()
            connection.execute("SELECT 1").fetchone()
            counts = count_by_status(connection)
            due = due_count(connection)
            database_path = Path(app.config["DATABASE_PATH"])
            writable = os.access(database_path.parent, os.W_OK)
        except sqlite3.Error as exc:
            app.logger.exception("Health check failed")
            return jsonify({"status": "error", "error": str(exc)}), 503

        return jsonify(
            {
                "status": "ok",
                "timezone": app.config["APP_TIMEZONE"],
                "database": str(database_path),
                "database_writable": writable,
                "queue": counts,
                "due_now": due,
            }
        )


def register_cli(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db_command() -> None:
        initialize_database(app.config["DATABASE_PATH"])
        click.echo(f"Database initialized at {app.config['DATABASE_PATH']}")

