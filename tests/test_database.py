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
