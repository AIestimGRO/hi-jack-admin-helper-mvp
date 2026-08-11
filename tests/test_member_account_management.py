from pathlib import Path

import pytest

from app.db import init_db, transaction
from app.member_account_management import (
    assert_current_phone_available,
    ensure_member_account_management_schema,
    normalize_birth_date,
)
from app.services.client_phone_aliases import phones_for_client, resolve_client_id_by_phone


def test_new_member_accounts_require_birth_date_but_existing_rows_are_not_backfilled(tmp_path: Path) -> None:
    db_path = tmp_path / "birthday-schema.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        client_old = conn.execute(
            "INSERT INTO clients(phone_local,source) VALUES ('9991112233','test')"
        ).lastrowid
        account_old = conn.execute(
            """
            INSERT INTO member_accounts(client_id,email,email_normalized,password_hash,email_verified_at)
            VALUES (?,?,?,?,CURRENT_TIMESTAMP)
            """,
            (client_old, "old@example.com", "old@example.com", "hash"),
        ).lastrowid
        ensure_member_account_management_schema(conn)
        old_row = conn.execute(
            "SELECT birth_date_required FROM member_accounts WHERE id=?", (account_old,)
        ).fetchone()
        assert old_row["birth_date_required"] == 0

        client_new = conn.execute(
            "INSERT INTO clients(phone_local,source) VALUES ('9992223344','test')"
        ).lastrowid
        account_new = conn.execute(
            """
            INSERT INTO member_accounts(client_id,email,email_normalized,password_hash,email_verified_at)
            VALUES (?,?,?,?,CURRENT_TIMESTAMP)
            """,
            (client_new, "new@example.com", "new@example.com", "hash"),
        ).lastrowid
        new_row = conn.execute(
            "SELECT birth_date_required FROM member_accounts WHERE id=?", (account_new,)
        ).fetchone()
        assert new_row["birth_date_required"] == 1


def test_current_phone_is_unique_and_old_alias_is_not_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "phone-identity.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_member_account_management_schema(conn)
        first = conn.execute(
            "INSERT INTO clients(phone_local,source) VALUES ('9991112233','test')"
        ).lastrowid
        conn.execute(
            """
            INSERT INTO member_accounts(client_id,email,email_normalized,password_hash,email_verified_at)
            VALUES (?,?,?,?,CURRENT_TIMESTAMP)
            """,
            (first, "one@example.com", "one@example.com", "hash"),
        )
        second = conn.execute(
            "INSERT INTO clients(phone_local,source) VALUES ('9992223344','test')"
        ).lastrowid
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS client_phone_aliases(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                phone_local TEXT NOT NULL UNIQUE,
                reason TEXT NOT NULL DEFAULT 'phone_change',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO client_phone_aliases(client_id,phone_local) VALUES (?,?)",
            (first, "9993334455"),
        )

        with pytest.raises(ValueError, match="уже привязан"):
            assert_current_phone_available(conn, phone_local="9991112233", client_id=second)
        assert phones_for_client(conn, int(first)) == ["9991112233"]
        assert resolve_client_id_by_phone(conn, "9993334455") is None


def test_birth_date_validation() -> None:
    assert normalize_birth_date("1992-10-01") == "1992-10-01"
    assert normalize_birth_date("", required=False) is None
    with pytest.raises(ValueError):
        normalize_birth_date("1899-12-31")
    with pytest.raises(ValueError):
        normalize_birth_date("2999-01-01")


def test_registration_profile_and_master_ui_contain_new_controls() -> None:
    root = Path(__file__).resolve().parents[1]
    register = (root / "app/templates/member_register.html").read_text(encoding="utf-8")
    profile = (root / "app/static/js/account-security.js").read_text(encoding="utf-8")
    master = (root / "app/templates/master_member_accounts.html").read_text(encoding="utf-8")
    base = (root / "app/templates/base.html").read_text(encoding="utf-8")
    main = (root / "app/main.py").read_text(encoding="utf-8")

    assert 'name="birth_date"' in register
    assert "member-registration-extra.js" in register
    assert "/account/security/password/change" in profile
    assert "Дата рождения" in profile
    assert "Регистрационные и учётные данные" in master
    assert "/master/member-accounts" in base
    assert "install_member_account_management" in main
