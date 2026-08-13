from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app import admin_information_architecture as admin_ia
from app.db import init_db, transaction
from app.jackside_multi_issue import create_issue_multi, ensure_multi_issue_schema
from app.jackside_multi_runtime import (
    current_featured_issue_runtime,
    effective_campaign_schedule_multi,
)
from app.jackside_reschedule_snapshot import reschedule_with_snapshot_cleanup
from app.prelaunch_experience import ensure_prelaunch_schema
from app.services.jackside_issues import ensure_issue_campaign


MOSCOW = ZoneInfo("Europe/Moscow")


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "club.sqlite3"
    init_db(path)
    ensure_multi_issue_schema(path)
    return path


def test_closed_issue_is_hidden_from_member_active_campaign_list(tmp_path: Path) -> None:
    path = _db(tmp_path)
    day = (datetime.now(MOSCOW) + timedelta(days=2)).date()
    start = datetime.combine(day, datetime.min.time(), tzinfo=MOSCOW).replace(hour=12)
    with transaction(path) as conn:
        issue = create_issue_multi(
            conn,
            issue_date_value=day,
            starts_at=start,
            title="Lunch",
        )
        campaign = ensure_issue_campaign(conn, issue=issue)
        conn.execute(
            "UPDATE quiz_campaigns SET is_active=1 WHERE id=?",
            (int(campaign["id"]),),
        )
        conn.execute(
            "UPDATE jackside_issues SET status='closed' WHERE id=?",
            (int(issue["id"]),),
        )
        campaign = conn.execute(
            "SELECT * FROM quiz_campaigns WHERE id=?", (int(campaign["id"]),)
        ).fetchone()
        payload = effective_campaign_schedule_multi(conn, campaign)

    assert int(payload["is_active"]) == 0


def test_scheduled_issue_remains_visible_to_member_campaign_list(tmp_path: Path) -> None:
    path = _db(tmp_path)
    day = (datetime.now(MOSCOW) + timedelta(days=3)).date()
    start = datetime.combine(day, datetime.min.time(), tzinfo=MOSCOW).replace(hour=18, minute=14)
    with transaction(path) as conn:
        issue = create_issue_multi(
            conn,
            issue_date_value=day,
            starts_at=start,
            title="Daily",
        )
        campaign = ensure_issue_campaign(conn, issue=issue)
        conn.execute(
            "UPDATE quiz_campaigns SET is_active=1 WHERE id=?",
            (int(campaign["id"]),),
        )
        conn.execute(
            "UPDATE jackside_issues SET status='scheduled' WHERE id=?",
            (int(issue["id"]),),
        )
        campaign = conn.execute(
            "SELECT * FROM quiz_campaigns WHERE id=?", (int(campaign["id"]),)
        ).fetchone()
        payload = effective_campaign_schedule_multi(conn, campaign)

    assert int(payload["is_active"]) == 1
    assert str(payload["active_from"]).endswith("18:14:00")


def test_featured_release_hands_off_after_previous_final_window(tmp_path: Path) -> None:
    path = _db(tmp_path)
    day = date(2026, 8, 14)
    first_start = datetime(2026, 8, 14, 12, 0, tzinfo=MOSCOW)
    second_start = datetime(2026, 8, 14, 18, 0, tzinfo=MOSCOW)
    with transaction(path) as conn:
        first = create_issue_multi(
            conn,
            issue_date_value=day,
            starts_at=first_start,
            title="Lunch",
        )
        second = create_issue_multi(
            conn,
            issue_date_value=day,
            starts_at=second_start,
            title="Evening",
        )
        for issue in (first, second):
            campaign = ensure_issue_campaign(conn, issue=issue)
            conn.execute(
                "UPDATE quiz_campaigns SET is_active=1 WHERE id=?",
                (int(campaign["id"]),),
            )
            conn.execute(
                "UPDATE jackside_issues SET status='scheduled' WHERE id=?",
                (int(issue["id"]),),
            )
        featured = current_featured_issue_runtime(
            conn,
            now=datetime(2026, 8, 14, 12, 10, tzinfo=MOSCOW),
        )

    assert featured is not None
    assert featured["campaign_code"] == second["campaign_code"]
    assert featured["title"] == "Evening"


def test_reschedule_moves_streak_snapshot_and_cleans_empty_old_day(tmp_path: Path) -> None:
    path = _db(tmp_path)
    first_day = (datetime.now(MOSCOW) + timedelta(days=5)).date()
    next_day = first_day + timedelta(days=1)
    first_start = datetime.combine(first_day, datetime.min.time(), tzinfo=MOSCOW).replace(
        hour=18, minute=14
    )
    next_start = datetime.combine(next_day, datetime.min.time(), tzinfo=MOSCOW).replace(
        hour=20, minute=0
    )
    with transaction(path) as conn:
        ensure_prelaunch_schema(conn)
        issue = create_issue_multi(
            conn,
            issue_date_value=first_day,
            starts_at=first_start,
            title="Move me",
        )
        original_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM jackcoin_economy_snapshots
                WHERE entity_type='jackside_day' AND entity_id=?
                  AND setting_key LIKE 'streak_%'
                """,
                (first_day.isoformat(),),
            ).fetchone()[0]
        )
        reschedule_with_snapshot_cleanup(
            conn,
            issue_id=int(issue["id"]),
            issue_date_value=next_day,
            starts_at=next_start,
            title="Moved",
            timezone_name="Europe/Moscow",
        )
        old_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM jackcoin_economy_snapshots
                WHERE entity_type='jackside_day' AND entity_id=?
                """,
                (first_day.isoformat(),),
            ).fetchone()[0]
        )
        moved_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM jackcoin_economy_snapshots
                WHERE entity_type='jackside_day' AND entity_id=?
                  AND setting_key LIKE 'streak_%'
                """,
                (next_day.isoformat(),),
            ).fetchone()[0]
        )

    assert original_count > 0
    assert old_count == 0
    assert moved_count == original_count


def test_multi_issue_runtime_busts_admin_ia_asset_cache() -> None:
    assert admin_ia.ASSET_VERSION == "admin-ia-v4"
