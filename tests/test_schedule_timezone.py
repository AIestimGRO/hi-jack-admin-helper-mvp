"""Schedule timezone correctness for JACKSIDE / daily_414."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.daily_414 import campaign_local_datetime, main_prize_eligible


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
