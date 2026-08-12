from pathlib import Path

import pytest

from app.db import init_db, transaction
from app.hijack_rating_baseline import ensure_baseline_schema
from app.hijack_rating_transfer import transfer_hijack_rating_owner
from app.services.hijack_rating import ensure_hijack_rating_schema


ROOT = Path(__file__).resolve().parents[1]


def _client(conn, name: str, *, status: str = "existing") -> int:
    return int(
        conn.execute(
            "INSERT INTO clients(first_name,client_status,source) VALUES (?,?, 'test')",
            (name, status),
        ).lastrowid
    )


def _member_account(conn, client_id: int, email: str) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO member_accounts(client_id,email,email_normalized,password_hash)
            VALUES (?,?,?,?)
            """,
            (client_id, email, email.lower(), "test-hash"),
        ).lastrowid
    )


def _import(conn, name: str = "Tournament") -> int:
    return int(
        conn.execute(
            """
            INSERT INTO hi_jack_rating_imports(
                tournament_name,tournament_date,source_filename
            ) VALUES (?, '2026-08-01', 'rating.xlsx')
            """,
            (name,),
        ).lastrowid
    )


def test_hijack_rating_owner_transfer_moves_baseline_and_tournaments(tmp_path) -> None:
    db_path = tmp_path / "rating-transfer.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_hijack_rating_schema(conn)
        ensure_baseline_schema(conn)
        source_id = _client(conn, "Old", status="deleted")
        target_id = _client(conn, "New")
        _member_account(conn, target_id, "new@example.com")
        import_id = _import(conn)
        conn.execute(
            """
            INSERT INTO hi_jack_rating_entries(
                import_id,client_id,phone_local,phone_raw,rating_points,kills,source_row
            ) VALUES (?,?, '9991112233', '9991112233', 42, 3, 2)
            """,
            (import_id, source_id),
        )
        conn.execute(
            """
            INSERT INTO hi_jack_rating_baseline_entries(
                client_id,phone_local,phone_raw,rating_points,kills,source_row
            ) VALUES (?, '9991112233', '9991112233', 120, 7, 2)
            """,
            (source_id,),
        )

        result = transfer_hijack_rating_owner(
            conn,
            source_client_id=source_id,
            target_client_id=target_id,
        )

        assert result == {
            "tournament_rows": 1,
            "baseline_rows": 1,
            "total_rows": 2,
        }
        assert int(
            conn.execute(
                "SELECT client_id FROM hi_jack_rating_entries WHERE import_id=?",
                (import_id,),
            ).fetchone()[0]
        ) == target_id
        assert int(
            conn.execute(
                "SELECT client_id FROM hi_jack_rating_baseline_entries"
            ).fetchone()[0]
        ) == target_id


def test_hijack_rating_owner_transfer_blocks_overlapping_tournament(tmp_path) -> None:
    db_path = tmp_path / "rating-transfer-conflict.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_hijack_rating_schema(conn)
        ensure_baseline_schema(conn)
        source_id = _client(conn, "Old", status="deleted")
        target_id = _client(conn, "New")
        _member_account(conn, target_id, "new@example.com")
        import_id = _import(conn)
        for client_id, source_row in ((source_id, 2), (target_id, 3)):
            conn.execute(
                """
                INSERT INTO hi_jack_rating_entries(
                    import_id,client_id,phone_local,phone_raw,rating_points,kills,source_row
                ) VALUES (?,?,NULL,'',10,1,?)
                """,
                (import_id, client_id, source_row),
            )

        with pytest.raises(ValueError, match="тех же турнирах"):
            transfer_hijack_rating_owner(
                conn,
                source_client_id=source_id,
                target_client_id=target_id,
            )

        owners = {
            int(row[0])
            for row in conn.execute(
                "SELECT client_id FROM hi_jack_rating_entries WHERE import_id=?",
                (import_id,),
            ).fetchall()
        }
        assert owners == {source_id, target_id}


def test_prelaunch_ui_hotfix_keeps_campaign_save_in_place() -> None:
    script = (ROOT / "app/static/js/prelaunch-admin.js").read_text(encoding="utf-8")
    assert "form.campaign-edit" in script
    assert "event.preventDefault()" in script
    assert "new FormData(form)" in script
    assert "prelaunch-admin-toast" in script
    assert "referral" in script
    assert "active_from" in script


def test_prelaunch_ui_hotfix_uses_exact_rating_client_ids() -> None:
    script = (ROOT / "app/static/js/prelaunch-member.js").read_text(encoding="utf-8")
    assert "/api/account/rating-profile-links" in script
    assert "client_ids" in script
    assert "`/players/${clientId}`" in script
    assert "MutationObserver" in script


def test_prelaunch_ui_hotfix_compacts_home_quiz_card() -> None:
    css = (ROOT / "app/static/css/prelaunch-ui-hotfix.css").read_text(encoding="utf-8")
    assert ".quiz-feature-card{grid-template-columns:56px" in css
    assert ".quiz-feature-prize" in css
    assert "display:none!important" in css
    assert ".quiz-feature-actions{display:flex!important" in css


def test_hijack_rating_admin_has_safe_transfer_form() -> None:
    template = (ROOT / "app/templates/hijack_rating_admin.html").read_text(encoding="utf-8")
    assert "/api/master/hijack-rating/transfer-owner" in template
    assert 'name="source_client_id"' in template
    assert 'name="target_client_id"' in template
    assert "ПЕРЕНЕСТИ РЕЙТИНГ" in template
