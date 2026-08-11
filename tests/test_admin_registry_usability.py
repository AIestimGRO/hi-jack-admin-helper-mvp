from pathlib import Path

from app.admin_account_lifecycle import linked_client_records
from app.db import init_db, transaction


def test_linked_client_records_ignores_defaults_but_blocks_history(tmp_path: Path) -> None:
    db_path = tmp_path / "registry-delete.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        client_id = int(
            conn.execute(
                "INSERT INTO clients(phone_local,source) VALUES ('9991112233','test')"
            ).lastrowid
        )
        preference_type = conn.execute(
            "SELECT id FROM preference_types ORDER BY id LIMIT 1"
        ).fetchone()
        if preference_type:
            conn.execute(
                "INSERT INTO client_preferences(client_id,preference_type_id) VALUES (?,?)",
                (client_id, int(preference_type["id"])),
            )
        assert linked_client_records(conn, client_id) == []

        conn.execute(
            """
            INSERT INTO preference_log(
                client_id,preference_type_id,operation_type,reason,comment,admin_name
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                client_id,
                int(preference_type["id"]),
                "test",
                "history",
                "",
                "test",
            ),
        )
        linked = linked_client_records(conn, client_id)
        assert any(table == "preference_log" and count == 1 for table, _column, count in linked)


def test_admin_registry_contains_lifecycle_and_readability_controls() -> None:
    root = Path(__file__).resolve().parents[1]
    template = (root / "app/templates/master_member_accounts.html").read_text(encoding="utf-8")
    base = (root / "app/templates/base.html").read_text(encoding="utf-8")
    css = (root / "app/static/css/admin-usability-hotfix.css").read_text(encoding="utf-8")
    main = (root / "app/main.py").read_text(encoding="utf-8")

    assert "/delete-account" in template
    assert "УДАЛИТЬ ЛК" in template
    assert "/delete-empty" in template
    assert "УДАЛИТЬ ЗАПИСЬ" in template
    assert "admin-usability-hotfix.css" in base
    assert "--admin-rail-width: 300px" in css
    assert "font-size: 15px" in css
    assert "account-registry-danger-card" in css
    assert "install_admin_account_lifecycle" in main
