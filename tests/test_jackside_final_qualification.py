from __future__ import annotations

from datetime import datetime, timezone

from app.db import init_db, transaction
from app.services.daily_414_final import (
    ensure_final_table,
    reconcile_final_table,
    seed_finalists,
)


def _seed_submission(
    conn,
    *,
    campaign_code: str,
    number: int,
    correct_count: int,
    completion_time_ms: int,
) -> int:
    client_id = int(
        conn.execute(
            "INSERT INTO clients(first_name, source) VALUES (?, 'test')",
            (f"Player {number}",),
        ).lastrowid
    )
    return int(
        conn.execute(
            """
            INSERT INTO quiz_submissions(
                campaign_code, campaign_version, client_id, phone_raw,
                phone_local, answers_json, correct_count, max_correct_count,
                completion_time_ms, main_prize_eligible, main_round_completed,
                ip_hash
            ) VALUES (?, 1, ?, ?, ?, '{}', ?, 10, ?, 1, 1, ?)
            """,
            (
                campaign_code,
                client_id,
                str(number),
                str(number),
                correct_count,
                completion_time_ms,
                f"ip-{number}",
            ),
        ).lastrowid
    )


def test_jackside_final_uses_only_fastest_ten_perfect_scores(tmp_path) -> None:
    db_path = tmp_path / "jackside-perfect-top-ten.sqlite3"
    init_db(db_path)
    campaign_code = "jackside_20260821"
    start = datetime(2026, 8, 21, 15, 19, 14, tzinfo=timezone.utc)

    with transaction(db_path) as conn:
        perfect = []
        for number, completion_time_ms in enumerate(
            (7200, 3100, 8600, 2500, 4300, 6900, 1200, 5400, 9800, 3600, 6100, 4700),
            start=1,
        ):
            submission_id = _seed_submission(
                conn,
                campaign_code=campaign_code,
                number=number,
                correct_count=10,
                completion_time_ms=completion_time_ms,
            )
            perfect.append((submission_id, completion_time_ms))

        fast_nine_of_ten = _seed_submission(
            conn,
            campaign_code=campaign_code,
            number=99,
            correct_count=9,
            completion_time_ms=1,
        )

        table = ensure_final_table(
            conn,
            campaign_code=campaign_code,
            campaign_version=1,
            starts_at=start,
            questions=[{"id": "f1"}],
        )
        seed_finalists(conn, final_table=table)
        finalists = conn.execute(
            """
            SELECT df.submission_id, df.seed, qs.correct_count, qs.completion_time_ms
            FROM daily_414_finalists df
            JOIN quiz_submissions qs ON qs.id=df.submission_id
            WHERE df.final_table_id=?
            ORDER BY df.seed
            """,
            (table["id"],),
        ).fetchall()

    expected = [
        submission_id
        for submission_id, _ in sorted(perfect, key=lambda item: item[1])[:10]
    ]
    actual = [int(row["submission_id"]) for row in finalists]

    assert len(finalists) == 10
    assert actual == expected
    assert fast_nine_of_ten not in actual
    assert all(int(row["correct_count"]) == 10 for row in finalists)


def test_jackside_final_is_cancelled_when_nobody_scores_ten_of_ten(tmp_path) -> None:
    db_path = tmp_path / "jackside-no-perfect.sqlite3"
    init_db(db_path)
    campaign_code = "jackside_20260821_none"
    start = datetime(2026, 8, 21, 15, 19, 14, tzinfo=timezone.utc)

    with transaction(db_path) as conn:
        for number, score in enumerate((9, 8, 7), start=1):
            _seed_submission(
                conn,
                campaign_code=campaign_code,
                number=number,
                correct_count=score,
                completion_time_ms=1000 + number,
            )

        table = ensure_final_table(
            conn,
            campaign_code=campaign_code,
            campaign_version=1,
            starts_at=start,
            questions=[{"id": "f1"}],
        )
        completed = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start,
        )
        finalist_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM daily_414_finalists WHERE final_table_id=?",
                (table["id"],),
            ).fetchone()[0]
        )

    assert finalist_count == 0
    assert completed["status"] == "completed"
    assert completed["outcome"] == "cancelled"
    assert completed["winner_submission_id"] is None


def test_legacy_daily_final_keeps_nonperfect_score_ranking(tmp_path) -> None:
    db_path = tmp_path / "legacy-final-ranking.sqlite3"
    init_db(db_path)
    campaign_code = "daily_final"
    start = datetime(2026, 8, 21, 15, 19, 14, tzinfo=timezone.utc)

    with transaction(db_path) as conn:
        first = _seed_submission(
            conn,
            campaign_code=campaign_code,
            number=1,
            correct_count=9,
            completion_time_ms=2000,
        )
        second = _seed_submission(
            conn,
            campaign_code=campaign_code,
            number=2,
            correct_count=8,
            completion_time_ms=1000,
        )
        table = ensure_final_table(
            conn,
            campaign_code=campaign_code,
            campaign_version=1,
            starts_at=start,
            questions=[{"id": "f1"}],
        )
        seed_finalists(conn, final_table=table)
        finalists = conn.execute(
            """
            SELECT submission_id FROM daily_414_finalists
            WHERE final_table_id=? ORDER BY seed
            """,
            (table["id"],),
        ).fetchall()

    assert [int(row["submission_id"]) for row in finalists] == [first, second]
