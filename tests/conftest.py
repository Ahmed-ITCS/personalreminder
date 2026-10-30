from __future__ import annotations

import pytest

from app import create_app


@pytest.fixture()
def app(tmp_path):
    data_dir = tmp_path / "data"
    application = create_app(
        {
            "TESTING": True,
            "DATA_DIR": str(data_dir),
            "DATABASE_PATH": str(data_dir / "test.sqlite3"),
            "SECRET_KEY": "test-secret",
            "APP_TIMEZONE": "Asia/Karachi",
            "ULTRAMSG_INSTANCE_ID": "instance-test",
            "ULTRAMSG_TOKEN": "token-test",
            "ULTRAMSG_WEBHOOK_TOKEN": "hook-token",
            "REMINDER_TASK_TOKEN": "task-token",
            "DASHBOARD_PASSWORD": "",
            "FORCE_HTTPS": False,
            "TRUST_PROXY": False,
            "REMINDER_POLL_LIMIT": 10,
        }
    )
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()
