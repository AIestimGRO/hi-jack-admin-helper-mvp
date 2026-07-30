from datetime import date, datetime, timedelta

from app.db import connect, init_db, transaction
from app.services.daily_414 import (
    award_daily_jackcoin,
    main_prize_eligible,
)
from app.services.quiz import load_builder_questions


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


def test_daily_414_jackcoin_amounts_are_configurable(tmp_path) -> None:
    db_path = tmp_path / "daily-custom-jackcoin.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        client_id = int(
            conn.execute(
                "INSERT INTO clients(first_name, source) VALUES ('Игрок', 'test')"
            ).lastrowid
        )
        result = award_daily_jackcoin(
            conn,
            client_id=client_id,
            submission_id=201,
            issue_day=date(2026, 7, 30),
            correct_count=10,
            max_correct_count=10,
            jackcoin_per_correct=7,
            jackcoin_completion_bonus=13,
            jackcoin_perfect_bonus=29,
        )

    assert result["answers"] == 70
    assert result["completion"] == 13
    assert result["perfect"] == 29
    assert result["total"] == 112


def test_daily_414_question_order_ignores_visual_section_order(tmp_path) -> None:
    db_path = tmp_path / "daily-order.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            """
            INSERT INTO quiz_campaigns(code, title, campaign_type)
            VALUES ('daily_order', '4:14 order', 'daily_414')
            """
        )
        first_section = int(
            conn.execute(
                """
                INSERT INTO quiz_sections(campaign_code, title, position)
                VALUES ('daily_order', 'Поздний фон', 900)
                """
            ).lastrowid
        )
        second_section = int(
            conn.execute(
                """
                INSERT INTO quiz_sections(campaign_code, title, position)
                VALUES ('daily_order', 'Ранний фон', 10)
                """
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO quiz_questions(
                campaign_code, code, type, title, section_id, position, is_active
            ) VALUES
                ('daily_order', 'q1', 'text', 'Первый', ?, 10, 1),
                ('daily_order', 'q2', 'text', 'Второй', ?, 20, 1)
            """,
            (first_section, second_section),
        )

        questions = load_builder_questions(conn, "daily_order")

    assert [question["id"] for question in questions] == ["q1", "q2"]
