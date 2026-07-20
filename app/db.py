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

CREATE TABLE IF NOT EXISTS quiz_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    bonus_preference_code TEXT,
    bonus_amount INTEGER NOT NULL DEFAULT 0 CHECK(bonus_amount >= 0),
    pass_score INTEGER NOT NULL DEFAULT 0 CHECK(pass_score >= 0),
    question_time_limit_seconds INTEGER NOT NULL DEFAULT 20 CHECK(question_time_limit_seconds >= 0),
    quiz_time_limit_seconds INTEGER NOT NULL DEFAULT 120 CHECK(quiz_time_limit_seconds >= 0),
    active_from TEXT,
    active_until TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS quiz_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_code TEXT NOT NULL,
    code TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('single_choice', 'multi_choice', 'text')),
    title TEXT NOT NULL,
    placeholder TEXT,
    required INTEGER NOT NULL DEFAULT 1,
    points INTEGER NOT NULL DEFAULT 1 CHECK(points >= 0),
    time_limit_seconds INTEGER,
    position INTEGER NOT NULL DEFAULT 100,
    is_active INTEGER NOT NULL DEFAULT 0,
    created_by_admin_id INTEGER REFERENCES admins(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(campaign_code, code)
);
CREATE INDEX IF NOT EXISTS ix_quiz_questions_campaign ON quiz_questions(campaign_code, position, id);

CREATE TABLE IF NOT EXISTS quiz_options (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES quiz_questions(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    text TEXT NOT NULL,
    is_correct INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 100,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(question_id, code)
);
CREATE INDEX IF NOT EXISTS ix_quiz_options_question ON quiz_options(question_id, position, id);

CREATE TABLE IF NOT EXISTS quiz_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_code TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    questions_snapshot_json TEXT NOT NULL,
    answers_json TEXT NOT NULL DEFAULT '{}',
    current_index INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'in_progress' CHECK(status IN ('in_progress', 'awaiting_contact', 'submitted', 'expired')),
    question_started_at TEXT,
    question_deadline_at TEXT,
    attempt_deadline_at TEXT,
    completed_questions_at TEXT,
    ip_hash TEXT NOT NULL,
    user_agent TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_quiz_attempts_ip ON quiz_attempts(ip_hash, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_quiz_attempts_status ON quiz_attempts(status, created_at DESC);

CREATE TABLE IF NOT EXISTS quiz_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER UNIQUE REFERENCES quiz_attempts(id),
    campaign_code TEXT NOT NULL,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    phone_raw TEXT NOT NULL,
    phone_local TEXT NOT NULL,
    name TEXT,
    username TEXT,
    nickname TEXT,
    answers_json TEXT NOT NULL,
    questions_snapshot_json TEXT,
    score REAL,
    max_score INTEGER NOT NULL DEFAULT 0,
    correct_count INTEGER NOT NULL DEFAULT 0,
    max_correct_count INTEGER NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL DEFAULT 0,
    bonus_granted INTEGER NOT NULL DEFAULT 0,
    bonus_pending INTEGER NOT NULL DEFAULT 0,
    bonus_type TEXT,
    is_duplicate INTEGER NOT NULL DEFAULT 0,
    is_new_client INTEGER NOT NULL DEFAULT 0,
    quiz_referrer_id TEXT,
    source TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    user_agent TEXT,
    ip_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_quiz_submissions_created ON quiz_submissions(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_quiz_submissions_campaign ON quiz_submissions(campaign_code, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_quiz_submissions_phone ON quiz_submissions(phone_local, campaign_code);
CREATE INDEX IF NOT EXISTS ix_quiz_submissions_ip ON quiz_submissions(ip_hash, created_at DESC);
"""

PREFERENCE_TYPES = (
    ("free_entry", "ФриЭнтри", "counter"),
    ("free_reentry", "ФриРеЭнтри", "counter"),
    ("free_addon", "ФриАдон", "counter"),
    ("bar_hookah_discount_percent", "Скидка на Бар&Кальян", "percent"),
)

QUIZ_CAMPAIGNS = (
    ("default", "Опрос Hi, Jack!"),
    ("summer", "Летний опрос"),
    ("honor_more", "Honor & More"),
    ("ladies", "Hi, Ladies!"),
    ("badbeat", "Bad Beat"),
    ("new_player", "Новый игрок"),
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
        _ensure_column(conn, "quiz_submissions", "is_new_client INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "quiz_submissions", "max_score INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "quiz_submissions", "questions_snapshot_json TEXT")
        _ensure_column(conn, "quiz_submissions", "correct_count INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "quiz_submissions", "max_correct_count INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "quiz_submissions", "passed INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "quiz_campaigns", "pass_score INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "quiz_campaigns", "question_time_limit_seconds INTEGER NOT NULL DEFAULT 20")
        _ensure_column(conn, "quiz_campaigns", "quiz_time_limit_seconds INTEGER NOT NULL DEFAULT 120")
        _ensure_column(conn, "quiz_campaigns", "active_from TEXT")
        _ensure_column(conn, "quiz_campaigns", "active_until TEXT")
        _ensure_column(conn, "quiz_questions", "time_limit_seconds INTEGER")
        _ensure_column(conn, "quiz_submissions", "attempt_id INTEGER")
        _ensure_column(conn, "quiz_attempts", "attempt_deadline_at TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_quiz_submissions_attempt ON quiz_submissions(attempt_id) WHERE attempt_id IS NOT NULL"
        )
        conn.execute(
            """
            UPDATE quiz_submissions SET passed = 1
            WHERE campaign_code IN (SELECT code FROM quiz_campaigns WHERE pass_score = 0)
            """
        )
        conn.execute("UPDATE preference_types SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP), updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP)")
        conn.executemany(
            "INSERT OR IGNORE INTO preference_types(code, title, kind) VALUES (?, ?, ?)",
            PREFERENCE_TYPES,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO quiz_campaigns(code, title) VALUES (?, ?)",
            QUIZ_CAMPAIGNS,
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
