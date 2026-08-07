"""Acceptance coverage for JACKSIDE final-core stabilization."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db import init_db, transaction
from app.services.daily_414 import (
    daily_main_round_completed,
    main_round_answers_complete,
)
from app.services.daily_414_final import (
    ensure_final_table,
    list_final_winners,
    question_window,
    reconcile_final_table,
    split_jackcoin_amounts,
)
from app.services.jackside_rating import jackside_leaderboard
from app.services.vault import attach_final_table_reward


def _seed_candidate(
    conn,
    *,
    campaign_code: str = "daily_final",
    client_number: int,
    correct_count: int = 10,
    completion_time_ms: int = 10_000,
    main_round_completed: int = 1,
    main_prize_eligible: int = 1,
) -> tuple[int, int]:
    client_id = int(
        conn.execute(
            "INSERT INTO clients(first_name, source) VALUES (?, 'test')",
            (f"Игрок {client_number}",),
        ).lastrowid
    )
    submission_id = int(
        conn.execute(
            """
            INSERT INTO quiz_submissions(
                campaign_code, campaign_version, client_id, phone_raw,
                phone_local, answers_json, correct_count, max_correct_count,
                completion_time_ms, main_prize_eligible, main_round_completed,
                ip_hash, created_at
            ) VALUES (?, 1, ?, ?, ?, '{}', ?, 10, ?, ?, ?, ?, ?)
            """,
            (
                campaign_code,
                client_id,
                str(client_number),
                str(client_number),
                correct_count,
                completion_time_ms,
                main_prize_eligible,
                main_round_completed,
                f"ip-{client_number}",
                "2026-08-01 12:00:00",
            ),
        ).lastrowid
    )
    return client_id, submission_id


def test_main_round_completion_gates() -> None:
    questions = [{"id": "q1", "type": "single_choice"}, {"id": "q2", "type": "text"}]
    assert main_round_answers_complete(
        questions, {"q1": "a", "q2": "ok"}
    )
    assert not main_round_answers_complete(questions, {"q1": "a", "q2": ""})
    assert not main_round_answers_complete(questions, {"q1": "a"})
    assert daily_main_round_completed(
        timed_out=False, questions=questions, answers={"q1": "a", "q2": "ok"}
    )
    assert not daily_main_round_completed(
        timed_out=True, questions=questions, answers={"q1": "a", "q2": "ok"}
    )


def test_fewer_than_ten_eligible_finalists(tmp_path) -> None:
    db_path = tmp_path / "fewer-than-ten.sqlite3"
    init_db(db_path)
    start = datetime(2026, 8, 1, 18, 23, 14, tzinfo=timezone.utc)
    with transaction(db_path) as conn:
        for index in range(1, 4):
            _seed_candidate(conn, client_number=index)
        table = ensure_final_table(
            conn,
            campaign_code="daily_final",
            campaign_version=1,
            starts_at=start,
            questions=[{"id": "f1"}],
        )
        live = reconcile_final_table(
            conn, final_table_id=int(table["id"]), now=start
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM daily_414_finalists WHERE final_table_id=?",
            (table["id"],),
        ).fetchone()[0]
    assert live["status"] == "live"
    assert count == 3


def test_single_winner_and_idempotent_reconcile(tmp_path) -> None:
    db_path = tmp_path / "single-winner.sqlite3"
    init_db(db_path)
    start = datetime(2026, 8, 1, 18, 23, 14, tzinfo=timezone.utc)
    with transaction(db_path) as conn:
        for index in range(1, 3):
            _seed_candidate(conn, client_number=index)
        table = ensure_final_table(
            conn,
            campaign_code="daily_final",
            campaign_version=1,
            starts_at=start,
            questions=[{"id": "f1"}],
        )
        reconcile_final_table(conn, final_table_id=int(table["id"]), now=start)
        finalists = conn.execute(
            """
            SELECT id, seed FROM daily_414_finalists
            WHERE final_table_id=? ORDER BY seed
            """,
            (table["id"],),
        ).fetchall()
        conn.execute(
            """
            INSERT INTO daily_414_final_answers(
                final_table_id, finalist_id, question_index, question_code,
                answer_json, is_correct, response_time_ms, answered_at
            ) VALUES (?, ?, 0, 'f1', '"yes"', 1, 500, ?)
            """,
            (table["id"], finalists[0]["id"], start.isoformat()),
        )
        first = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start + timedelta(seconds=30),
        )
        second = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start + timedelta(seconds=60),
        )
        winners = list_final_winners(conn, final_table_id=int(table["id"]))

    assert first["status"] == "completed"
    assert first["outcome"] == "single_winner"
    assert second["outcome"] == "single_winner"
    assert len(winners) == 1
    assert winners[0]["seed"] == 1


def test_answer_exactly_on_deadline_window() -> None:
    start = datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)
    table = {
        "starts_at": start.isoformat(timespec="milliseconds"),
        "question_time_seconds": 30,
        "questions_snapshot_json": '[{"id":"f1"}]',
        "current_question_index": 0,
    }
    index, begins, deadline = question_window(table)
    assert index == 0
    assert begins == start
    assert deadline == start + timedelta(seconds=30)
    # Boundary rule used by API: now <= deadline is accepted.
    assert start + timedelta(seconds=30) <= deadline
    assert start + timedelta(seconds=30, milliseconds=1) > deadline


def test_incomplete_daily_excluded_from_rating(tmp_path) -> None:
    db_path = tmp_path / "rating-incomplete.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            """
            INSERT INTO quiz_campaigns(code, title, campaign_type)
            VALUES ('daily_r', 'Daily', 'daily_414'),
                   ('classic_r', 'Classic', 'classic')
            """
        )
        complete_id, _ = _seed_candidate(
            conn, campaign_code="daily_r", client_number=1, main_round_completed=1
        )
        incomplete_id, _ = _seed_candidate(
            conn, campaign_code="daily_r", client_number=2, main_round_completed=0
        )
        classic_id, _ = _seed_candidate(
            conn, campaign_code="classic_r", client_number=3, main_round_completed=1
        )
        board = jackside_leaderboard(conn, as_of="2026-08-09 00:00:00")
        scored_ids = {entry["client_id"] for entry in board}

    assert complete_id in scored_ids
    assert classic_id not in scored_ids
    assert incomplete_id not in scored_ids


def test_jackcoin_even_split_without_remainder() -> None:
    assert split_jackcoin_amounts(90, 3) == [30, 30, 30]
    assert sum(split_jackcoin_amounts(90, 3)) == 90


def test_co_winner_list_matches_prize_shares(tmp_path) -> None:
    db_path = tmp_path / "cowinner-list.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        clients = []
        submissions = []
        for index in range(1, 3):
            client_id, submission_id = _seed_candidate(
                conn,
                campaign_code="daily_list",
                client_number=index,
                completion_time_ms=5_000 + index,
            )
            clients.append(client_id)
            submissions.append(submission_id)
        table_id = int(
            conn.execute(
                """
                INSERT INTO daily_414_final_tables(
                    campaign_code, campaign_version, starts_at,
                    questions_snapshot_json, prize_type, prize_jackcoin_amount,
                    status, outcome, winner_submission_id, completed_at
                ) VALUES (
                    'daily_list', 1, '2026-08-01T18:00:00+00:00',
                    '[]', 'jackcoin', 101, 'completed', 'co_winners', ?, CURRENT_TIMESTAMP
                )
                """,
                (submissions[0],),
            ).lastrowid
        )
        for seed, (client_id, submission_id) in enumerate(
            zip(clients, submissions), start=1
        ):
            conn.execute(
                """
                INSERT INTO daily_414_finalists(
                    final_table_id, submission_id, client_id, seed, status,
                    final_response_time_ms
                ) VALUES (?, ?, ?, ?, 'winner', ?)
                """,
                (table_id, submission_id, client_id, seed, seed * 100),
            )
        winners = list_final_winners(conn, final_table_id=table_id)
        prize = attach_final_table_reward(conn, final_table_id=table_id)
        again = attach_final_table_reward(conn, final_table_id=table_id)

    winner_ids = [int(row["client_id"]) for row in winners]
    assert winner_ids == prize["winner_client_ids"] == again["winner_client_ids"]
    assert prize["amount"] == 101
    assert sum(share["amount"] for share in prize["shares"]) == 101
    # Faster final time (seed 1) receives the remainder.
    assert prize["shares"][0]["client_id"] == clients[0]
    assert prize["shares"][0]["amount"] == 51
    assert prize["shares"][1]["amount"] == 50
