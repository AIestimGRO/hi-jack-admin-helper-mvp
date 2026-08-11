from __future__ import annotations

import sqlite3

from app.services.phone import normalize_phone


def ensure_phone_alias_schema(conn: sqlite3.Connection) -> None:
    """Keep the legacy table compatible, but do not use aliases for identity matching."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS client_phone_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            phone_local TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL DEFAULT 'phone_change',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_client_phone_aliases_client
        ON client_phone_aliases(client_id, created_at DESC)
        """
    )


def resolve_client_id_by_phone(
    conn: sqlite3.Connection, phone_value: str | None
) -> int | None:
    phone_local = normalize_phone(phone_value)
    if not phone_local:
        return None
    row = conn.execute(
        """
        SELECT id FROM clients
        WHERE phone_local=? AND COALESCE(client_status,'existing')<>'deleted'
        LIMIT 1
        """,
        (phone_local,),
    ).fetchone()
    return int(row["id"]) if row else None


def phones_for_client(conn: sqlite3.Connection, client_id: int) -> list[str]:
    row = conn.execute(
        "SELECT phone_local,phone_full,phone_raw FROM clients WHERE id=?",
        (client_id,),
    ).fetchone()
    if not row:
        return []
    current = normalize_phone(row["phone_local"] or row["phone_full"] or row["phone_raw"])
    return [current] if current else []


def assert_phone_available(
    conn: sqlite3.Connection, *, phone_local: str, client_id: int
) -> None:
    normalized = normalize_phone(phone_local)
    if not normalized:
        raise ValueError("Некорректный номер телефона")
    row = conn.execute(
        """
        SELECT id FROM clients
        WHERE phone_local=? AND id<>?
          AND COALESCE(client_status,'existing')<>'deleted'
        LIMIT 1
        """,
        (normalized, client_id),
    ).fetchone()
    if row:
        raise ValueError("Этот номер уже привязан к другому аккаунту")


def add_phone_alias(
    conn: sqlite3.Connection, *, client_id: int, phone_value: str | None
) -> None:
    """Legacy no-op: old numbers are deliberately released after a phone change."""
    _ = (conn, client_id, phone_value)


def remove_phone_aliases(conn: sqlite3.Connection, *, client_id: int) -> None:
    ensure_phone_alias_schema(conn)
    conn.execute("DELETE FROM client_phone_aliases WHERE client_id=?", (client_id,))
