from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.account_security import (
    _consume_code,
    _store_code,
    anonymize_account,
    apply_email_change,
    apply_phone_change,
    ensure_account_security_schema,
)
from app.db import init_db, transaction
from app.hijack_rating_relink import relink_hijack_history
from app.services.hijack_rating import import_hijack_rating


def _member(conn, *, phone: str = "9991112233", email: str = "old@example.com") -> tuple[int, int]:
    client = conn.execute(
        """
        INSERT INTO clients(
            nickname,username,first_name,phone_raw,phone_full,phone_local,
            email,email_normalized,telegram_user_id,source
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "Player",
            "player_name",
            "Ivan",
            f"+7{phone}",
            f"+7{phone}",
            phone,
            email,
            email,
            "123456",
            "test",
        ),
    )
    client_id = int(client.lastrowid)
    account = conn.execute(
        """
        INSERT INTO member_accounts(
            client_id,email,email_normalized,password_hash,email_verified_at
        ) VALUES (?,?,?,?,CURRENT_TIMESTAMP)
        """,
        (client_id, email, email, "test-hash"),
    )
    return int(account.lastrowid), client_id


def test_email_change_updates_account_and_client(tmp_path: Path) -> None:
    db_path = tmp_path / "security-email.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_account_security_schema(conn)
        account_id, client_id = _member(conn)
        apply_email_change(
            conn,
            account_id=account_id,
            client_id=client_id,
            new_email="NEW@Example.com",
        )
        account = conn.execute(
            "SELECT email,email_normalized,is_active FROM member_accounts WHERE id=?",
            (account_id,),
        ).fetchone()
        client = conn.execute(
            "SELECT email,email_normalized FROM clients WHERE id=?",
            (client_id,),
        ).fetchone()
        assert account["email"] == "new@example.com"
        assert account["email_normalized"] == "new@example.com"
        assert account["is_active"] == 1
        assert client["email"] == "new@example.com"
        assert client["email_normalized"] == "new@example.com"


def test_phone_change_keeps_old_phone_as_rating_alias(tmp_path: Path) -> None:
    db_path = tmp_path / "security-phone.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_account_security_schema(conn)
        account_id, client_id = _member(conn)
        apply_phone_change(
            conn,
            account_id=account_id,
            client_id=client_id,
            new_phone="+7 999 222-33-44",
        )
        client = conn.execute(
            "SELECT phone_local FROM clients WHERE id=?", (client_id,)
        ).fetchone()
        alias = conn.execute(
            "SELECT phone_local FROM client_phone_aliases WHERE client_id=?",
            (client_id,),
        ).fetchone()
        assert client["phone_local"] == "9992223344"
        assert alias["phone_local"] == "9991112233"

        result = import_hijack_rating(
            conn,
            tournament_name="Old phone import",
            tournament_date=date(2026, 8, 11),
            source_filename="rating.xlsx",
            rows=[
                {
                    "source_row": 2,
                    "phone_raw": "+7 999 111-22-33",
                    "phone_local": "9991112233",
                    "rating_points": 50,
                    "kills": 2,
                }
            ],
            admin_id=None,
        )
        assert result["unmatched_rows"] == 1
        assert relink_hijack_history(conn, client_id=client_id) == 1
        linked = conn.execute(
            "SELECT client_id FROM hi_jack_rating_entries WHERE import_id=?",
            (result["import_id"],),
        ).fetchone()
        assert linked["client_id"] == client_id


def test_security_code_is_single_use_and_limits_bad_attempts(tmp_path: Path) -> None:
    db_path = tmp_path / "security-code.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_account_security_schema(conn)
        account_id, _client_id = _member(conn)
        _store_code(
            conn,
            secret_key="s" * 40,
            account_id=account_id,
            kind="change_email",
            target_value="next@example.com",
            code="123456",
            expires_minutes=10,
        )
        with pytest.raises(ValueError, match="Неверный код"):
            _consume_code(
                conn,
                secret_key="s" * 40,
                account_id=account_id,
                kind="change_email",
                code="000000",
            )
        row = conn.execute(
            "SELECT attempts_left FROM member_account_security_codes WHERE account_id=?",
            (account_id,),
        ).fetchone()
        assert row["attempts_left"] == 4
        assert (
            _consume_code(
                conn,
                secret_key="s" * 40,
                account_id=account_id,
                kind="change_email",
                code="123456",
            )
            == "next@example.com"
        )
        with pytest.raises(ValueError, match="Сначала запросите"):
            _consume_code(
                conn,
                secret_key="s" * 40,
                account_id=account_id,
                kind="change_email",
                code="123456",
            )


def test_delete_anonymizes_pii_but_preserves_rating_result(tmp_path: Path) -> None:
    db_path = tmp_path / "security-delete.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_account_security_schema(conn)
        account_id, client_id = _member(conn)
        apply_phone_change(
            conn,
            account_id=account_id,
            client_id=client_id,
            new_phone="9992223344",
        )
        result = import_hijack_rating(
            conn,
            tournament_name="Keep history",
            tournament_date=date(2026, 8, 10),
            source_filename="rating.xlsx",
            rows=[
                {
                    "source_row": 2,
                    "phone_raw": "+7 999 222-33-44",
                    "phone_local": "9992223344",
                    "rating_points": 80,
                    "kills": 3,
                }
            ],
            admin_id=None,
        )
        assert result["matched_rows"] == 1

        anonymize_account(conn, account_id=account_id, client_id=client_id)

        account = conn.execute(
            "SELECT email_normalized,password_hash,is_active FROM member_accounts WHERE id=?",
            (account_id,),
        ).fetchone()
        client = conn.execute(
            """
            SELECT nickname,username,first_name,phone_local,email,telegram_user_id,client_status
            FROM clients WHERE id=?
            """,
            (client_id,),
        ).fetchone()
        rating = conn.execute(
            """
            SELECT client_id,phone_local,phone_raw,rating_points,kills
            FROM hi_jack_rating_entries WHERE import_id=?
            """,
            (result["import_id"],),
        ).fetchone()
        aliases = conn.execute(
            "SELECT COUNT(*) FROM client_phone_aliases WHERE client_id=?",
            (client_id,),
        ).fetchone()[0]

        assert account["is_active"] == 0
        assert account["password_hash"] == "deleted"
        assert account["email_normalized"].endswith("@deleted.invalid")
        assert client["nickname"] == "Удалённый игрок"
        assert client["username"] is None
        assert client["first_name"] is None
        assert client["phone_local"] is None
        assert client["email"] is None
        assert client["telegram_user_id"] is None
        assert client["client_status"] == "deleted"
        assert rating["client_id"] == client_id
        assert rating["phone_local"] is None
        assert rating["phone_raw"] == ""
        assert rating["rating_points"] == 80
        assert rating["kills"] == 3
        assert aliases == 0


def test_profile_loads_account_security_assets() -> None:
    root = Path(__file__).resolve().parents[1]
    loader = (root / "app/static/js/member-profile-refresh.js").read_text(encoding="utf-8")
    script = (root / "app/static/js/account-security.js").read_text(encoding="utf-8")
    assert "account-security.css?v=1" in loader
    assert "account-security.js?v=1" in loader
    assert "Настройки аккаунта" in script
    assert "/account/security/email/request" in script
    assert "/account/security/phone/request" in script
    assert "/account/security/delete/confirm" in script
