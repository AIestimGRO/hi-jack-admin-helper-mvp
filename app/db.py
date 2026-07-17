from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_user_id TEXT,
    telegram_id TEXT,
    referrer_app_user_id TEXT,
    nickname TEXT,
    username TEXT,
    first_name TEXT,
    phone_raw TEXT,
    phone_full TEXT,
    phone_local TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_clients_phone_local
    ON clients(phone_local) WHERE phone_local IS NOT NULL AND phone_local <> '';
CREATE UNIQUE INDEX IF NOT EXISTS ux_clients_app_user_id
    ON clients(app_user_id) WHERE app_user_id IS NOT NULL AND app_user_id <> '';
CREATE INDEX IF NOT EXISTS ix_clients_search ON clients(first_name, nickname, username);

CREATE TABLE IF NOT EXISTS preference_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('counter', 'percent')),
    is_active INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 100,
    created_by_admin_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS client_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    preference_type_id INTEGER NOT NULL REFERENCES preference_types(id),
    balance_int INTEGER NOT NULL DEFAULT 0 CHECK(balance_int >= 0),
    percent_value REAL NOT NULL DEFAULT 0 CHECK(percent_value >= 0 AND percent_value <= 100),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(client_id, preference_type_id)
);

CREATE TABLE IF NOT EXISTS preference_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    preference_type_id INTEGER NOT NULL REFERENCES preference_types(id),
    operation_type TEXT NOT NULL,
    delta_int INTEGER,
    old_balance_int INTEGER,
    new_balance_int INTEGER,
    old_percent_value REAL,
    new_percent_value REAL,
    reason TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    admin_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_preference_log_client ON preference_log(client_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_preference_log_created ON preference_log(created_at DESC);

CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    total_rows INTEGER NOT NULL,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    phone_error_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    admin_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    pin_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'master_admin')),
    is_active INTEGER NOT NULL DEFAULT 1,
    session_version INTEGER NOT NULL DEFAULT 1,
    last_login_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER REFERENCES admins(id),
    admin_name TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_admin_audit_created ON admin_audit_log(created_at DESC);
"""

PREFERENCE_TYPES = (
    ("free_entry", "ФриЭнтри", "counter"),
    ("free_reentry", "ФриРеЭнтри", "counter"),
    ("free_addon", "ФриАдон", "counter"),
    ("bar_hookah_discount_percent", "Скидка на Бар&Кальян", "percent"),
)


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "preference_types", "position INTEGER NOT NULL DEFAULT 100")
        _ensure_column(conn, "preference_types", "created_by_admin_id INTEGER")
        _ensure_column(conn, "preference_types", "created_at TEXT")
        _ensure_column(conn, "preference_types", "updated_at TEXT")
        _ensure_column(conn, "admins", "session_version INTEGER NOT NULL DEFAULT 1")
        conn.execute("UPDATE preference_types SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP), updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP)")
        conn.executemany(
            "INSERT OR IGNORE INTO preference_types(code, title, kind) VALUES (?, ?, ?)",
            PREFERENCE_TYPES,
        )


def _ensure_column(conn: sqlite3.Connection, table: str, definition: str) -> None:
    column = definition.split()[0]
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


@contextmanager
def transaction(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
