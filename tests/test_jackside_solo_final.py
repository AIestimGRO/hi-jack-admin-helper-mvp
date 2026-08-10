from datetime import datetime, timedelta

from app.db import init_db, transaction
from app.services.daily_414 import (
    DAILY_414_FINAL_LOBBY_SECONDS,
    DAILY_414_TIME_LIMIT_SECONDS,
    final_table_starts_at,
    main_round_deadline,
)
from app.services.daily_414_final import (
    MIN_FINAL_TABLE_PLAYERS,
    ensure_final_table,
    reconcile_final_table,
)


def _seed_candidate(conn, *, campaign_code: str, client_number: int = 1) -> int:
    client_id = int(
        conn.execute(
            "INSERT INTO clients(first_name, source) VALUES (?, 'test')",
            (f"Solo {client_number}",),
        ).lastrowid
    )
    return int(
        conn.execute(
            """
            INSERT INTO quiz_submissions(
                campaign_code, campaign_version, client_id, phone_raw,
                phone_local, answers_json, correct_count, max_correct_count,
                completion_time_ms, main_prize_eligible, main_round_completed,
                timed_out, ip_hash
            ) VALUES (?, 1, ?, ?, ?, '{}', 10, 10, 120000, 1, 1, 0, ?)
            """,
            (
                campaign_code,
                client_id,
                str(client_number),
                str(client_number),
                f"ip-{client_number}",
            ),
        ).lastrowid
    )


def test_issue_backed_final_starts_after_one_minute_lobby() -> None:
    start = datetime(2026, 8, 10, 18, 14)
    campaign = {
        "code": "jackside_20260810",
        "active_from": start.isoformat(timespec="seconds"),
    }

    main_deadline = main_round_deadline(campaign)
    final_start = final_table_starts_at(campaign)

    assert main_deadline == start + timedelta(seconds=DAILY_414_TIME_LIMIT_SECONDS)
    assert final_start == main_deadline + timedelta(
        seconds=DAILY_414_FINAL_LOBBY_SECONDS
    )
    assert final_start == start + timedelta(minutes=5, seconds=14)


def test_legacy_daily_final_schedule_is_unchanged() -> None:
    start = datetime(2026, 8, 10, 18, 14)
    campaign = {
        "code": "daily_legacy",
        "active_from": start.isoformat(timespec="seconds"),
    }

    assert final_table_starts_at(campaign) == start + timedelta(
        minutes=9, seconds=14
    )


def test_issue_backed_solo_finalist_can_win_by_answering_last_question(tmp_path) -> None:
    db_path = tmp_path / "jackside-solo-win.sqlite3"
    init_db(db_path)
    final_start = datetime(2026, 8, 10, 18, 19, 14)
    campaign_code = "jackside_20260810"

    with transaction(db_path) as conn:
        submission_id = _seed_candidate(conn, campaign_code=campaign_code)
        table = ensure_final_table(
            conn,
            campaign_code=campaign_code,
            campaign_version=1,
            starts_at=final_start,
            questions=[{"id": "final_1"}],
            question_time_seconds=30,
        )
        live = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=final_start,
        )
        finalist = conn.execute(
            """
            SELECT * FROM daily_414_finalists
            WHERE final_table_id=?
            """,
            (table["id"],),
        ).fetchone()

        assert MIN_FINAL_TABLE_PLAYERS == 1
        assert live["status"] == "live"
        assert finalist is not None
        assert finalist["status"] == "active"

        conn.execute(
            """
            INSERT INTO daily_414_final_answers(
                final_table_id, finalist_id, question_index, question_code,
                answer_json, is_correct, response_time_ms, answered_at
            ) VALUES (?, ?, 0, 'final_1', '"yes"', 1, 750, ?)
            """,
            (table["id"], finalist["id"], final_start.isoformat()),
        )
        completed = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=final_start + timedelta(seconds=30),
        )
        winner = conn.execute(
            """
            SELECT status FROM daily_414_finalists
            WHERE final_table_id=? AND submission_id=?
            """,
            (table["id"], submission_id),
        ).fetchone()

    assert completed["status"] == "completed"
    assert completed["outcome"] == "single_winner"
    assert completed["winner_submission_id"] == submission_id
    assert winner["status"] == "winner"


def test_issue_backed_solo_finalist_does_not_win_without_correct_answer(tmp_path) -> None:
    db_path = tmp_path / "jackside-solo-miss.sqlite3"
    init_db(db_path)
    final_start = datetime(2026, 8, 10, 18, 19, 14)
    campaign_code = "jackside_20260810_miss"

    with transaction(db_path) as conn:
        submission_id = _seed_candidate(conn, campaign_code=campaign_code)
        table = ensure_final_table(
            conn,
            campaign_code=campaign_code,
            campaign_version=1,
            starts_at=final_start,
            questions=[{"id": "final_1"}],
            question_time_seconds=30,
        )
        live = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=final_start,
        )
        assert live["status"] == "live"

        completed = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=final_start + timedelta(seconds=30),
        )
        finalist = conn.execute(
            """
            SELECT status FROM daily_414_finalists
            WHERE final_table_id=? AND submission_id=?
            """,
            (table["id"], submission_id),
        ).fetchone()

    assert completed["status"] == "completed"
    assert completed["outcome"] == "no_winner"
    assert completed["winner_submission_id"] is None
    assert finalist["status"] == "eliminated"
