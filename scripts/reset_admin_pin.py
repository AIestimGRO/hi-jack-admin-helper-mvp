#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import transaction  # noqa: E402
from app.services.auth import hash_pin, validate_username  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset an administrator PIN offline")
    parser.add_argument("--db", default="data/club_tools.sqlite3")
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    username = validate_username(args.username)
    pin = getpass.getpass("New PIN: ")
    confirmation = getpass.getpass("Repeat PIN: ")
    if pin != confirmation:
        raise SystemExit("PIN values do not match")
    encoded = hash_pin(pin)
    with transaction(Path(args.db)) as conn:
        row = conn.execute("SELECT id FROM admins WHERE username = ?", (username,)).fetchone()
        if not row:
            raise SystemExit("Administrator not found")
        conn.execute(
            "UPDATE admins SET pin_hash = ?, is_active = 1, session_version = session_version + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (encoded, row["id"]),
        )
    print(f"PIN updated for {username}")


if __name__ == "__main__":
    main()
