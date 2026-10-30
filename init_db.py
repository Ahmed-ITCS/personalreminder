#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

from app import create_app
from db import get_db


def main() -> int:
    app = create_app()
    with app.app_context():
        connection = get_db()
        connection.execute("SELECT 1").fetchone()
        row = connection.execute(
            "SELECT COUNT(*) AS total FROM reminders"
        ).fetchone()
        total = int(row["total"]) if row else 0

    data_dir = Path(app.config["DATA_DIR"])
    database_path = Path(app.config["DATABASE_PATH"])
    writable = os.access(data_dir, os.W_OK) and os.access(database_path, os.W_OK)

    print(f"Database ready: {database_path}")
    print(f"Data directory: {data_dir}")
    print(f"Timezone: {app.config['APP_TIMEZONE']}")
    print(f"Reminder rows: {total}")
    print(f"Writable: {'yes' if writable else 'no'}")

    if not writable:
        print("ERROR: the data directory or database file is not writable.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
