from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from app.db import connect
from app.telegram_transport import TelegramTransportError, _default_send_message


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely test JACKSIDE Bot delivery to exactly one linked client."
    )
    parser.add_argument(
        "--db",
        default="data/club_tools.sqlite3",
        help="Path to the Admin Helper SQLite database.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--list",
        action="store_true",
        help="List Telegram-linked clients without sending anything.",
    )
    group.add_argument(
        "--client-id",
        type=int,
        help="Send one test message to this Admin Helper client ID.",
    )
    parser.add_argument(
        "--text",
        default="JACKSIDE Bot test: Telegram delivery is connected successfully.",
        help="Test message text.",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help="Must be exactly SEND-ONE for delivery mode.",
    )
    return parser


def _linked_clients(db_path: Path):
    with connect(db_path) as conn:
        return conn.execute(
            """
            SELECT
                id,
                COALESCE(NULLIF(nickname,''),NULLIF(first_name,''),NULLIF(username,''),'')
                    AS display_name,
                username,
                COALESCE(NULLIF(telegram_user_id,''),NULLIF(telegram_id,''))
                    AS telegram_chat_id
            FROM clients
            WHERE COALESCE(telegram_user_id,'')<>''
               OR COALESCE(telegram_id,'')<>''
            ORDER BY id
            """
        ).fetchall()


def _list_clients(db_path: Path) -> int:
    rows = _linked_clients(db_path)
    if not rows:
        print("No Telegram-linked clients found.")
        return 0
    print("client_id\tdisplay\tusername\ttelegram_chat_id")
    for row in rows:
        print(
            f"{int(row['id'])}\t"
            f"{str(row['display_name'] or '-')}\t"
            f"{str(row['username'] or '-')}\t"
            f"{str(row['telegram_chat_id'] or '-')}"
        )
    return 0


def _send_one(db_path: Path, client_id: int, text: str, confirm: str) -> int:
    if confirm != "SEND-ONE":
        print("Refusing to send. Pass --confirm SEND-ONE.", file=sys.stderr)
        return 2

    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT
                id,
                COALESCE(NULLIF(nickname,''),NULLIF(first_name,''),NULLIF(username,''),'')
                    AS display_name,
                username,
                COALESCE(NULLIF(telegram_user_id,''),NULLIF(telegram_id,''))
                    AS telegram_chat_id
            FROM clients
            WHERE id=?
            """,
            (client_id,),
        ).fetchone()

    if not row:
        print("Client not found.", file=sys.stderr)
        return 2
    chat_id = str(row["telegram_chat_id"] or "").strip()
    if not chat_id:
        print("Client has no linked Telegram ID.", file=sys.stderr)
        return 2

    print(
        "Target: "
        f"client_id={int(row['id'])}, "
        f"display={str(row['display_name'] or '-')}, "
        f"username={str(row['username'] or '-')}, "
        f"telegram_chat_id={chat_id}"
    )
    token = getpass.getpass("JACKSIDE Bot token (hidden): ").strip()
    if not token:
        print("Bot token is required.", file=sys.stderr)
        return 2

    try:
        result = _default_send_message(
            token,
            chat_id,
            {"text": str(text or "").strip()},
            10.0,
        )
    except TelegramTransportError as exc:
        print(f"Telegram test failed: {exc}", file=sys.stderr)
        return 1

    print(
        "Telegram test sent successfully. "
        f"message_id={str(result.get('message_id') or '-')}."
    )
    return 0


def main() -> int:
    args = _parser().parse_args()
    db_path = Path(args.db)
    if args.list:
        return _list_clients(db_path)
    return _send_one(db_path, int(args.client_id), args.text, args.confirm)


if __name__ == "__main__":
    raise SystemExit(main())
