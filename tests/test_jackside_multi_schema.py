from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app import jackside_multi_issue as multi_issue
from app.db import connect, init_db, transaction
from app.jackside_multi_schema import ensure_multi_issue_schema


MOSCOW = ZoneInfo("Europe/Moscow")


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "club.sqlite3"
    init_db(path)
    return path


def _issue_date_is_unique(path: Path) -> bool:
    with connect(path) as conn:
        for index in conn.execute("PRAGMA index_list('jackside_issues')").fetchall():
            if not int(index[2]):
                continue
            columns = [
                str(row[2])
                for row in conn.execute(
                    f"PRAGMA index_info('{index[1]}')"
                ).fetchall()
            ]
            if columns == ["issue_date"]:
                return True
    return False


def test_migration_preserves_external_trigger_and_view_that_reference_issues(
    tmp_path: Path,
) -> None:
    path = _db(tmp_path)
    day = date(2026, 8, 14)
    with transaction(path) as conn:
        multi_issue.create_issue_multi(
            conn,
            issue_date_value=day,
            starts_at=datetime(2026, 8, 14, 18, 14, tzinfo=MOSCOW),
            title="Probe",
        )
        conn.execute(
            """
            CREATE VIEW v_jackside_issue_probe AS
            SELECT id, campaign_code, issue_date
            FROM jackside_issues
            """
        )
        conn.execute(
            """
            CREATE TRIGGER trg_jackside_shared_attempt_deadline_probe
            AFTER INSERT ON quiz_attempts
            WHEN EXISTS (
                SELECT 1
                FROM jackside_issues
                WHERE campaign_code=NEW.campaign_code
            )
            BEGIN
                SELECT COUNT(*) FROM jackside_issues;
            END
            """
        )

    assert _issue_date_is_unique(path) is True
    assert ensure_multi_issue_schema(path) is True
    assert _issue_date_is_unique(path) is False

    with connect(path) as conn:
        trigger = conn.execute(
            """
            SELECT tbl_name, sql
            FROM sqlite_master
            WHERE type='trigger'
              AND name='trg_jackside_shared_attempt_deadline_probe'
            """
        ).fetchone()
        view = conn.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type='view'
              AND name='v_jackside_issue_probe'
            """
        ).fetchone()
        assert trigger is not None
        assert trigger["tbl_name"] == "quiz_attempts"
        assert "jackside_issues" in str(trigger["sql"])
        assert view is not None
        assert "jackside_issues" in str(view["sql"])
        assert conn.execute("SELECT COUNT(*) FROM v_jackside_issue_probe").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_schema_helper_patches_legacy_multi_issue_symbol() -> None:
    assert multi_issue.ensure_multi_issue_schema is ensure_multi_issue_schema
