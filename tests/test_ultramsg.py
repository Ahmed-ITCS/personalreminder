from __future__ import annotations

import pytest

from ultramsg import UltraMsgError, send_message


class _Response:
    status_code = 200

    def json(self):
        return {"sent": True}


def _config():
    return {
        "ULTRAMSG_INSTANCE_ID": "instance1",
        "ULTRAMSG_TOKEN": "token",
        "DEFAULT_COUNTRY_CODE": "92",
    }


def test_send_message_keeps_group_chat_id(monkeypatch):
    calls = []

    def fake_post(url, data, timeout):
        calls.append(data)
        return _Response()

    monkeypatch.setattr("ultramsg.requests.post", fake_post)

    send_message("120363351693927930@g.us", "hello", _config())

    assert calls[0]["to"] == "120363351693927930@g.us"


def test_send_message_normalizes_phone(monkeypatch):
    calls = []

    def fake_post(url, data, timeout):
        calls.append(data)
        return _Response()

    monkeypatch.setattr("ultramsg.requests.post", fake_post)

    send_message("+92 300 1234567", "hello", _config())

    assert calls[0]["to"] == "923001234567"


def test_send_message_rejects_invalid_recipient(monkeypatch):
    monkeypatch.setattr(
        "ultramsg.requests.post",
        lambda *args, **kwargs: pytest.fail("should not call the API"),
    )

    with pytest.raises(UltraMsgError):
        send_message("123", "hello", _config())
