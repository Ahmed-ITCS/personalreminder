from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _as_int(name: str, default: int, minimum: int | None = None) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}, got {value}")
    return value


def _as_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _resolve_data_dir(raw: str | None) -> Path:
    if raw is None:
        path = Path.home() / "ultramsg-reminders-data"
    else:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = PROJECT_DIR / path
    return path.resolve()


def build_config() -> dict:
    """Build runtime configuration from environment variables and .env."""
    data_dir = _resolve_data_dir(_env("DATA_DIR"))

    raw_db_path = _env("DATABASE_PATH")
    if raw_db_path is None:
        db_path = data_dir / "reminders.sqlite3"
    else:
        db_path = Path(raw_db_path).expanduser()
        if not db_path.is_absolute():
            db_path = data_dir / db_path
    db_path = db_path.resolve()

    timezone_name = _env("APP_TIMEZONE", "Asia/Karachi") or "Asia/Karachi"
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"APP_TIMEZONE is not a valid IANA timezone: {timezone_name!r}"
        ) from exc

    force_https = _as_bool("FORCE_HTTPS", False)
    webhook_token = _env("ULTRAMSG_WEBHOOK_TOKEN", "") or ""

    return {
        "SECRET_KEY": _env("SECRET_KEY", "dev-only-change-this-secret"),
        "DATA_DIR": str(data_dir),
        "DATABASE_PATH": str(db_path),
        "APP_TIMEZONE": timezone_name,
        "APP_BASE_URL": (_env("APP_BASE_URL", "") or "").rstrip("/"),
        "DASHBOARD_USERNAME": _env("DASHBOARD_USERNAME", "admin") or "admin",
        "DASHBOARD_PASSWORD": _env("DASHBOARD_PASSWORD", "") or "",
        "FORCE_HTTPS": force_https,
        "TRUST_PROXY": _as_bool("TRUST_PROXY", False),
        "SESSION_COOKIE_SECURE": force_https,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "MAX_CONTENT_LENGTH": 1024 * 1024,
        "ULTRAMSG_INSTANCE_ID": _env("ULTRAMSG_INSTANCE_ID", "") or "",
        "ULTRAMSG_TOKEN": _env("ULTRAMSG_TOKEN", "") or "",
        "ULTRAMSG_API_URL": _env("ULTRAMSG_API_URL", "https://api.ultramsg.com")
        or "https://api.ultramsg.com",
        "ULTRAMSG_TIMEOUT": _as_int("ULTRAMSG_TIMEOUT", 10, minimum=1),
        "ULTRAMSG_WEBHOOK_TOKEN": webhook_token,
        "REMINDER_TASK_TOKEN": _env("REMINDER_TASK_TOKEN", "") or "",
        "REMINDER_POLL_LIMIT": _as_int("REMINDER_POLL_LIMIT", 10, minimum=1),
        "REMINDER_MAX_ATTEMPTS": _as_int("REMINDER_MAX_ATTEMPTS", 6, minimum=1),
        "REMINDER_RETRY_BASE_SECONDS": _as_int(
            "REMINDER_RETRY_BASE_SECONDS", 300, minimum=30
        ),
        "REMINDER_STALE_SECONDS": _as_int(
            "REMINDER_STALE_SECONDS", 900, minimum=60
        ),
        "DEFAULT_COUNTRY_CODE": _env("DEFAULT_COUNTRY_CODE", "92") or "92",
        "MAX_REMINDER_MESSAGE_LENGTH": _as_int(
            "MAX_REMINDER_MESSAGE_LENGTH", 1000, minimum=1
        ),
    }
