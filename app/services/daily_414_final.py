from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.daily_414 import (
    DAILY_414_FINAL_QUESTION_SECONDS,
    DAILY_414_FINAL_TABLE_SIZE,
)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _question_seconds(final_table: sqlite3.Row | dict[str, Any]) -> int:
    try:
        value = int(final_table["question_time_seconds"] or 0)
    except (KeyError, TypeError, ValueError, IndexError):
        value = DAILY_414_FINAL_QUESTION_SECONDS
    return min(300, max(5, value or DAILY_414_FINAL_QUESTION_SECONDS))


def ensure_final_table(
    conn: sqlite3.Connection,
    *,
    campaign_code: str,
    campaign_version: int,
    starts_at: datetime,
    questions: list[dict[str, Any]],
    question_time_seconds: int = DAILY_414_FINAL_QUESTION_SECONDS,
    prize_type: str = "none",
    prize_catalog_reward_id: int | None = None,
    prize_jackcoin_amount: int = 0,
) -> sqlite3.Row:
    if prize_type == "none" and prize_catalog_reward_id:
        prize_type = "reward_card"
    row = conn.execute(
        """
        SELECT * FROM daily_414_final_tables
        WHERE campaign_code=? AND campaign_version=?
        """,
        (campaign_code, campaign_version),
    ).fetchone()
    payload = json.dumps(questions, ensure_ascii=False, sort_keys=True)
    if row:
        if row["status"] in {"waiting", "unavailable"}:
            snapshot = payload if questions else row["questions_snapshot_json"]
            has_questions = bool(questions) or bool(
                str(row["questions_snapshot_json"] or "").strip() not in {"", "[]"}
            )
            conn.execute(
                """
                UPDATE daily_414_final_tables
                SET starts_at=?, questions_snapshot_json=?, status=?,
                    question_time_seconds=?, prize_type=?,
                    prize_catalog_reward_id=?, prize_jackcoin_amount=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    _timestamp(starts_at),
                    snapshot,
                    "waiting" if has_questions else "unavailable",
                    min(300, max(5, int(question_time_seconds))),
                    prize_type,
                    prize_catalog_reward_id,
                    max(0, int(prize_jackcoin_amount)),
                    row["id"],
                ),
            )
            return conn.execute(
                "SELECT * FROM daily_414_final_tables WHERE id=?",
                (row["id"],),
            ).fetchone()
        return row
    waiting_status = "waiting" if questions else "unavailable"
    try:
        conn.execute(
            """
            INSERT INTO daily_414_final_tables(
                campaign_code, campaign_version, starts_at,
                questions_snapshot_json, question_time_seconds, prize_type,
                prize_catalog_reward_id, prize_jackcoin_amount, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_code,
                campaign_version,
                _timestamp(starts_at),
                payload,
                min(300, max(5, int(question_time_seconds))),
                prize_type,
                prize_catalog_reward_id,
                max(0, int(prize_jackcoin_amount)),
                waiting_status,
            ),
        )
    except sqlite3.IntegrityError:
        pass
    return conn.execute(
        """
        SELECT * FROM daily_414_final_tables
        WHERE campaign_code=? AND campaign_version=?
        """,
        (campaign_code, campaign_version),
    ).fetchone()


def final_table_needs_reconcile(
    table: sqlite3.Row | None,
    *,
    now: datetime,
    schedule_starts_at: datetime | None = None,
) -> bool:
    if table is None:
        return True
    if table["status"] in {"completed", "unavailable"}:
        return False
    now_utc = _as_utc(now)
    starts_at = _as_utc(
        schedule_starts_at
        if schedule_starts_at is not None
        else datetime.fromisoformat(str(table["starts_at"]))
    )
    if table["status"] == "waiting":
        return now_utc >= starts_at
    if table["status"] != "live":
        return False
    try:
        questions = json.loads(table["questions_snapshot_json"] or "[]")
    except json.JSONDecodeError:
        return True
    if not isinstance(questions, list):
        return True
    elapsed = max(0.0, (now_utc - starts_at).total_seconds())
    completed_count = min(
        len(questions),
        int(elapsed // _question_seconds(table)),
    )
    return completed_count > int(table["current_question_index"] or 0)


def seed_finalists(
    conn: sqlite3.Connection,
    *,
    final_table: sqlite3.Row,
) -> None:
    if conn.execute(
        "SELECT 1 FROM daily_414_finalists WHERE final_table_id=? LIMIT 1",
        (final_table["id"],),
    ).fetchone():
        return
    submissions = conn.execute(
        """
        SELECT id, client_id
        FROM quiz_submissions
        WHERE campaign_code=? AND campaign_version=? AND main_prize_eligible=1
        ORDER BY correct_count DESC, IFNULL(completion_time_ms, 2147483647) ASC, id ASC
        LIMIT ?
        """,
        (
            final_table["campaign_code"],
            final_table["campaign_version"],
            DAILY_414_FINAL_TABLE_SIZE,
        ),
    ).fetchall()
    conn.executemany(
        """
        INSERT OR IGNORE INTO daily_414_finalists(
            final_table_id, submission_id, client_id, seed
        ) VALUES (?, ?, ?, ?)
        """,
        [
            (
                final_table["id"],
                submission["id"],
                submission["client_id"],
                seed,
            )
            for seed, submission in enumerate(submissions, start=1)
        ],
    )


def reconcile_final_table(
    conn: sqlite3.Connection,
    *,
    final_table_id: int,
    now: datetime,
    schedule_starts_at: datetime | None = None,
) -> sqlite3.Row:
    table = conn.execute(
        "SELECT * FROM daily_414_final_tables WHERE id=?",
        (final_table_id,),
    ).fetchone()
    if not table or table["status"] in {"completed", "unavailable"}:
        return table
    now_utc = _as_utc(now)
    starts_at = _as_utc(
        schedule_starts_at
        if schedule_starts_at is not None
        else datetime.fromisoformat(str(table["starts_at"]))
    )
    if (
        schedule_starts_at is not None
        and table["status"] in {"waiting", "unavailable"}
        and _as_utc(datetime.fromisoformat(str(table["starts_at"]))) != starts_at
    ):
        conn.execute(
            """
            UPDATE daily_414_final_tables
            SET starts_at=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (_timestamp(starts_at), table["id"]),
        )
        table = conn.execute(
            "SELECT * FROM daily_414_final_tables WHERE id=?",
            (table["id"],),
        ).fetchone()
    if now_utc < starts_at:
        return table

    seed_finalists(conn, final_table=table)
    questions = json.loads(table["questions_snapshot_json"] or "[]")
    finalists = conn.execute(
        """
        SELECT * FROM daily_414_finalists
        WHERE final_table_id=? AND status='active'
        ORDER BY seed
        """,
        (table["id"],),
    ).fetchall()
    if not finalists:
        conn.execute(
            """
            UPDATE daily_414_final_tables
            SET status='completed', completed_at=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (_timestamp(now_utc), table["id"]),
        )
        return conn.execute(
            "SELECT * FROM daily_414_final_tables WHERE id=?",
            (table["id"],),
        ).fetchone()

    if not questions:
        conn.execute(
            """
            UPDATE daily_414_final_tables
            SET status='unavailable', updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (table["id"],),
        )
        return conn.execute(
            "SELECT * FROM daily_414_final_tables WHERE id=?",
            (table["id"],),
        ).fetchone()

    elapsed = max(0.0, (now_utc - starts_at).total_seconds())
    completed_count = min(
        len(questions),
        int(elapsed // _question_seconds(table)),
    )
    current_index = int(table["current_question_index"] or 0)
    for question_index in range(current_index, completed_count):
        active = conn.execute(
            """
            SELECT * FROM daily_414_finalists
            WHERE final_table_id=? AND status='active'
            ORDER BY seed
            """,
            (table["id"],),
        ).fetchall()
        if len(active) <= 1:
            break
        correct_ids = {
            int(row["finalist_id"])
            for row in conn.execute(
                """
                SELECT finalist_id FROM daily_414_final_answers
                WHERE final_table_id=? AND question_index=? AND is_correct=1
                """,
                (table["id"], question_index),
            ).fetchall()
        }
        # If nobody answered correctly, the deal is replayed and nobody is
        # eliminated. This prevents a single round from emptying the table.
        if correct_ids:
            active_ids = {int(row["id"]) for row in active}
            eliminated_ids = active_ids - correct_ids
            if eliminated_ids:
                placeholders = ",".join("?" for _ in eliminated_ids)
                conn.execute(
                    f"""
                    UPDATE daily_414_finalists
                    SET status='eliminated', eliminated_question_index=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders})
                    """,
                    (question_index, *sorted(eliminated_ids)),
                )
        conn.execute(
            """
            UPDATE daily_414_final_tables
            SET current_question_index=?, status='live',
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (question_index + 1, table["id"]),
        )

    active = conn.execute(
        """
        SELECT * FROM daily_414_finalists
        WHERE final_table_id=? AND status='active'
        ORDER BY seed
        """,
        (table["id"],),
    ).fetchall()
    next_index = int(
        conn.execute(
            """
            SELECT current_question_index
            FROM daily_414_final_tables WHERE id=?
            """,
            (table["id"],),
        ).fetchone()[0]
    )
    if len(active) == 1:
        winner = active[0]
        conn.execute(
            """
            UPDATE daily_414_finalists
            SET status='winner', updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (winner["id"],),
        )
        conn.execute(
            """
            UPDATE daily_414_final_tables
            SET status='completed', winner_submission_id=?, completed_at=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (winner["submission_id"], _timestamp(now_utc), table["id"]),
        )
    elif next_index >= len(questions):
        conn.execute(
            """
            UPDATE daily_414_final_tables
            SET status='completed', completed_at=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (_timestamp(now_utc), table["id"]),
        )
    else:
        conn.execute(
            """
            UPDATE daily_414_final_tables
            SET status='live', updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (table["id"],),
        )
    return conn.execute(
        "SELECT * FROM daily_414_final_tables WHERE id=?",
        (table["id"],),
    ).fetchone()


def question_window(
    final_table: sqlite3.Row,
) -> tuple[int, datetime, datetime]:
    question_index = int(final_table["current_question_index"] or 0)
    question_seconds = _question_seconds(final_table)
    starts_at = _as_utc(datetime.fromisoformat(str(final_table["starts_at"]))) + timedelta(
        seconds=question_index * question_seconds
    )
    return (
        question_index,
        starts_at,
        starts_at + timedelta(seconds=question_seconds),
    )
