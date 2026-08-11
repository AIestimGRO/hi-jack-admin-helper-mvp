from __future__ import annotations

import sqlite3

from app.services.phone import normalize_phone


def ensure_phone_alias_schema(conn: sqlite3.Connection) -> None:
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
    if row:
        return int(row["id"])
    ensure_phone_alias_schema(conn)
    row = conn.execute(
        """
        SELECT a.client_id
        FROM client_phone_aliases a
        JOIN clients c ON c.id=a.client_id
        WHERE a.phone_local=? AND COALESCE(c.client_status,'existing')<>'deleted'
        LIMIT 1
        """,
        (phone_local,),
    ).fetchone()
    return int(row["client_id"]) if row else None


def phones_for_client(conn: sqlite3.Connection, client_id: int) -> list[str]:
    ensure_phone_alias_schema(conn)
    values: list[str] = []
    row = conn.execute(
        "SELECT phone_local,phone_full,phone_raw FROM clients WHERE id=?",
        (client_id,),
    ).fetchone()
    if row:
        current = normalize_phone(row["phone_local"] or row["phone_full"] or row["phone_raw"])
        if current:
            values.append(current)
    for alias in conn.execute(
        "SELECT phone_local FROM client_phone_aliases WHERE client_id=? ORDER BY id",
        (client_id,),
    ).fetchall():
        value = normalize_phone(alias["phone_local"])
        if value and value not in values:
            values.append(value)
    return values


def assert_phone_available(
    conn: sqlite3.Connection, *, phone_local: str, client_id: int
) -> None:
    normalized = normalize_phone(phone_local)
    if not normalized:
        raise ValueError("Некорректный номер телефона")
    row = conn.execute(
        "SELECT id FROM clients WHERE phone_local=? AND id<>? LIMIT 1",
        (normalized, client_id),
    ).fetchone()
    if row:
        raise ValueError("Этот номер уже привязан к другому аккаунту")
    ensure_phone_alias_schema(conn)
    row = conn.execute(
        "SELECT client_id FROM client_phone_aliases WHERE phone_local=? AND client_id<>? LIMIT 1",
        (normalized, client_id),
    ).fetchone()
    if row:
        raise ValueError("Этот номер уже использовался другим аккаунтом")


def add_phone_alias(
    conn: sqlite3.Connection, *, client_id: int, phone_value: str | None
) -> None:
    phone_local = normalize_phone(phone_value)
    if not phone_local:
        return
    ensure_phone_alias_schema(conn)
    row = conn.execute(
        "SELECT client_id FROM client_phone_aliases WHERE phone_local=?",
        (phone_local,),
    ).fetchone()
    if row and int(row["client_id"]) != int(client_id):
        raise ValueError("Старый номер уже связан с другим аккаунтом")
    conn.execute(
        """
        INSERT OR IGNORE INTO client_phone_aliases(client_id,phone_local)
        VALUES (?,?)
        """,
        (client_id, phone_local),
    )


def remove_phone_aliases(conn: sqlite3.Connection, *, client_id: int) -> None:
    ensure_phone_alias_schema(conn)
    conn.execute("DELETE FROM client_phone_aliases WHERE client_id=?", (client_id,))
