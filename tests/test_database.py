from pathlib import Path
import sqlite3

import pytest

from app.db import connect, init_db, transaction
from app.services.clients import ensure_preferences, upsert_client
from app.services.preferences import change_counter, set_percent


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.sqlite3"
    init_db(path)
    return path


def test_upsert_uses_phone_and_preserves_existing_values(db_path):
    with transaction(db_path) as conn:
        client_id, action = upsert_client(conn, {"app_user_id": "42", "first_name": "Иван", "phone_raw": "+7 999 123-45-67"})
        assert action == "inserted"
        same_id, action = upsert_client(conn, {"app_user_id": "42", "nickname": "Vanya", "phone_raw": "89991234567"})
        assert same_id == client_id
        assert action == "updated"
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        assert row["first_name"] == "Иван"
        assert row["nickname"] == "Vanya"
        assert row["phone_local"] == "9991234567"


def test_counter_and_discount_are_logged_atomically(db_path):
    with transaction(db_path) as conn:
        client_id, _ = upsert_client(conn, {"app_user_id": "1", "phone_raw": "9991234567"})
        ensure_preferences(conn, client_id)
        assert change_counter(conn, client_id=client_id, code="free_reentry", delta=2, reason="bonus", comment="", admin_name="Alex") == 2
        assert change_counter(conn, client_id=client_id, code="free_reentry", delta=-1, reason="used", comment="", admin_name="Alex") == 1
        assert set_percent(conn, client_id=client_id, code="bar_hookah_discount_percent", percent=15, reason="VIP", comment="", admin_name="Alex") == 15
    with connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM preference_log").fetchone()[0] == 3


def test_counter_cannot_go_below_zero(db_path):
    with pytest.raises(ValueError, match="insufficient_balance"):
        with transaction(db_path) as conn:
            client_id, _ = upsert_client(conn, {"app_user_id": "2"})
            ensure_preferences(conn, client_id)
            change_counter(conn, client_id=client_id, code="free_entry", delta=-1, reason="used", comment="", admin_name="Alex")
    with connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 0


def test_existing_preference_table_is_migrated_without_data_loss(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE preference_types (id INTEGER PRIMARY KEY, code TEXT UNIQUE, title TEXT, kind TEXT, is_active INTEGER)"
        )
        conn.execute(
            "INSERT INTO preference_types(id, code, title, kind, is_active) VALUES (1, 'legacy_bonus', 'Старый бонус', 'counter', 1)"
        )
    init_db(path)
    with connect(path) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(preference_types)")}
        assert {"position", "created_by_admin_id", "created_at", "updated_at"}.issubset(columns)
        assert conn.execute("SELECT title FROM preference_types WHERE code='legacy_bonus'").fetchone()[0] == "Старый бонус"


def test_quiz_campaign_schedule_columns_are_migrated(tmp_path):
    path = tmp_path / "legacy-campaign.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE quiz_campaigns (id INTEGER PRIMARY KEY, code TEXT UNIQUE, title TEXT, is_active INTEGER)"
        )
        conn.execute("INSERT INTO quiz_campaigns VALUES (1, 'legacy', 'Старый квиз', 1)")
    init_db(path)
    with connect(path) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(quiz_campaigns)")}
        assert {"active_from", "active_until", "archived_at", "deleted_at"}.issubset(columns)
        assert conn.execute("SELECT title FROM quiz_campaigns WHERE code='legacy'").fetchone()[0] == "Старый квиз"


def test_414_and_member_tables_are_added_without_changing_legacy_campaign(tmp_path):
    path = tmp_path / "legacy-before-member-portal.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE quiz_campaigns (
                id INTEGER PRIMARY KEY, code TEXT UNIQUE, title TEXT,
                is_active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            "INSERT INTO quiz_campaigns(id, code, title) VALUES (7, 'legacy', 'Старый квиз')"
        )
    init_db(path)
    with connect(path) as conn:
        campaign = conn.execute(
            """
            SELECT id, title, campaign_type, jackcoin_per_correct,
                   jackcoin_completion_bonus, jackcoin_perfect_bonus
            FROM quiz_campaigns WHERE id=7
            """
        ).fetchone()
        assert tuple(campaign) == (7, "Старый квиз", "classic", 5, 10, 20)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "member_accounts",
            "member_sessions",
            "member_email_codes",
            "legal_documents",
            "member_consents",
            "jackcoin_ledger",
            "daily_414_progress",
            "daily_414_final_tables",
            "daily_414_finalists",
            "daily_414_final_answers",
            "club_rating_snapshots",
            "club_rating_entries",
        }.issubset(tables)
        assert conn.execute(
            "SELECT COUNT(*) FROM legal_documents WHERE is_active=1"
        ).fetchone()[0] == 2


def test_text_answer_column_is_migrated_without_rebuilding_questions(tmp_path):
    path = tmp_path / "legacy-questions.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE quiz_questions (
                id INTEGER PRIMARY KEY, campaign_code TEXT, code TEXT, type TEXT, title TEXT,
                position INTEGER NOT NULL DEFAULT 100
            )
            """
        )
        conn.execute(
            "INSERT INTO quiz_questions VALUES (7, 'default', 'legacy_text', 'text', 'Старый вопрос', 100)"
        )
    init_db(path)
    with connect(path) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(quiz_questions)")}
        assert "accepted_text_answers_json" in columns
        assert "game_round" in columns
        assert conn.execute(
            "SELECT game_round FROM quiz_questions WHERE id=7"
        ).fetchone()[0] == "main"
        row = conn.execute("SELECT id, title, accepted_text_answers_json FROM quiz_questions WHERE id=7").fetchone()
        assert tuple(row) == (7, "Старый вопрос", "[]")


def test_v18_quiz_tables_are_migrated_without_data_loss(tmp_path):
    path = tmp_path / "legacy-v18.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT, app_user_id TEXT, telegram_id TEXT,
                referrer_app_user_id TEXT, nickname TEXT, username TEXT, first_name TEXT,
                phone_raw TEXT, phone_full TEXT, phone_local TEXT, source TEXT NOT NULL DEFAULT 'manual',
                comment TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE quiz_campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1, bonus_preference_code TEXT, bonus_amount INTEGER NOT NULL DEFAULT 0,
                pass_score INTEGER NOT NULL DEFAULT 0, question_time_limit_seconds INTEGER NOT NULL DEFAULT 20,
                quiz_time_limit_seconds INTEGER NOT NULL DEFAULT 120, active_from TEXT, active_until TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE quiz_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_code TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                questions_snapshot_json TEXT NOT NULL, answers_json TEXT NOT NULL DEFAULT '{}', current_index INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'in_progress', question_started_at TEXT, question_deadline_at TEXT,
                attempt_deadline_at TEXT, completed_questions_at TEXT, ip_hash TEXT NOT NULL, user_agent TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE quiz_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id INTEGER, campaign_code TEXT NOT NULL,
                client_id INTEGER NOT NULL, phone_raw TEXT NOT NULL, phone_local TEXT NOT NULL,
                answers_json TEXT NOT NULL, questions_snapshot_json TEXT, score REAL, max_score INTEGER NOT NULL DEFAULT 0,
                correct_count INTEGER NOT NULL DEFAULT 0, max_correct_count INTEGER NOT NULL DEFAULT 0,
                passed INTEGER NOT NULL DEFAULT 0, bonus_granted INTEGER NOT NULL DEFAULT 0,
                bonus_pending INTEGER NOT NULL DEFAULT 0, bonus_type TEXT, is_duplicate INTEGER NOT NULL DEFAULT 0,
                is_new_client INTEGER NOT NULL DEFAULT 0, quiz_referrer_id TEXT, source TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, user_agent TEXT, ip_hash TEXT NOT NULL
            );
            INSERT INTO clients(first_name, phone_local, source) VALUES ('Старый клиент', '9991234567', 'import');
            """
        )
    init_db(path)
    with connect(path) as conn:
        client = conn.execute("SELECT * FROM clients").fetchone()
        assert client["first_name"] == "Старый клиент"
        assert client["client_status"] == "existing"
        campaign = conn.execute("SELECT * FROM quiz_campaigns WHERE code='default'").fetchone()
        assert campaign["max_attempts"] == 3
        assert campaign["welcome_kicker"]
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(quiz_attempts)")}
        assert {"client_id", "attempt_number", "last_activity_at"}.issubset(columns)
        assert conn.execute("SELECT COUNT(*) FROM quiz_reward_codes").fetchone()[0] == 0
        device_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(quiz_device_tokens)")
        }
        assert {
            "token_hash", "client_id", "expires_at", "last_used_at", "revoked_at"
        }.issubset(device_columns)
