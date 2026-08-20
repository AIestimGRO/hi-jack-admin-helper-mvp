from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from app.telegram_transport import TelegramPermanentError, validate_private_chat_id


def _invalid_chat_id(value: object) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        validate_private_chat_id(raw)
    except TelegramPermanentError:
        return True
    return False


def repair_legacy_telegram_ids(db_path: Path, *, apply: bool) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, telegram_user_id
            FROM clients
            WHERE COALESCE(telegram_user_id, '') <> ''
            ORDER BY id
            """
        ).fetchall()
        invalid = [row for row in rows if _invalid_chat_id(row["telegram_user_id"])]
        if not apply:
            return {"scanned": len(rows), "invalid": len(invalid), "cleared": 0}

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_legacy_identity_archive(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                legacy_telegram_user_id TEXT NOT NULL,
                archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(client_id, legacy_telegram_user_id)
            )
            """
        )
        cleared = 0
        for row in invalid:
            client_id = int(row["id"])
            legacy_id = str(row["telegram_user_id"])
            conn.execute(
                """
                INSERT OR IGNORE INTO telegram_legacy_identity_archive(
                    client_id, legacy_telegram_user_id
                ) VALUES (?, ?)
                """,
                (client_id, legacy_id),
            )
            cursor = conn.execute(
                """
                UPDATE clients
                SET telegram_user_id=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND telegram_user_id=?
                """,
                (client_id, legacy_id),
            )
            cleared += int(cursor.rowcount or 0)
        conn.commit()
        return {"scanned": len(rows), "invalid": len(invalid), "cleared": cleared}
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Archive unusable legacy Telegram OIDC subject values and clear them "
            "so users can relink with a Bot API user ID."
        )
    )
    parser.add_argument("db_path", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    result = repair_legacy_telegram_ids(args.db_path, apply=args.apply)
    mode = "APPLY" if args.apply else "DRY_RUN"
    print(
        f"mode={mode} scanned={result['scanned']} invalid={result['invalid']} "
        f"cleared={result['cleared']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
