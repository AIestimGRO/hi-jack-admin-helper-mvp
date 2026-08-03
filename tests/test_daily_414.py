from datetime import date, datetime, timedelta, timezone

from app.db import connect, init_db, transaction
from app.services.daily_414 import (
    award_daily_jackcoin,
    final_table_candidate_eligible,
    final_table_starts_at,
    main_prize_eligible,
    rank_final_candidates,
)
from app.services.daily_414_final import (
    ensure_final_table,
    question_window,
    reconcile_final_table,
)
from app.services.quiz import load_builder_questions


def test_rank_final_candidates_orders_by_score_then_speed() -> None:
    ranked = rank_final_candidates(
        [
            {"id": 1, "client_id": 11, "correct_count": 8, "completion_time_ms": 10000},
            {"id": 2, "client_id": 22, "correct_count": 9, "completion_time_ms": 20000},
            {"id": 3, "client_id": 33, "correct_count": 9, "completion_time_ms": 15000},
        ]
    )
    assert [item["client_id"] for item in ranked] == [33, 22, 11]
    assert [item["place"] for item in ranked] == [1, 2, 3]


def test_daily_414_final_table_entry_window_is_five_minutes() -> None:
    start = datetime(2026, 7, 29, 18, 14)
    campaign = {"active_from": start.isoformat(timespec="minutes")}

    assert main_prize_eligible(
        campaign,
        started_at=start + timedelta(minutes=5),
    )
    assert not main_prize_eligible(
        campaign,
        started_at=start + timedelta(minutes=5, seconds=1),
    )


def test_daily_414_candidate_must_finish_before_shared_final_start() -> None:
    start = datetime(2026, 7, 29, 18, 14)
    campaign = {"active_from": start.isoformat(timespec="minutes")}
    final_start = final_table_starts_at(campaign)

    assert final_start == start + timedelta(minutes=9, seconds=14)
    assert final_table_candidate_eligible(
        campaign,
        started_at=start + timedelta(minutes=4, seconds=59),
        finished_at=final_start,
    )
    assert not final_table_candidate_eligible(
        campaign,
        started_at=start + timedelta(minutes=4, seconds=59),
        finished_at=final_start + timedelta(milliseconds=1),
    )
    assert not final_table_candidate_eligible(
        campaign,
        started_at=start + timedelta(minutes=5, seconds=1),
        finished_at=final_start,
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


def _seed_final_candidate(
    conn,
    *,
    campaign_code: str,
    client_number: int,
    correct_count: int,
    completion_time_ms: int,
) -> int:
    client_id = int(
        conn.execute(
            "INSERT INTO clients(first_name, source) VALUES (?, 'test')",
            (f"Игрок {client_number}",),
        ).lastrowid
    )
    return int(
        conn.execute(
            """
            INSERT INTO quiz_submissions(
                campaign_code, campaign_version, client_id, phone_raw,
                phone_local, answers_json, correct_count, max_correct_count,
                completion_time_ms, main_prize_eligible, ip_hash
            ) VALUES ('daily_final', 1, ?, ?, ?, '{}', ?, 10, ?, 1, ?)
            """,
            (
                client_id,
                str(client_number),
                str(client_number),
                correct_count,
                completion_time_ms,
                f"ip-{client_number}",
            ),
        ).lastrowid
    )


def test_daily_414_final_table_takes_top_ten_in_score_time_order(
    tmp_path,
) -> None:
    db_path = tmp_path / "daily-final-top-ten.sqlite3"
    init_db(db_path)
    start = datetime(2026, 7, 30, 18, 23, 14)
    with transaction(db_path) as conn:
        submission_ids = [
            _seed_final_candidate(
                conn,
                campaign_code="daily_final",
                client_number=index,
                correct_count=10 - (index // 4),
                completion_time_ms=10_000 + index,
            )
            for index in range(1, 12)
        ]
        table = ensure_final_table(
            conn,
            campaign_code="daily_final",
            campaign_version=1,
            starts_at=start,
            questions=[{"id": "f1"}],
        )
        reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start,
        )
        finalists = conn.execute(
            """
            SELECT submission_id, seed FROM daily_414_finalists
            WHERE final_table_id=? ORDER BY seed
            """,
            (table["id"],),
        ).fetchall()

    assert len(finalists) == 10
    assert [row["submission_id"] for row in finalists] == submission_ids[:10]


def test_daily_414_final_questions_eliminate_synchronously(tmp_path) -> None:
    db_path = tmp_path / "daily-final-elimination.sqlite3"
    init_db(db_path)
    start = datetime(2026, 7, 30, 18, 23, 14)
    questions = [{"id": "f1"}, {"id": "f2"}]
    with transaction(db_path) as conn:
        for index in range(1, 4):
            _seed_final_candidate(
                conn,
                campaign_code="daily_final",
                client_number=index,
                correct_count=10,
                completion_time_ms=10_000 + index,
            )
        table = ensure_final_table(
            conn,
            campaign_code="daily_final",
            campaign_version=1,
            starts_at=start,
            questions=questions,
        )
        live = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start,
        )
        finalists = conn.execute(
            """
            SELECT * FROM daily_414_finalists
            WHERE final_table_id=? ORDER BY seed
            """,
            (table["id"],),
        ).fetchall()
        assert live["status"] == "live"
        assert len(finalists) == 3

        for finalist in finalists[:2]:
            conn.execute(
                """
                INSERT INTO daily_414_final_answers(
                    final_table_id, finalist_id, question_index, question_code,
                    answer_json, is_correct, response_time_ms, answered_at
                ) VALUES (?, ?, 0, 'f1', '"yes"', 1, 1000, ?)
                """,
                (table["id"], finalist["id"], start.isoformat()),
            )
        after_first = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start + timedelta(seconds=30),
        )
        statuses = conn.execute(
            """
            SELECT seed, status FROM daily_414_finalists
            WHERE final_table_id=? ORDER BY seed
            """,
            (table["id"],),
        ).fetchall()
        assert after_first["current_question_index"] == 1
        assert [row["status"] for row in statuses] == [
            "active",
            "active",
            "eliminated",
        ]

        second = finalists[1]
        conn.execute(
            """
            INSERT INTO daily_414_final_answers(
                final_table_id, finalist_id, question_index, question_code,
                answer_json, is_correct, response_time_ms, answered_at
            ) VALUES (?, ?, 1, 'f2', '"yes"', 1, 900, ?)
            """,
            (
                table["id"],
                second["id"],
                (start + timedelta(seconds=31)).isoformat(),
            ),
        )
        completed = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start + timedelta(seconds=60),
        )
        winner = conn.execute(
            """
            SELECT seed, status FROM daily_414_finalists
            WHERE final_table_id=? AND status='winner'
            """,
            (table["id"],),
        ).fetchone()

    assert completed["status"] == "completed"
    assert winner["seed"] == 2


def test_daily_414_final_round_keeps_table_when_nobody_is_correct(
    tmp_path,
) -> None:
    db_path = tmp_path / "daily-final-redeal.sqlite3"
    init_db(db_path)
    start = datetime(2026, 7, 30, 18, 23, 14)
    with transaction(db_path) as conn:
        for index in range(1, 3):
            _seed_final_candidate(
                conn,
                campaign_code="daily_final",
                client_number=index,
                correct_count=9,
                completion_time_ms=12_000 + index,
            )
        table = ensure_final_table(
            conn,
            campaign_code="daily_final",
            campaign_version=1,
            starts_at=start,
            questions=[{"id": "f1"}, {"id": "f2"}],
        )
        reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start,
        )
        after_missed_question = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start + timedelta(seconds=30),
        )
        active_count = conn.execute(
            """
            SELECT COUNT(*) FROM daily_414_finalists
            WHERE final_table_id=? AND status='active'
            """,
            (table["id"],),
        ).fetchone()[0]

    assert after_missed_question["status"] == "live"
    assert after_missed_question["current_question_index"] == 1
    assert active_count == 2


def test_daily_414_final_question_time_is_snapshotted(tmp_path) -> None:
    db_path = tmp_path / "daily-final-custom-time.sqlite3"
    init_db(db_path)
    start = datetime(2026, 8, 3, 18, 23, 14)
    with transaction(db_path) as conn:
        for index in range(1, 3):
            _seed_final_candidate(
                conn,
                campaign_code="daily_final",
                client_number=index,
                correct_count=10,
                completion_time_ms=10_000 + index,
            )
        table = ensure_final_table(
            conn,
            campaign_code="daily_final",
            campaign_version=1,
            starts_at=start,
            questions=[{"id": "f1"}, {"id": "f2"}],
            question_time_seconds=45,
        )
        live = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start,
        )
        still_first = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start + timedelta(seconds=44),
        )
        second = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start + timedelta(seconds=45),
        )

    assert live["question_time_seconds"] == 45
    assert still_first["current_question_index"] == 0
    assert second["current_question_index"] == 1


def test_daily_414_final_questions_use_individual_snapshotted_times(tmp_path) -> None:
    db_path = tmp_path / "daily-final-individual-times.sqlite3"
    init_db(db_path)
    start = datetime(2026, 8, 3, 18, 14)
    with transaction(db_path) as conn:
        for index in range(1, 3):
            _seed_final_candidate(
                conn,
                campaign_code="daily_final",
                client_number=index,
                correct_count=10,
                completion_time_ms=10_000 + index,
            )
        table = ensure_final_table(
            conn,
            campaign_code="daily_final",
            campaign_version=1,
            starts_at=start,
            questions=[
                {"id": "f1", "time_limit_seconds": 10},
                {"id": "f2", "time_limit_seconds": 20},
                {"id": "f3"},
            ],
            question_time_seconds=45,
        )
        first = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start + timedelta(seconds=9),
        )
        first_window = question_window(first)
        second = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start + timedelta(seconds=10),
        )
        second_window = question_window(second)
        still_second = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start + timedelta(seconds=29),
        )
        third = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start + timedelta(seconds=30),
        )
        third_window = question_window(third)

    assert first["current_question_index"] == 0
    utc_start = start.replace(tzinfo=timezone.utc)
    assert first_window[1:] == (
        utc_start,
        utc_start + timedelta(seconds=10),
    )
    assert second["current_question_index"] == 1
    assert second_window[1:] == (
        utc_start + timedelta(seconds=10),
        utc_start + timedelta(seconds=30),
    )
    assert still_second["current_question_index"] == 1
    assert third["current_question_index"] == 2
    assert third_window[1:] == (
        utc_start + timedelta(seconds=30),
        utc_start + timedelta(seconds=75),
    )
