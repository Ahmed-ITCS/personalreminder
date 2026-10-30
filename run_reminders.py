#!/usr/bin/env python3
"""Reminder queue dispatcher.

Two modes:

* Local (default): opens the SQLite database directly, claims due rows atomically
  and sends them through UltraMsg. Use this when the script runs on the same
  machine/disk as the Flask app.
* Remote (--remote): calls the Flask app's token-protected task endpoint. Use
  this on Render, where Cron Jobs cannot attach or read the web service's
  persistent disk.

Both modes are safe against overlapping runs: local mode uses an OS file lock
plus an atomic SQLite claim, and remote mode relies on the same atomic claim
inside the web service (Render additionally guarantees a single run per cron
service).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from pathlib import Path

import requests

from app import create_app
from config import build_config
from reminders import ConfigurationError, process_due_reminders


def _acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w", encoding="utf-8")  # noqa: SIM115
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def _run_local(args: argparse.Namespace) -> int:
    config = build_config()
    app = create_app()
    lock_handle = _acquire_lock(Path(config["DATA_DIR"]) / "run_reminders.lock")
    if lock_handle is None:
        print(json.dumps({"status": "skipped", "reason": "another run is active"}))
        return 0

    try:
        with app.app_context():
            try:
                summary = process_due_reminders(
                    limit=args.limit, dry_run=args.dry_run
                )
            except ConfigurationError as exc:
                print(json.dumps({"status": "error", "error": str(exc)}))
                return 1
        print(json.dumps({"status": "ok", **summary}, indent=2, default=str))
        return 0
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


def _run_remote(args: argparse.Namespace) -> int:
    if args.dry_run:
        print("--dry-run is only supported in local mode", file=sys.stderr)
        return 2

    config = build_config()
    url = os.environ.get("REMINDER_TASK_URL", "").strip()
    if not url and config["APP_BASE_URL"]:
        url = f"{config['APP_BASE_URL']}/tasks/run-reminders"
    token = (
        os.environ.get("REMINDER_TASK_TOKEN", "").strip()
        or config["REMINDER_TASK_TOKEN"]
    )
    if not url:
        print(
            "REMINDER_TASK_URL is required (for example "
            "https://your-service.onrender.com/tasks/run-reminders)",
            file=sys.stderr,
        )
        return 2
    if not token:
        print("REMINDER_TASK_TOKEN is required", file=sys.stderr)
        return 2

    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "ultramsg-reminder-dispatcher/1.0",
            },
            params={"limit": args.limit} if args.limit else None,
            timeout=args.timeout,
        )
    except requests.RequestException as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 1

    try:
        payload = response.json()
    except ValueError:
        payload = {"body": response.text[:1000]}

    print(json.dumps(payload, indent=2, default=str))
    if response.status_code >= 400:
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Send due UltraMsg reminders")
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Call the web service task endpoint instead of opening SQLite directly",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum reminders to process in this run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List due reminders without claiming or sending them",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="HTTP timeout in seconds for remote mode (default: 180)",
    )
    args = parser.parse_args()

    if args.remote:
        return _run_remote(args)
    return _run_local(args)


if __name__ == "__main__":
    raise SystemExit(main())
