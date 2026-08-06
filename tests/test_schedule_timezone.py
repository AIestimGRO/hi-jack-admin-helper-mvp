"""Schedule timezone correctness for JACKSIDE / daily_414."""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.db import init_db, transaction
from app.services.daily_414 import campaign_local_datetime, main_prize_eligible
from app.services.jackside_issues import create_issue, issue_schedule_local


def test_campaign_local_datetime_converts_utc_offset() -> None:
    local = campaign_local_datetime(
        "2026-08-06T15:14:00+00:00", timezone_name="Europe/Moscow"
    )
    assert local == datetime(2026, 8, 6, 18, 14)


def test_campaign_local_datetime_keeps_naive_local() -> None:
    local = campaign_local_datetime(
        "2026-08-06T18:14:00", timezone_name="Europe/Moscow"
    )
    assert local == datetime(2026, 8, 6, 18, 14)


def test_replace_tzinfo_must_not_be_used_for_utc_stamps() -> None:
    """Regression: .replace(tzinfo=Moscow) on UTC wall clock looks 3h early."""
    utc_stamp = datetime.fromisoformat("2026-08-06T15:14:00+00:00")
    wrong = utc_stamp.replace(tzinfo=ZoneInfo("Europe/Moscow"))
    right = utc_stamp.astimezone(ZoneInfo("Europe/Moscow")).replace(tzinfo=None)
    assert wrong.hour == 15
    assert right.hour == 18
    assert main_prize_eligible(
        {"active_from": "2026-08-06T15:14:00+00:00"},
        started_at=datetime(2026, 8, 6, 18, 16),
        timezone_name="Europe/Moscow",
    )


def test_issue_closes_at_moscow_local_end_of_day(tmp_path) -> None:
    db_path = tmp_path / "moscow-end.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        issue = create_issue(
            conn,
            issue_date_value=date(2026, 8, 10),
            starts_at=datetime(2026, 8, 10, 15, 14, tzinfo=timezone.utc),
            timezone_name="Europe/Moscow",
        )
        assert datetime.fromisoformat(issue["ends_at"]) == datetime(
            2026, 8, 10, 20, 59, 59, tzinfo=timezone.utc
        )
        assert issue_schedule_local(
            issue, timezone_name="Europe/Moscow"
        ) == ("2026-08-10T18:14:00", "2026-08-10T23:59:59")


def test_issue_closes_at_new_york_local_end_of_day(tmp_path) -> None:
    db_path = tmp_path / "new-york-end.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        issue = create_issue(
            conn,
            issue_date_value=date(2026, 8, 10),
            starts_at=datetime(2026, 8, 10, 22, 14, tzinfo=timezone.utc),
            timezone_name="America/New_York",
        )
        assert datetime.fromisoformat(issue["ends_at"]) == datetime(
            2026, 8, 11, 3, 59, 59, tzinfo=timezone.utc
        )
        assert issue_schedule_local(
            issue, timezone_name="America/New_York"
        ) == ("2026-08-10T18:14:00", "2026-08-10T23:59:59")
