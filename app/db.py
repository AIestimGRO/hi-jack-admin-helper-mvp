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
    telegram_user_id TEXT,
    email TEXT,
    email_normalized TEXT,
    client_status TEXT NOT NULL DEFAULT 'existing',
    acquisition_campaign_code TEXT,
    acquisition_source TEXT,
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
CREATE INDEX IF NOT EXISTS ix_clients_username_nocase ON clients(username COLLATE NOCASE);

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
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts >= 1),
    verification_required INTEGER NOT NULL DEFAULT 0,
    welcome_kicker TEXT NOT NULL DEFAULT 'Короткий опрос клуба',
    welcome_text TEXT NOT NULL DEFAULT 'Ответь на несколько вопросов — это займёт пару минут и поможет нам делать события интереснее.',
    start_button_text TEXT NOT NULL DEFAULT 'Начать',
    identity_text TEXT NOT NULL DEFAULT 'Укажи номер телефона или Telegram username, чтобы мы сохранили попытку и смогли продолжить её после закрытия страницы.',
    victory_title TEXT NOT NULL DEFAULT 'Поздравляем!',
    victory_text TEXT NOT NULL DEFAULT 'Отличная игра! Твой результат — {score} из {max_score}.',
    failure_title TEXT NOT NULL DEFAULT 'Не расстраивайся',
    failure_text TEXT NOT NULL DEFAULT 'Попробуй ещё раз — использовано попыток: {attempts_used} из {max_attempts}.',
    completion_title TEXT NOT NULL DEFAULT 'Спасибо!',
    completion_text TEXT NOT NULL DEFAULT 'Твои ответы сохранены.',
    reward_validity_mode TEXT NOT NULL DEFAULT 'end_of_day',
    reward_validity_value INTEGER NOT NULL DEFAULT 0,
    reward_valid_from TEXT,
    reward_valid_until TEXT,
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
    client_id INTEGER REFERENCES clients(id),
    attempt_number INTEGER NOT NULL DEFAULT 1,
    identity_method TEXT NOT NULL DEFAULT 'legacy',
    is_new_client INTEGER NOT NULL DEFAULT 0,
    quiz_referrer_id TEXT,
    source TEXT,
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
    last_activity_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
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

CREATE TABLE IF NOT EXISTS client_quiz_campaigns (
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    campaign_code TEXT NOT NULL,
    first_source TEXT,
    first_referrer_id TEXT,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(client_id, campaign_code)
);

CREATE TABLE IF NOT EXISTS quiz_participation_summary (
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    campaign_code TEXT NOT NULL,
    attempts_used INTEGER NOT NULL DEFAULT 0,
    successful INTEGER NOT NULL DEFAULT 0,
    reward_issued INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(client_id, campaign_code)
);

CREATE TABLE IF NOT EXISTS quiz_reward_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    campaign_code TEXT NOT NULL,
    submission_id INTEGER,
    preference_code TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK(amount > 0),
    status TEXT NOT NULL DEFAULT 'issued' CHECK(status IN ('issued', 'used', 'expired', 'cancelled')),
    valid_from TEXT,
    valid_until TEXT,
    used_at TEXT,
    used_by_admin_id INTEGER REFERENCES admins(id),
    cancelled_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_quiz_reward_codes_client ON quiz_reward_codes(client_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_quiz_reward_codes_status ON quiz_reward_codes(status, valid_until);

CREATE TABLE IF NOT EXISTS quiz_reward_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reward_id INTEGER REFERENCES quiz_reward_codes(id) ON DELETE SET NULL,
    code TEXT NOT NULL,
    client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    campaign_code TEXT NOT NULL,
    action TEXT NOT NULL,
    admin_name TEXT NOT NULL DEFAULT 'system',
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_quiz_reward_events_created ON quiz_reward_events(created_at DESC);

CREATE TABLE IF NOT EXISTS quiz_email_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_normalized TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    campaign_code TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts_left INTEGER NOT NULL DEFAULT 5,
    used_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_quiz_email_codes_lookup ON quiz_email_codes(email_normalized, campaign_code, created_at DESC);

CREATE TABLE IF NOT EXISTS maintenance_log (
    task TEXT PRIMARY KEY,
    last_run_at TEXT NOT NULL
);
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
        _ensure_column(conn, "clients", "telegram_user_id TEXT")
        _ensure_column(conn, "clients", "email TEXT")
        _ensure_column(conn, "clients", "email_normalized TEXT")
        _ensure_column(conn, "clients", "client_status TEXT NOT NULL DEFAULT 'existing'")
        _ensure_column(conn, "clients", "acquisition_campaign_code TEXT")
        _ensure_column(conn, "clients", "acquisition_source TEXT")
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
        _ensure_column(conn, "quiz_campaigns", "max_attempts INTEGER NOT NULL DEFAULT 3")
        _ensure_column(conn, "quiz_campaigns", "verification_required INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "quiz_campaigns", "welcome_kicker TEXT NOT NULL DEFAULT 'Короткий опрос клуба'")
        _ensure_column(conn, "quiz_campaigns", "welcome_text TEXT NOT NULL DEFAULT 'Ответь на несколько вопросов — это займёт пару минут и поможет нам делать события интереснее.'")
        _ensure_column(conn, "quiz_campaigns", "start_button_text TEXT NOT NULL DEFAULT 'Начать'")
        _ensure_column(conn, "quiz_campaigns", "identity_text TEXT NOT NULL DEFAULT 'Укажи номер телефона или Telegram username, чтобы мы сохранили попытку и смогли продолжить её после закрытия страницы.'")
        _ensure_column(conn, "quiz_campaigns", "victory_title TEXT NOT NULL DEFAULT 'Поздравляем!'")
        _ensure_column(conn, "quiz_campaigns", "victory_text TEXT NOT NULL DEFAULT 'Отличная игра! Твой результат — {score} из {max_score}.'")
        _ensure_column(conn, "quiz_campaigns", "failure_title TEXT NOT NULL DEFAULT 'Не расстраивайся'")
        _ensure_column(conn, "quiz_campaigns", "failure_text TEXT NOT NULL DEFAULT 'Попробуй ещё раз — использовано попыток: {attempts_used} из {max_attempts}.'")
        _ensure_column(conn, "quiz_campaigns", "completion_title TEXT NOT NULL DEFAULT 'Спасибо!'")
        _ensure_column(conn, "quiz_campaigns", "completion_text TEXT NOT NULL DEFAULT 'Твои ответы сохранены.'")
        _ensure_column(conn, "quiz_campaigns", "reward_validity_mode TEXT NOT NULL DEFAULT 'end_of_day'")
        _ensure_column(conn, "quiz_campaigns", "reward_validity_value INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "quiz_campaigns", "reward_valid_from TEXT")
        _ensure_column(conn, "quiz_campaigns", "reward_valid_until TEXT")
        _ensure_column(conn, "quiz_questions", "time_limit_seconds INTEGER")
        _ensure_column(conn, "quiz_submissions", "attempt_id INTEGER")
        _ensure_column(conn, "quiz_attempts", "attempt_deadline_at TEXT")
        _ensure_column(conn, "quiz_attempts", "client_id INTEGER REFERENCES clients(id)")
        _ensure_column(conn, "quiz_attempts", "attempt_number INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "quiz_attempts", "identity_method TEXT NOT NULL DEFAULT 'legacy'")
        _ensure_column(conn, "quiz_attempts", "is_new_client INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "quiz_attempts", "quiz_referrer_id TEXT")
        _ensure_column(conn, "quiz_attempts", "source TEXT")
        _ensure_column(conn, "quiz_attempts", "last_activity_at TEXT")
        _ensure_column(conn, "quiz_attempts", "finished_at TEXT")
        conn.execute("UPDATE quiz_attempts SET last_activity_at = COALESCE(last_activity_at, created_at)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_clients_telegram_user_id ON clients(telegram_user_id) WHERE telegram_user_id IS NOT NULL AND telegram_user_id <> ''")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_clients_email_normalized ON clients(email_normalized) WHERE email_normalized IS NOT NULL AND email_normalized <> ''")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_clients_username_nocase ON clients(username COLLATE NOCASE)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_quiz_attempts_client_campaign ON quiz_attempts(client_id, campaign_code, status, created_at DESC)")
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
        conn.execute(
            """
            INSERT OR IGNORE INTO quiz_participation_summary(
                client_id, campaign_code, attempts_used, successful, reward_issued,
                last_attempt_at, completed_at
            )
            SELECT client_id, campaign_code, COUNT(*), MAX(passed),
                   MAX(CASE WHEN bonus_granted=1 OR bonus_pending=1 THEN 1 ELSE 0 END),
                   MAX(created_at), MAX(CASE WHEN passed=1 THEN created_at END)
            FROM quiz_submissions
            GROUP BY client_id, campaign_code
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO client_quiz_campaigns(client_id, campaign_code, first_source, first_referrer_id, first_seen_at, last_seen_at)
            SELECT client_id, campaign_code, MIN(source), MIN(quiz_referrer_id), MIN(created_at), MAX(created_at)
            FROM quiz_submissions
            GROUP BY client_id, campaign_code
            """
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
