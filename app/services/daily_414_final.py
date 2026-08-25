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


def _default_question_seconds(
    final_table: sqlite3.Row | dict[str, Any],
) -> int:
    try:
        value = int(final_table["question_time_seconds"] or 0)
    except (KeyError, TypeError, ValueError, IndexError):
        value = DAILY_414_FINAL_QUESTION_SECONDS
    return min(300, max(5, value or DAILY_414_FINAL_QUESTION_SECONDS))


def _questions(final_table: sqlite3.Row | dict[str, Any]) -> list[dict[str, Any]]:
    try:
        payload = json.loads(final_table["questions_snapshot_json"] or "[]")
    except (KeyError, TypeError, json.JSONDecodeError, IndexError):
        return []
    if not isinstance(payload, list):
        return []
    return [item if isinstance(item, dict) else {} for item in payload]


def _question_durations(
    final_table: sqlite3.Row | dict[str, Any],
) -> list[int]:
    fallback = _default_question_seconds(final_table)
    durations: list[int] = []
    for question in _questions(final_table):
        try:
            value = int(question.get("time_limit_seconds") or fallback)
        except (TypeError, ValueError):
            value = fallback
        durations.append(min(300, max(5, value)))
    return durations


def _completed_question_count(
    final_table: sqlite3.Row | dict[str, Any],
    *,
    now: datetime,
    starts_at: datetime,
) -> int:
    elapsed = max(0.0, (_as_utc(now) - _as_utc(starts_at)).total_seconds())
    completed = 0
    boundary = 0
    for duration in _question_durations(final_table):
        boundary += duration
        if elapsed < boundary:
            break
        completed += 1
    return completed


def _resolved_prize_snapshot(
    conn: sqlite3.Connection,
    *,
    campaign_code: str,
    prize_type: str,
    prize_catalog_reward_id: int | None,
    prize_jackcoin_amount: int,
) -> tuple[str, int | None, int]:
    """Resolve the immutable final-table prize from the JACKSIDE campaign.

    Some quiz callers create the final table without prize arguments. The final
    table service owns snapshot creation, so issue-backed JACKSIDE campaigns are
    resolved here rather than by runtime monkey patches around individual callers.
    Explicit prize arguments still win for legacy/direct callers.
    """
    clean_type = str(prize_type or "none")
    clean_catalog_id = (
        int(prize_catalog_reward_id) if prize_catalog_reward_id else None
    )
    clean_jackcoin = max(0, int(prize_jackcoin_amount or 0))
    if clean_type == "none" and clean_catalog_id:
        clean_type = "reward_card"

    explicit = bool(
        clean_type != "none" or clean_catalog_id is not None or clean_jackcoin > 0
    )
    if explicit or not str(campaign_code or "").startswith("jackside_"):
        return clean_type, clean_catalog_id, clean_jackcoin

    campaign = conn.execute(
        """
        SELECT final_prize_type,final_prize_catalog_reward_id,
               final_prize_jackcoin_amount
        FROM quiz_campaigns
        WHERE code=?
        LIMIT 1
        """,
        (campaign_code,),
    ).fetchone()
    if not campaign:
        return clean_type, clean_catalog_id, clean_jackcoin

    campaign_type = str(campaign["final_prize_type"] or "none")
    campaign_catalog_id = (
        int(campaign["final_prize_catalog_reward_id"])
        if campaign["final_prize_catalog_reward_id"]
        else None
    )
    campaign_jackcoin = max(0, int(campaign["final_prize_jackcoin_amount"] or 0))
    if campaign_type == "reward_card" and campaign_catalog_id:
        return "reward_card", campaign_catalog_id, 0
    if campaign_type == "jackcoin" and campaign_jackcoin > 0:
        return "jackcoin", None, campaign_jackcoin
    return "none", None, 0


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
    prize_type, prize_catalog_reward_id, prize_jackcoin_amount = (
        _resolved_prize_snapshot(
            conn,
            campaign_code=campaign_code,
            prize_type=prize_type,
            prize_catalog_reward_id=prize_catalog_reward_id,
            prize_jackcoin_amount=prize_jackcoin_amount,
        )
    )
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
    questions = _questions(table)
    if not questions:
        return True
    completed_count = _completed_question_count(
        table,
        now=now_utc,
        starts_at=starts_at,
    )
    return completed_count > int(table["current_question_index"] or 0)


def seed_finalists(
    conn: sqlite3.Connection,
    *,
    final_table: sqlite3.Row,
) -> None:
    """Seed finalists from completed main rounds using the campaign's own policy."""
    if conn.execute(
        "SELECT 1 FROM daily_414_finalists WHERE final_table_id=? LIMIT 1",
        (final_table["id"],),
    ).fetchone():
        return

    campaign_code = str(final_table["campaign_code"] or "")
    if campaign_code.startswith("jackside_"):
        submissions = conn.execute(
            """
            SELECT id, client_id
            FROM quiz_submissions
            WHERE campaign_code=? AND campaign_version=?
              AND main_prize_eligible=1
              AND IFNULL(main_round_completed, 1)=1
              AND max_correct_count > 0
              AND correct_count = max_correct_count
            ORDER BY created_at ASC, id ASC
            """,
            (campaign_code, final_table["campaign_version"]),
        ).fetchall()
    else:
        submissions = conn.execute(
            """
            SELECT id, client_id
            FROM quiz_submissions
            WHERE campaign_code=? AND campaign_version=?
              AND main_prize_eligible=1
              AND IFNULL(main_round_completed, 1)=1
            ORDER BY correct_count DESC,
                     IFNULL(completion_time_ms, 2147483647) ASC,
                     id ASC
            LIMIT ?
            """,
            (
                campaign_code,
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


def final_winner_announcement(winner_count: int) -> str:
    """Human-readable line for the last-question race winner."""
    count = max(0, int(winner_count))
    if count == 1:
        return "Вы первым правильно ответили на последний вопрос финального стола."
    if count > 1:
        # Kept only for historical tables created before the first-answer rule.
        return f"Победителей финального стола: {count}."
    return "Победитель финального стола не определён."


def final_eliminated_message() -> str:
    return "Ваш ответ не прошёл дальше — вы выбыли из финального стола."


def final_cancelled_message(*, jackcoin_awarded: int = 0) -> str:
    jc = max(0, int(jackcoin_awarded or 0))
    return (
        f"Поздравляю, вы получаете {jc} JC за основной этап. "
        "К сожалению, финальный стол не состоится из-за недостаточного "
        "количества участников."
    )


# New issue-backed JACKSIDE finals can run heads-up or solo. The value remains
# public because the final-status API exposes it to the client.
MIN_FINAL_TABLE_PLAYERS = 1
LEGACY_MIN_FINAL_TABLE_PLAYERS = 2


def _minimum_finalists(final_table: sqlite3.Row | dict[str, Any]) -> int:
    try:
        code = str(final_table["campaign_code"] or "")
    except (KeyError, IndexError, TypeError):
        code = ""
    return (
        MIN_FINAL_TABLE_PLAYERS
        if code.startswith("jackside_")
        else LEGACY_MIN_FINAL_TABLE_PLAYERS
    )


def list_final_winners(
    conn: sqlite3.Connection, *, final_table_id: int
) -> list[sqlite3.Row]:
    winners = conn.execute(
        """
        SELECT df.*, qs.completion_time_ms AS main_completion_time_ms
        FROM daily_414_finalists df
        JOIN quiz_submissions qs ON qs.id=df.submission_id
        WHERE df.final_table_id=? AND df.status='winner'
        ORDER BY IFNULL(df.final_response_time_ms, 2147483647) ASC,
                 IFNULL(qs.completion_time_ms, 2147483647) ASC,
                 df.id ASC
        """,
        (final_table_id,),
    ).fetchall()
    if winners:
        return list(winners)
    table = conn.execute(
        "SELECT * FROM daily_414_final_tables WHERE id=?",
        (final_table_id,),
    ).fetchone()
    if not table or not table["winner_submission_id"]:
        return []
    legacy = conn.execute(
        """
        SELECT df.*, qs.completion_time_ms AS main_completion_time_ms
        FROM daily_414_finalists df
        JOIN quiz_submissions qs ON qs.id=df.submission_id
        WHERE df.final_table_id=? AND df.submission_id=?
        """,
        (final_table_id, table["winner_submission_id"]),
    ).fetchone()
    return [legacy] if legacy else []


def split_jackcoin_amounts(total: int, winner_count: int) -> list[int]:
    """Split prize as evenly as possible; retained for historical co-winner tables."""
    count = max(0, int(winner_count))
    fund = max(0, int(total))
    if count <= 0 or fund <= 0:
        return []
    base = fund // count
    remainder = fund % count
    return [base + (1 if index < remainder else 0) for index in range(count)]


def _mark_completed(
    conn: sqlite3.Connection,
    *,
    table_id: int,
    now_utc: datetime,
    outcome: str,
    winner_submission_id: int | None,
    prize_resolution: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE daily_414_final_tables
        SET status='completed', outcome=?, winner_submission_id=?,
            prize_resolution=COALESCE(?, prize_resolution),
            completed_at=?, updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (
            outcome,
            winner_submission_id,
            prize_resolution,
            _timestamp(now_utc),
            table_id,
        ),
    )


def _promote_winners(
    conn: sqlite3.Connection,
    *,
    table_id: int,
    winners: list[sqlite3.Row],
    now_utc: datetime,
    outcome: str,
    eliminated_question_index: int | None = None,
) -> None:
    if not winners:
        _mark_completed(
            conn,
            table_id=table_id,
            now_utc=now_utc,
            outcome="no_winner",
            winner_submission_id=None,
            prize_resolution="none",
        )
        return
    winner_ids = [int(row["id"]) for row in winners]
    placeholders = ",".join("?" for _ in winner_ids)
    conn.execute(
        f"""
        UPDATE daily_414_finalists
        SET status='eliminated',
            eliminated_question_index=COALESCE(eliminated_question_index, ?),
            updated_at=CURRENT_TIMESTAMP
        WHERE final_table_id=? AND status='active'
          AND id NOT IN ({placeholders})
        """,
        (eliminated_question_index, table_id, *winner_ids),
    )
    conn.execute(
        f"""
        UPDATE daily_414_finalists
        SET status='winner', updated_at=CURRENT_TIMESTAMP
        WHERE id IN ({placeholders})
        """,
        winner_ids,
    )
    ordered = list_final_winners(conn, final_table_id=table_id)
    _mark_completed(
        conn,
        table_id=table_id,
        now_utc=now_utc,
        outcome=outcome,
        winner_submission_id=(
            int(ordered[0]["submission_id"])
            if ordered
            else int(winners[0]["submission_id"])
        ),
    )


def _end_without_winner(
    conn: sqlite3.Connection,
    *,
    table_id: int,
    question_index: int,
    now_utc: datetime,
) -> None:
    conn.execute(
        """
        UPDATE daily_414_finalists
        SET status='eliminated', eliminated_question_index=?,
            updated_at=CURRENT_TIMESTAMP
        WHERE final_table_id=? AND status='active'
        """,
        (question_index, table_id),
    )
    _mark_completed(
        conn,
        table_id=table_id,
        now_utc=now_utc,
        outcome="no_winner",
        winner_submission_id=None,
        prize_resolution="none",
    )


def _first_correct_answer(
    conn: sqlite3.Connection,
    *,
    final_table_id: int,
    question_index: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT dff.*
        FROM daily_414_final_answers dfa
        JOIN daily_414_finalists dff ON dff.id=dfa.finalist_id
        WHERE dfa.final_table_id=? AND dfa.question_index=?
          AND dfa.is_correct=1 AND dff.status='active'
        ORDER BY dfa.answered_at ASC,
                 IFNULL(dfa.response_time_ms, 2147483647) ASC,
                 dfa.id ASC
        LIMIT 1
        """,
        (final_table_id, question_index),
    ).fetchone()


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
    questions = _questions(table)
    finalists = conn.execute(
        """
        SELECT * FROM daily_414_finalists
        WHERE final_table_id=? AND status='active'
        ORDER BY seed
        """,
        (table["id"],),
    ).fetchall()
    # New issue-backed JACKSIDE finals open even for one qualifier. Historic
    # directly-created daily finals keep the old two-player minimum.
    if table["status"] == "waiting" and len(finalists) < _minimum_finalists(table):
        _mark_completed(
            conn,
            table_id=table["id"],
            now_utc=now_utc,
            outcome="cancelled",
            winner_submission_id=None,
            prize_resolution="none",
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

    completed_count = _completed_question_count(
        table,
        now=now_utc,
        starts_at=starts_at,
    )
    current_index = int(table["current_question_index"] or 0)
    last_index = len(questions) - 1

    for question_index in range(current_index, completed_count):
        active = conn.execute(
            """
            SELECT * FROM daily_414_finalists
            WHERE final_table_id=? AND status='active'
            ORDER BY seed
            """,
            (table["id"],),
        ).fetchall()
        if not active:
            _mark_completed(
                conn,
                table_id=table["id"],
                now_utc=now_utc,
                outcome="no_winner",
                winner_submission_id=None,
                prize_resolution="none",
            )
            return conn.execute(
                "SELECT * FROM daily_414_final_tables WHERE id=?",
                (table["id"],),
            ).fetchone()

        if question_index == last_index:
            # The last question is a race: the earliest correct server-recorded
            # answer wins, even if several finalists eventually answer correctly.
            winner = _first_correct_answer(
                conn,
                final_table_id=int(table["id"]),
                question_index=question_index,
            )
            if not winner:
                _end_without_winner(
                    conn,
                    table_id=table["id"],
                    question_index=question_index,
                    now_utc=now_utc,
                )
            else:
                _promote_winners(
                    conn,
                    table_id=table["id"],
                    winners=[winner],
                    now_utc=now_utc,
                    outcome="single_winner",
                    eliminated_question_index=question_index,
                )
            return conn.execute(
                "SELECT * FROM daily_414_final_tables WHERE id=?",
                (table["id"],),
            ).fetchone()

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
        if not correct_ids:
            # All remaining players missed or timed out before the last question.
            _end_without_winner(
                conn,
                table_id=table["id"],
                question_index=question_index,
                now_utc=now_utc,
            )
            return conn.execute(
                "SELECT * FROM daily_414_final_tables WHERE id=?",
                (table["id"],),
            ).fetchone()
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
        # Even if only one player survives, they are not a winner yet. They must
        # continue and correctly answer the final question.
        conn.execute(
            """
            UPDATE daily_414_final_tables
            SET current_question_index=?, status='live',
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (question_index + 1, table["id"]),
        )

    next_index = int(
        conn.execute(
            """
            SELECT current_question_index
            FROM daily_414_final_tables WHERE id=?
            """,
            (table["id"],),
        ).fetchone()[0]
    )
    if next_index >= len(questions):
        # Defensive fallback: a completed question set should have returned from
        # the last-question branch above. Never create new co-winners.
        _mark_completed(
            conn,
            table_id=table["id"],
            now_utc=now_utc,
            outcome="no_winner",
            winner_submission_id=None,
            prize_resolution="none",
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
    durations = _question_durations(final_table)
    fallback = _default_question_seconds(final_table)
    question_seconds = (
        durations[question_index]
        if question_index < len(durations)
        else fallback
    )
    starts_at = _as_utc(datetime.fromisoformat(str(final_table["starts_at"]))) + timedelta(
        seconds=sum(durations[:question_index])
    )
    return (
        question_index,
        starts_at,
        starts_at + timedelta(seconds=question_seconds),
    )
