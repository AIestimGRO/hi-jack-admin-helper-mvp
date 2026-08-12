from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db import init_db, transaction
from app.hijack_rating_baseline import ensure_baseline_schema
from app.hijack_rating_transfer import transfer_hijack_rating_owner
from app.prelaunch_data_integrity import (
    calendar_jackside_rating_payload,
    dedupe_title_collection,
)
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
            INSERT INTO member_accounts(
                client_id,email,email_normalized,password_hash,email_verified_at
            ) VALUES (?,?,?,?,CURRENT_TIMESTAMP)
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


def _daily_campaign(conn, code: str) -> None:
    conn.execute(
        """
        INSERT INTO quiz_campaigns(code,title,campaign_type)
        VALUES (?,?,'daily_414')
        """,
        (code, code),
    )


def _submission(
    conn,
    *,
    client_id: int,
    code: str,
    created_at: str,
    correct: int = 8,
    questions: int = 10,
) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO quiz_submissions(
                campaign_code,campaign_version,client_id,phone_raw,phone_local,
                answers_json,max_score,correct_count,max_correct_count,passed,
                main_round_completed,created_at,ip_hash
            ) VALUES (?,1,?,'','9990000000','{}',?,?,?,?,1,?,'test')
            """,
            (code, client_id, questions, correct, questions, int(correct >= questions), created_at),
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


def test_jackside_month_rating_uses_calendar_month_and_keeps_old_source_rows(tmp_path) -> None:
    db_path = tmp_path / "calendar-month.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        player = _client(conn, "August")
        old_player = _client(conn, "July")
        for index, day in enumerate(("2026-08-01", "2026-08-05", "2026-08-12"), start=1):
            code = f"jackside_202608{index:02d}"
            _daily_campaign(conn, code)
            conn.execute(
                "INSERT INTO jackside_issues(issue_date,title,campaign_code) VALUES (?,?,?)",
                (day, code, code),
            )
            _submission(
                conn,
                client_id=player,
                code=code,
                created_at=f"{day}T15:20:00+00:00",
            )
        old_code = "jackside_20260731"
        _daily_campaign(conn, old_code)
        conn.execute(
            "INSERT INTO jackside_issues(issue_date,title,campaign_code) VALUES ('2026-07-31',?,?)",
            (old_code, old_code),
        )
        _submission(
            conn,
            client_id=old_player,
            code=old_code,
            created_at="2026-07-31T15:20:00+00:00",
        )

        payload = calendar_jackside_rating_payload(
            conn,
            client_id=player,
            period="month",
            as_of=datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc),
        )

        assert payload["label"] == "08.2026"
        assert payload["source_rows"] == 3
        assert payload["stored_source_rows"] == 4
        assert [int(row["client_id"]) for row in payload["rows"]] == [player]
        assert int(conn.execute("SELECT COUNT(*) FROM quiz_submissions").fetchone()[0]) == 4


def test_jackside_year_rating_uses_calendar_year(tmp_path) -> None:
    db_path = tmp_path / "calendar-year.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        player = _client(conn, "Current year")
        old_player = _client(conn, "Previous year")
        for code, day, owner in (
            ("jackside_20260102", "2026-01-02", player),
            ("jackside_20260812", "2026-08-12", player),
            ("jackside_20251231", "2025-12-31", old_player),
        ):
            _daily_campaign(conn, code)
            conn.execute(
                "INSERT INTO jackside_issues(issue_date,title,campaign_code) VALUES (?,?,?)",
                (day, code, code),
            )
            _submission(
                conn,
                client_id=owner,
                code=code,
                created_at=f"{day}T15:20:00+00:00",
                correct=7,
            )

        payload = calendar_jackside_rating_payload(
            conn,
            client_id=player,
            period="year",
            as_of=datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc),
        )

        assert payload["label"] == "2026"
        assert payload["source_rows"] == 2
        assert payload["stored_source_rows"] == 3
        assert [int(row["client_id"]) for row in payload["rows"]] == [player]
        assert int(payload["rows"][0]["completed_count"]) == 2


def test_title_collection_hides_duplicate_visible_names_without_deleting_history() -> None:
    payload = {
        "items": [
            {"kind": "title", "state": "active", "name": "Финалист"},
            {"kind": "achievement", "state": "active", "name": "Финалист"},
            {"kind": "title", "state": "locked", "name": "Легенда JACKSIDE"},
        ],
        "active_count": 2,
        "total_count": 3,
    }
    result = dedupe_title_collection(payload)
    assert [item["name"] for item in result["items"]] == ["Финалист", "Легенда JACKSIDE"]
    assert result["active_count"] == 1
    assert result["total_count"] == 2


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


def test_prelaunch_ui_hotfix_uses_calendar_month_and_year_ui() -> None:
    script = (ROOT / "app/static/js/prelaunch-member.js").read_text(encoding="utf-8")
    assert "/api/account/jackside-calendar-rating" in script
    assert "yearTab.textContent = 'Год'" in script
    assert "Календарный месяц" in script
    assert "Календарный год" in script


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
