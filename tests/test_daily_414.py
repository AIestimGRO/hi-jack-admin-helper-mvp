from datetime import date, datetime, timedelta

from app.db import connect, init_db, transaction
from app.services.daily_414 import (
    award_daily_jackcoin,
    main_prize_eligible,
)


def test_daily_414_main_prize_has_thirty_second_start_grace() -> None:
    start = datetime(2026, 7, 29, 18, 14)
    campaign = {"active_from": start.isoformat(timespec="minutes")}

    assert main_prize_eligible(
        campaign,
        started_at=start + timedelta(seconds=30),
    )
    assert not main_prize_eligible(
        campaign,
        started_at=start + timedelta(seconds=31),
    )


def test_daily_414_jackcoin_streak_bonus_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "daily.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        client_id = int(
            conn.execute(
                """
                INSERT INTO clients(first_name, source)
                VALUES ('Игрок', 'test')
                """
            ).lastrowid
        )
        first = award_daily_jackcoin(
            conn,
            client_id=client_id,
            submission_id=101,
            issue_day=date(2026, 7, 27),
            correct_count=2,
            max_correct_count=10,
        )
        second = award_daily_jackcoin(
            conn,
            client_id=client_id,
            submission_id=102,
            issue_day=date(2026, 7, 28),
            correct_count=2,
            max_correct_count=10,
        )
        third = award_daily_jackcoin(
            conn,
            client_id=client_id,
            submission_id=103,
            issue_day=date(2026, 7, 29),
            correct_count=2,
            max_correct_count=10,
        )
        repeated = award_daily_jackcoin(
            conn,
            client_id=client_id,
            submission_id=103,
            issue_day=date(2026, 7, 29),
            correct_count=2,
            max_correct_count=10,
        )

    assert first["total"] == 20
    assert second["total"] == 20
    assert third["streak_days"] == 3
    assert third["streak_bonus"] == 10
    assert third["total"] == 30
    assert repeated["streak_days"] == 3
    with connect(db_path) as conn:
        assert conn.execute(
            "SELECT SUM(amount) FROM jackcoin_ledger WHERE client_id=?",
            (client_id,),
        ).fetchone()[0] == 70
        assert conn.execute(
            "SELECT COUNT(*) FROM jackcoin_ledger WHERE client_id=?",
            (client_id,),
        ).fetchone()[0] == 3
