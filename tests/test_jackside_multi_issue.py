from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.db import connect, init_db, transaction
from app.jackside_multi_issue import (
    create_issue_multi,
    current_featured_issue_multi,
    ensure_multi_issue_schema,
    reschedule_future_issue,
    same_day_issues,
)
from app.services.daily_414 import _next_streak
from app.services.jackside_issues import ensure_issue_campaign


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
                for row in conn.execute(f"PRAGMA index_info('{index[1]}')").fetchall()
            ]
            if columns == ["issue_date"]:
                return True
    return False


def test_schema_migration_preserves_issue_ids_and_child_links(tmp_path: Path) -> None:
    path = _db(tmp_path)
    day = date(2026, 8, 14)
    with transaction(path) as conn:
        client_id = int(
            conn.execute(
                "INSERT INTO clients(first_name,source) VALUES ('Test','test')"
            ).lastrowid
        )
        first = create_issue_multi(
            conn,
            issue_date_value=day,
            starts_at=datetime(2026, 8, 14, 18, 14, tzinfo=MOSCOW),
            title="First",
        )
        issue_id = int(first["id"])
        conn.execute(
            "INSERT INTO jackside_issue_participants(issue_id,client_id) VALUES (?,?)",
            (issue_id, client_id),
        )

    assert _issue_date_is_unique(path) is True
    assert ensure_multi_issue_schema(path) is True
    assert _issue_date_is_unique(path) is False

    with connect(path) as conn:
        row = conn.execute(
            "SELECT id,title FROM jackside_issues WHERE id=?", (issue_id,)
        ).fetchone()
        child = conn.execute(
            "SELECT issue_id,client_id FROM jackside_issue_participants WHERE issue_id=?",
            (issue_id,),
        ).fetchone()
        assert row is not None and int(row["id"]) == issue_id
        assert row["title"] == "First"
        assert child is not None and int(child["client_id"]) == client_id
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_two_releases_same_day_get_independent_campaign_codes(tmp_path: Path) -> None:
    path = _db(tmp_path)
    ensure_multi_issue_schema(path)
    day = date(2026, 8, 14)
    with transaction(path) as conn:
        first = create_issue_multi(
            conn,
            issue_date_value=day,
            starts_at=datetime(2026, 8, 14, 12, 30, tzinfo=MOSCOW),
            title="Lunch",
        )
        second = create_issue_multi(
            conn,
            issue_date_value=day,
            starts_at=datetime(2026, 8, 14, 18, 14, tzinfo=MOSCOW),
            title="Daily",
        )
        third = create_issue_multi(
            conn,
            issue_date_value=day,
            starts_at=datetime(2026, 8, 14, 18, 14, tzinfo=MOSCOW),
            title="Daily 2",
        )

    assert first["issue_date"] == second["issue_date"] == third["issue_date"] == "2026-08-14"
    assert first["campaign_code"] == "jackside_20260814"
    assert second["campaign_code"] == "jackside_20260814_1814"
    assert third["campaign_code"] == "jackside_20260814_1814_2"
    assert len({first["campaign_code"], second["campaign_code"], third["campaign_code"]}) == 3


def test_featured_issue_chooses_nearest_upcoming_release(tmp_path: Path) -> None:
    path = _db(tmp_path)
    ensure_multi_issue_schema(path)
    day = date(2026, 8, 14)
    with transaction(path) as conn:
        first = create_issue_multi(
            conn,
            issue_date_value=day,
            starts_at=datetime(2026, 8, 14, 18, 14, tzinfo=MOSCOW),
            title="18:14",
        )
        second = create_issue_multi(
            conn,
            issue_date_value=day,
            starts_at=datetime(2026, 8, 14, 22, 0, tzinfo=MOSCOW),
            title="22:00",
        )
        for issue in (first, second):
            campaign = ensure_issue_campaign(conn, issue=issue)
            conn.execute(
                "UPDATE jackside_issues SET status='scheduled' WHERE id=?",
                (int(issue["id"]),),
            )
            conn.execute(
                "UPDATE quiz_campaigns SET is_active=1 WHERE id=?",
                (int(campaign["id"]),),
            )
        featured = current_featured_issue_multi(
            conn,
            now=datetime(2026, 8, 14, 12, 0, tzinfo=MOSCOW),
        )

    assert featured is not None
    assert featured["campaign_code"] == first["campaign_code"]
    assert featured["title"] == "18:14"


def test_reschedule_future_release_keeps_identity_and_syncs_campaign(tmp_path: Path) -> None:
    path = _db(tmp_path)
    ensure_multi_issue_schema(path)
    initial_day = (datetime.now(MOSCOW) + timedelta(days=3)).date()
    next_day = initial_day + timedelta(days=1)
    start = datetime.combine(initial_day, datetime.min.time(), tzinfo=MOSCOW).replace(
        hour=18, minute=14
    )
    new_start = datetime.combine(next_day, datetime.min.time(), tzinfo=MOSCOW).replace(
        hour=20, minute=30
    )
    with transaction(path) as conn:
        issue = create_issue_multi(
            conn,
            issue_date_value=initial_day,
            starts_at=start,
            title="Before",
        )
        campaign = ensure_issue_campaign(conn, issue=issue)
        conn.execute(
            "UPDATE jackside_issues SET status='scheduled' WHERE id=?",
            (int(issue["id"]),),
        )
        original_code = str(issue["campaign_code"])
        updated = reschedule_future_issue(
            conn,
            issue_id=int(issue["id"]),
            issue_date_value=next_day,
            starts_at=new_start,
            title="After",
            timezone_name="Europe/Moscow",
        )
        campaign_after = conn.execute(
            "SELECT * FROM quiz_campaigns WHERE id=?", (int(campaign["id"]),)
        ).fetchone()

    assert updated["issue_date"] == next_day.isoformat()
    assert updated["campaign_code"] == original_code
    assert updated["title"] == "After"
    assert updated["status"] == "scheduled"
    assert campaign_after["title"] == "After"
    assert campaign_after["active_from"] == f"{next_day.isoformat()}T20:30:00"


def test_same_day_conflicts_exclude_the_issue_being_edited(tmp_path: Path) -> None:
    path = _db(tmp_path)
    ensure_multi_issue_schema(path)
    day = (datetime.now(MOSCOW) + timedelta(days=4)).date()
    with transaction(path) as conn:
        one = create_issue_multi(
            conn,
            issue_date_value=day,
            starts_at=datetime.combine(day, datetime.min.time(), tzinfo=MOSCOW).replace(hour=12),
            title="One",
        )
        two = create_issue_multi(
            conn,
            issue_date_value=day,
            starts_at=datetime.combine(day, datetime.min.time(), tzinfo=MOSCOW).replace(hour=18),
            title="Two",
        )
        rows = same_day_issues(
            conn,
            issue_date_value=day,
            exclude_issue_id=int(one["id"]),
        )

    assert [int(row["id"]) for row in rows] == [int(two["id"])]


def test_multiple_releases_same_day_do_not_extend_day_streak() -> None:
    day = date(2026, 8, 14)
    assert _next_streak(previous_date=day, previous_streak=7, current_date=day) == 7
    assert _next_streak(
        previous_date=day,
        previous_streak=7,
        current_date=day + timedelta(days=1),
    ) == 8


def test_admin_assets_use_multi_issue_create_and_hide_jackside_version_button() -> None:
    root = Path(__file__).resolve().parents[1]
    template = (root / "app/templates/admin_jackside_workspace.html").read_text(
        encoding="utf-8"
    )
    script = (root / "app/static/js/admin-ia-v2.js").read_text(encoding="utf-8")
    assert 'action="/api/master/jackside/create-release-v2"' in template
    assert "data-same-day-confirm" in template
    assert "data-edit-issue" in template
    assert "publish-version" in script
    assert "publishForm.remove()" in script
