from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app import admin_information_architecture as admin_ia
from app.db import init_db, transaction
from app.jackside_multi_issue import create_issue_multi, ensure_multi_issue_schema
from app.jackside_multi_runtime import effective_campaign_schedule_multi
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


def test_multi_issue_runtime_busts_admin_ia_asset_cache() -> None:
    assert admin_ia.ASSET_VERSION == "admin-ia-v4"
