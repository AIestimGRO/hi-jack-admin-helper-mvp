from __future__ import annotations

from app.db import init_db, transaction
from app.services.jackside_analytics import (
    build_jackside_analytics,
    get_or_refresh_snapshot,
    refresh_jackside_analytics,
)


def _campaign(conn, code: str, campaign_type: str = "daily_414") -> None:
    conn.execute(
        """
        INSERT INTO quiz_campaigns(code, title, campaign_type)
        VALUES (?, ?, ?)
        """,
        (code, code, campaign_type),
    )


def _client(conn, name: str) -> int:
    return int(
        conn.execute(
            "INSERT INTO clients(first_name, source) VALUES (?, 'test')",
            (name,),
        ).lastrowid
    )


def _submission(
    conn,
    *,
    campaign: str,
    client_id: int,
    correct: int,
    total: int = 10,
    completion_ms: int = 40_000,
    completed: int = 1,
    timed_out: int = 0,
    created_at: str = "2026-08-06T12:00:00+00:00",
) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO quiz_submissions(
                campaign_code, client_id, phone_raw, phone_local,
                answers_json, ip_hash, correct_count, max_correct_count,
                completion_time_ms, main_round_completed, timed_out, created_at
            ) VALUES (?, ?, '', '', '{}', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                campaign,
                client_id,
                f"analytics-{campaign}-{client_id}-{created_at}",
                correct,
                total,
                completion_ms,
                completed,
                timed_out,
                created_at,
            ),
        ).lastrowid
    )


def test_today_rating_uses_moscow_date_and_excludes_incomplete_and_classic(
    tmp_path,
) -> None:
    db_path = tmp_path / "today.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        _campaign(conn, "daily")
        _campaign(conn, "classic", "classic")
        before_midnight = _client(conn, "Before")
        slower = _client(conn, "Slower")
        faster = _client(conn, "Faster")
        incomplete = _client(conn, "Incomplete")
        classic = _client(conn, "Classic")
        _submission(
            conn,
            campaign="daily",
            client_id=before_midnight,
            correct=10,
            created_at="2026-08-05T20:59:59+00:00",
        )
        _submission(
            conn,
            campaign="daily",
            client_id=slower,
            correct=8,
            completion_ms=50_000,
            created_at="2026-08-05T21:00:00+00:00",
        )
        _submission(
            conn,
            campaign="daily",
            client_id=faster,
            correct=8,
            completion_ms=40_000,
            created_at="2026-08-06T10:00:00+00:00",
        )
        _submission(
            conn,
            campaign="daily",
            client_id=incomplete,
            correct=10,
            completion_ms=20_000,
            completed=0,
        )
        _submission(
            conn,
            campaign="classic",
            client_id=classic,
            correct=10,
            completion_ms=10_000,
        )
        payload = build_jackside_analytics(
            conn,
            as_of="2026-08-06T12:00:00+00:00",
            timezone_name="Europe/Moscow",
        )

    today = payload["today"]
    assert [row["client_id"] for row in today] == [faster, slower]
    assert {row["client_id"] for row in payload["all_time"]} == {
        before_midnight,
        slower,
        faster,
    }


def test_today_rating_marks_finalists_and_co_winners(tmp_path) -> None:
    db_path = tmp_path / "finals.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        _campaign(conn, "daily")
        first = _client(conn, "First")
        second = _client(conn, "Second")
        first_submission = _submission(
            conn, campaign="daily", client_id=first, correct=10
        )
        second_submission = _submission(
            conn, campaign="daily", client_id=second, correct=9
        )
        table_id = int(
            conn.execute(
                """
                INSERT INTO daily_414_final_tables(
                    campaign_code, campaign_version, starts_at,
                    questions_snapshot_json, status, outcome, prize_resolution
                ) VALUES (
                    'daily', 1, '2026-08-06T15:20:00+00:00', '[]',
                    'completed', 'co_winners', 'awarded'
                )
                """
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO daily_414_finalists(
                final_table_id, submission_id, client_id, seed, status
            ) VALUES (?, ?, ?, 1, 'winner'), (?, ?, ?, 2, 'winner')
            """,
            (
                table_id,
                first_submission,
                first,
                table_id,
                second_submission,
                second,
            ),
        )
        payload = build_jackside_analytics(
            conn, as_of="2026-08-06T16:00:00+00:00"
        )

    assert all(row["is_finalist"] for row in payload["today"])
    assert all(row["is_winner"] for row in payload["today"])
    assert all(row["is_co_winner"] for row in payload["today"])


def test_all_time_points_formula_and_tie_breaks(tmp_path) -> None:
    db_path = tmp_path / "all-time.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        _campaign(conn, "daily")
        perfect_winner = _client(conn, "Perfect winner")
        regular_finalist = _client(conn, "Regular finalist")
        winner_submission = _submission(
            conn,
            campaign="daily",
            client_id=perfect_winner,
            correct=10,
            completion_ms=30_000,
        )
        finalist_submission = _submission(
            conn,
            campaign="daily",
            client_id=regular_finalist,
            correct=8,
            completion_ms=28_000,
        )
        _submission(
            conn,
            campaign="daily",
            client_id=regular_finalist,
            correct=7,
            completion_ms=32_000,
            created_at="2026-08-05T12:00:00+00:00",
        )
        table_id = int(
            conn.execute(
                """
                INSERT INTO daily_414_final_tables(
                    campaign_code, campaign_version, starts_at,
                    questions_snapshot_json, status, outcome, prize_resolution
                ) VALUES (
                    'daily', 1, '2026-08-06T15:20:00+00:00', '[]',
                    'completed', 'single_winner', 'awarded'
                )
                """
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO daily_414_finalists(
                final_table_id, submission_id, client_id, seed, status
            ) VALUES (?, ?, ?, 1, 'winner'), (?, ?, ?, 2, 'eliminated')
            """,
            (
                table_id,
                winner_submission,
                perfect_winner,
                table_id,
                finalist_submission,
                regular_finalist,
            ),
        )
        payload = build_jackside_analytics(
            conn, as_of="2026-08-06T16:00:00+00:00"
        )

    by_client = {row["client_id"]: row for row in payload["all_time"]}
    assert by_client[perfect_winner]["points"] == 17
    assert by_client[regular_finalist]["points"] == 17
    assert by_client[perfect_winner]["finals_count"] == 1
    assert by_client[perfect_winner]["wins_count"] == 1
    assert by_client[regular_finalist]["wins_count"] == 0
    assert payload["all_time"][0]["client_id"] == perfect_winner


def test_month_rating_is_rolling_daily_only_and_complete_only(tmp_path) -> None:
    db_path = tmp_path / "month.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        _campaign(conn, "daily")
        _campaign(conn, "classic", "classic")
        player = _client(conn, "Player")
        classic = _client(conn, "Classic")
        for day in (1, 2, 3):
            _submission(
                conn,
                campaign="daily",
                client_id=player,
                correct=9,
                created_at=f"2026-08-0{day}T10:00:00+00:00",
            )
        _submission(
            conn,
            campaign="daily",
            client_id=player,
            correct=10,
            created_at="2026-06-01T10:00:00+00:00",
        )
        _submission(
            conn,
            campaign="daily",
            client_id=player,
            correct=10,
            completed=0,
            created_at="2026-08-04T10:00:00+00:00",
        )
        _submission(
            conn,
            campaign="classic",
            client_id=classic,
            correct=10,
            created_at="2026-08-04T10:00:00+00:00",
        )
        payload = build_jackside_analytics(
            conn, as_of="2026-08-06T12:00:00+00:00"
        )

    assert len(payload["month"]) == 1
    assert payload["month"][0]["client_id"] == player
    assert payload["month"][0]["completed_count"] == 3
    assert payload["month"][0]["accuracy"] == 90
    assert payload["month"][0]["place"] == 1


def test_cached_snapshot_throttles_dirty_rebuilds_to_five_minutes(tmp_path) -> None:
    db_path = tmp_path / "cache.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        _campaign(conn, "daily")
        player = _client(conn, "Player")
        first = refresh_jackside_analytics(
            conn, as_of="2026-08-06T12:00:00+00:00"
        )
        second = get_or_refresh_snapshot(
            conn, as_of="2026-08-06T12:00:01+00:00"
        )
        assert second["generated_at"] == first["generated_at"]
        _submission(conn, campaign="daily", client_id=player, correct=8)
        still_cached = get_or_refresh_snapshot(
            conn, as_of="2026-08-06T12:00:02+00:00"
        )
        third = get_or_refresh_snapshot(
            conn, as_of="2026-08-06T12:05:01+00:00"
        )

    assert still_cached["generated_at"] == first["generated_at"]
    assert still_cached["all_time"] == []
    assert third["generated_at"] != first["generated_at"]
    assert third["all_time"][0]["client_id"] == player


def test_admin_completion_and_timeout_rates_use_started_attempts(tmp_path) -> None:
    db_path = tmp_path / "admin-rates.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        _campaign(conn, "daily")
        completed_client = _client(conn, "Completed")
        timeout_client = _client(conn, "Timeout")
        first_attempt = int(
            conn.execute(
                """
                INSERT INTO quiz_attempts(
                    campaign_code, client_id, token_hash,
                    questions_snapshot_json, status, ip_hash, created_at
                ) VALUES (
                    'daily', ?, 'attempt-complete', '[]', 'submitted',
                    'ip-complete', '2026-08-06T10:00:00+00:00'
                )
                """,
                (completed_client,),
            ).lastrowid
        )
        second_attempt = int(
            conn.execute(
                """
                INSERT INTO quiz_attempts(
                    campaign_code, client_id, token_hash,
                    questions_snapshot_json, status, ip_hash, created_at
                ) VALUES (
                    'daily', ?, 'attempt-timeout', '[]', 'submitted',
                    'ip-timeout', '2026-08-06T10:01:00+00:00'
                )
                """,
                (timeout_client,),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO quiz_submissions(
                attempt_id, campaign_code, client_id, phone_raw, phone_local,
                answers_json, ip_hash, correct_count, max_correct_count,
                main_round_completed, timed_out, created_at
            ) VALUES
              (?, 'daily', ?, '', '', '{}', 'submission-complete', 8, 10, 1, 0,
               '2026-08-06T10:02:00+00:00'),
              (?, 'daily', ?, '', '', '{}', 'submission-timeout', 5, 10, 0, 1,
               '2026-08-06T10:03:00+00:00')
            """,
            (
                first_attempt,
                completed_client,
                second_attempt,
                timeout_client,
            ),
        )
        payload = build_jackside_analytics(
            conn, as_of="2026-08-06T12:00:00+00:00"
        )

    participants = payload["admin"]["participants"]
    assert participants["completion_rate"] == 50
    assert participants["timeout_rate"] == 50


def test_retention_uses_exact_club_local_d1_d7_d30_boundaries(tmp_path) -> None:
    db_path = tmp_path / "retention.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        _campaign(conn, "daily")
        cohorts = (
            ("D1", "2026-08-05T09:00:00+00:00"),
            ("D7", "2026-07-30T09:00:00+00:00"),
            ("D30", "2026-07-07T09:00:00+00:00"),
        )
        for label, registered_at in cohorts:
            client_id = _client(conn, label)
            conn.execute(
                """
                INSERT INTO member_accounts(
                    client_id, email, email_normalized, password_hash,
                    email_verified_at, created_at
                ) VALUES (?, ?, ?, 'hash', ?, ?)
                """,
                (
                    client_id,
                    f"{label.lower()}@example.com",
                    f"{label.lower()}@example.com",
                    registered_at,
                    registered_at,
                ),
            )
            _submission(
                conn,
                campaign="daily",
                client_id=client_id,
                correct=7,
                created_at="2026-08-06T09:00:00+00:00",
            )

        payload = build_jackside_analytics(
            conn,
            as_of="2026-08-06T12:00:00+00:00",
            timezone_name="Europe/Moscow",
        )

    retention = payload["admin"]["participants"]["retention"]
    assert retention["1"] == {"eligible": 3, "returned": 1, "rate": 33.3}
    assert retention["7"] == {"eligible": 2, "returned": 1, "rate": 50}
    assert retention["30"] == {"eligible": 1, "returned": 1, "rate": 100}
