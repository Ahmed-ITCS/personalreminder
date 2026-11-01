from __future__ import annotations

import requests

from utils import normalize_phone


class UltraMsgError(RuntimeError):
    pass


def send_message(phone: str, body: str, config: dict) -> dict:
    instance_id = (config.get("ULTRAMSG_INSTANCE_ID") or "").strip()
    token = (config.get("ULTRAMSG_TOKEN") or "").strip()
    if not instance_id or not token:
        raise UltraMsgError("ULTRAMSG_INSTANCE_ID and ULTRAMSG_TOKEN are not configured")

    recipient = normalize_phone(phone, config.get("DEFAULT_COUNTRY_CODE", "92"))
    if not recipient:
        raise UltraMsgError("Recipient phone number is empty")
    if not 8 <= len(recipient) <= 15:
        raise UltraMsgError(
            f"Recipient is not a valid phone number: {recipient}"
        )

    api_url = (config.get("ULTRAMSG_API_URL") or "https://api.ultramsg.com").rstrip("/")
    url = f"{api_url}/{instance_id}/messages/chat"

    try:
        response = requests.post(
            url,
            data={"token": token, "to": recipient, "body": body},
            timeout=config.get("ULTRAMSG_TIMEOUT", 10),
        )
    except requests.RequestException as exc:
        raise UltraMsgError(f"UltraMsg request failed: {exc}") from exc

    if response.status_code >= 400:
        raise UltraMsgError(
            f"UltraMsg returned HTTP {response.status_code}: {response.text[:300]}"
        )

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if payload.get("error"):
        raise UltraMsgError(f"UltraMsg error: {payload['error']}")
    if payload.get("sent") is False:
        raise UltraMsgError(f"UltraMsg did not send the message: {response.text[:300]}")

    return payload
