from pathlib import Path

from app.admin_telegram_unlink import unlink_telegram_identity
from app.db import init_db, transaction


ROOT = Path(__file__).resolve().parents[1]


def test_master_unlink_releases_telegram_identity_for_another_client(tmp_path: Path) -> None:
    db_path = tmp_path / "telegram-unlink.sqlite3"
    init_db(db_path)

    with transaction(db_path) as conn:
        first = int(
            conn.execute(
                """
                INSERT INTO clients(
                    phone_local,telegram_id,telegram_user_id,source
                ) VALUES ('9991112233','legacy-telegram','telegram-user-42','test')
                """
            ).lastrowid
        )
        account_id = int(
            conn.execute(
                """
                INSERT INTO member_accounts(
                    client_id,email,email_normalized,password_hash,email_verified_at
                ) VALUES (?,?,?,?,CURRENT_TIMESTAMP)
                """,
                (first, "one@example.com", "one@example.com", "hash"),
            ).lastrowid
        )
        second = int(
            conn.execute(
                "INSERT INTO clients(phone_local,source) VALUES ('9992223344','test')"
            ).lastrowid
        )

        returned_account_id, changed = unlink_telegram_identity(conn, first)
        assert changed is True
        assert returned_account_id == account_id

        row = conn.execute(
            "SELECT telegram_id,telegram_user_id FROM clients WHERE id=?",
            (first,),
        ).fetchone()
        assert row["telegram_id"] is None
        assert row["telegram_user_id"] is None

        conn.execute(
            "UPDATE clients SET telegram_user_id='telegram-user-42' WHERE id=?",
            (second,),
        )
        rebound = conn.execute(
            "SELECT id FROM clients WHERE telegram_user_id='telegram-user-42'"
        ).fetchone()
        assert int(rebound["id"]) == second


def test_admin_nav_is_dropdown_and_registry_exposes_unlink_control() -> None:
    css = (ROOT / "app/static/css/admin-usability-hotfix.css").read_text(encoding="utf-8")
    script = (ROOT / "app/static/js/master-security-journal.js").read_text(encoding="utf-8")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")

    assert ".admin-persistent-nav[hidden]" in css
    assert "display: none !important" in css
    assert ".admin-persistent-nav:not([hidden])" in css
    assert ".admin-app-body .topbar > .admin-menu-toggle" in css
    assert "width: min(1240px, calc(100vw - 48px))" in css
    assert "font-size: 15px" in css

    assert "/unlink-telegram" in script
    assert "Отвязать Telegram" in script
    assert "telegram_user_id" in script
    assert "install_admin_telegram_unlink" in main
