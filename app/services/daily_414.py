from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


DAILY_414_TIME_LIMIT_SECONDS = 254
DAILY_414_QUESTION_COUNT = 10
# The main round is one shared club clock. This compatibility constant is kept
# for API/status consumers that still call it the entry window.
DAILY_414_ENTRY_WINDOW_SECONDS = DAILY_414_TIME_LIMIT_SECONDS
LEGACY_DAILY_414_ENTRY_WINDOW_SECONDS = 5 * 60
DAILY_414_FINAL_TABLE_DELAY_SECONDS = DAILY_414_TIME_LIMIT_SECONDS
LEGACY_DAILY_414_FINAL_TABLE_DELAY_SECONDS = (
    LEGACY_DAILY_414_ENTRY_WINDOW_SECONDS + DAILY_414_TIME_LIMIT_SECONDS
)
DAILY_414_FINAL_QUESTION_SECONDS = 30
DAILY_414_FINAL_TABLE_SIZE = 10

JACKCOIN_PER_CORRECT = 5
JACKCOIN_COMPLETION_BONUS = 10
JACKCOIN_PERFECT_BONUS = 20
JACKCOIN_STREAK_BONUSES = {
    3: 10,
    7: 30,
    14: 70,
    30: 150,
}
DEFAULT_CAMPAIGN_TIMEZONE = "Europe/Moscow"


def campaign_local_datetime(
    value: str | datetime | None,
    *,
    timezone_name: str = DEFAULT_CAMPAIGN_TIMEZONE,
) -> datetime | None:
    """Normalize campaign schedule stamps to naive local wall time."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        return parsed.astimezone(ZoneInfo(timezone_name)).replace(tzinfo=None)
    return parsed.replace(tzinfo=None)


def stage_for_question(index: int) -> str:
    if index < 2:
        return "preflop"
    if index < 5:
        return "flop"
    if index < 8:
        return "turn"
    return "river"


def public_daily_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, question in enumerate(questions):
        item = dict(question)
        item["game_stage"] = stage_for_question(index)
        item["river_reveal"] = index == DAILY_414_QUESTION_COUNT - 1
        result.append(item)
    return result


def validate_daily_questions(
    questions: list[dict[str, Any]], campaign_code: str
) -> None:
    if len(questions) != DAILY_414_QUESTION_COUNT:
        raise ValueError("daily_414_requires_ten_questions")
    if any(str(question.get("campaign")) != campaign_code for question in questions):
        raise ValueError("daily_414_requires_own_questions")


def issue_date(
    campaign: sqlite3.Row | dict[str, Any], *, local_finished_at: datetime
) -> date:
    active_from = campaign["active_from"]
    if active_from:
        return datetime.fromisoformat(str(active_from)).date()
    return local_finished_at.date()


def _campaign_code(campaign: sqlite3.Row | dict[str, Any]) -> str:
    try:
        return str(campaign["code"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def _is_legacy_daily_campaign(campaign: sqlite3.Row | dict[str, Any]) -> bool:
    code = _campaign_code(campaign)
    return bool(code and not code.startswith("jackside_"))


def main_round_deadline(
    campaign: sqlite3.Row | dict[str, Any],
    *,
    timezone_name: str = DEFAULT_CAMPAIGN_TIMEZONE,
) -> datetime | None:
    """Return the shared main-round deadline in club-local wall time."""
    start = campaign_local_datetime(
        campaign["active_from"], timezone_name=timezone_name
    )
    if not start:
        return None
    return start + timedelta(seconds=DAILY_414_TIME_LIMIT_SECONDS)


def main_prize_eligible(
    campaign: sqlite3.Row | dict[str, Any],
    *,
    started_at: datetime,
    timezone_name: str = DEFAULT_CAMPAIGN_TIMEZONE,
) -> bool:
    start = campaign_local_datetime(
        campaign["active_from"], timezone_name=timezone_name
    )
    if not start:
        return False
    deadline = (
        start + timedelta(seconds=LEGACY_DAILY_414_ENTRY_WINDOW_SECONDS)
        if _is_legacy_daily_campaign(campaign)
        else main_round_deadline(campaign, timezone_name=timezone_name)
    )
    if not deadline:
        return False
    local_started = campaign_local_datetime(
        started_at, timezone_name=timezone_name
    )
    if local_started is None:
        return False
    # New issue-backed JACKSIDE: a late join never receives a fresh personal
    # 4:14. Historic non-issue campaigns retain their old 5-minute entry rule.
    return start <= local_started < deadline


def final_table_starts_at(
    campaign: sqlite3.Row | dict[str, Any],
    *,
    timezone_name: str = DEFAULT_CAMPAIGN_TIMEZONE,
) -> datetime | None:
    start = campaign_local_datetime(
        campaign["active_from"], timezone_name=timezone_name
    )
    if not start:
        return None
    # New issue-backed JACKSIDE releases use jackside_YYYYMMDD codes and start
    # the final immediately after the shared 4:14 closes. Keep old directly
    # created daily_414 campaigns on the historical 5:00 + 4:14 schedule so
    # existing archived/test releases are not silently reinterpreted.
    if _is_legacy_daily_campaign(campaign):
        return start + timedelta(seconds=LEGACY_DAILY_414_FINAL_TABLE_DELAY_SECONDS)
    return start + timedelta(seconds=DAILY_414_FINAL_TABLE_DELAY_SECONDS)


def main_round_answers_complete(
    questions: list[dict[str, Any]], answers: dict[str, Any]
) -> bool:
    """True when every question has a non-empty saved answer."""
    if not questions:
        return False
    for question in questions:
        question_id = str(question.get("id") or "")
        if not question_id:
            return False
        value = answers.get(question_id)
        if question.get("type") == "multi_choice":
            if not isinstance(value, list) or not value:
                return False
            continue
        if not str(value or "").strip():
            return False
    return True


def daily_main_round_completed(
    *,
    timed_out: bool,
    questions: list[dict[str, Any]],
    answers: dict[str, Any],
) -> bool:
    """Economy/rating/final eligibility requires a finished, fully answered run."""
    return (not timed_out) and main_round_answers_complete(questions, answers)


def final_table_candidate_eligible(
    campaign: sqlite3.Row | dict[str, Any],
    *,
    started_at: datetime,
    finished_at: datetime,
    timed_out: bool = False,
    main_round_completed: bool = True,
    timezone_name: str = DEFAULT_CAMPAIGN_TIMEZONE,
) -> bool:
    final_start = final_table_starts_at(campaign, timezone_name=timezone_name)
    local_finished = campaign_local_datetime(
        finished_at, timezone_name=timezone_name
    )
    return bool(
        main_round_completed
        and not timed_out
        and final_start
        and local_finished
        and main_prize_eligible(
            campaign, started_at=started_at, timezone_name=timezone_name
        )
        and local_finished <= final_start
    )


def rank_final_candidates(
    rows: list[sqlite3.Row] | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Order eligible submissions: more correct first, then faster, then earlier id."""
    ordered = sorted(
        rows,
        key=lambda row: (
            -int(row["correct_count"] or 0),
            int(row["completion_time_ms"] if row["completion_time_ms"] is not None else 2_147_483_647),
            int(row["id"]),
        ),
    )
    return [
        {
            "place": index,
            "submission_id": int(row["id"]),
            "client_id": int(row["client_id"]),
            "correct_count": int(row["correct_count"] or 0),
            "completion_time_ms": row["completion_time_ms"],
        }
        for index, row in enumerate(ordered, start=1)
    ]


def elapsed_milliseconds(*, started_at: datetime, finished_at: datetime) -> int:
    return max(0, int((finished_at - started_at).total_seconds() * 1000))


def _next_streak(
    *,
    previous_date: date | None,
    previous_streak: int,
    current_date: date,
) -> int:
    if previous_date == current_date:
        return max(1, previous_streak)
    if previous_date == current_date - timedelta(days=1):
        return max(1, previous_streak + 1)
    return 1


def award_daily_jackcoin(
    conn: sqlite3.Connection,
    *,
    client_id: int,
    submission_id: int,
    issue_day: date,
    correct_count: int,
    max_correct_count: int,
    jackcoin_per_correct: int = JACKCOIN_PER_CORRECT,
    jackcoin_completion_bonus: int = JACKCOIN_COMPLETION_BONUS,
    jackcoin_perfect_bonus: int = JACKCOIN_PERFECT_BONUS,
) -> dict[str, int]:
    progress = conn.execute(
        "SELECT * FROM daily_414_progress WHERE client_id=?",
        (client_id,),
    ).fetchone()
    previous_date = (
        date.fromisoformat(str(progress["last_issue_date"]))
        if progress and progress["last_issue_date"]
        else None
    )
    previous_streak = int(progress["current_streak"]) if progress else 0
    streak = _next_streak(
        previous_date=previous_date,
        previous_streak=previous_streak,
        current_date=issue_day,
    )
    best_streak = max(streak, int(progress["best_streak"]) if progress else 0)
    streak_bonus = JACKCOIN_STREAK_BONUSES.get(streak, 0)
    per_correct = max(0, int(jackcoin_per_correct))
    completion_amount = max(0, int(jackcoin_completion_bonus))
    perfect_bonus = max(0, int(jackcoin_perfect_bonus))
    answer_amount = max(0, int(correct_count)) * per_correct
    perfect_amount = (
        perfect_bonus
        if (
            max_correct_count == DAILY_414_QUESTION_COUNT
            and correct_count == DAILY_414_QUESTION_COUNT
        )
        else 0
    )
    total = answer_amount + completion_amount + perfect_amount + streak_bonus

    conn.execute(
        """
        INSERT INTO daily_414_progress(
            client_id, current_streak, best_streak, last_issue_date, updated_at
        ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(client_id) DO UPDATE SET
            current_streak=excluded.current_streak,
            best_streak=MAX(daily_414_progress.best_streak, excluded.best_streak),
            last_issue_date=excluded.last_issue_date,
            updated_at=CURRENT_TIMESTAMP
        """,
        (client_id, streak, best_streak, issue_day.isoformat()),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO jackcoin_ledger(
            client_id, amount, operation_type, source_type, source_id,
            idempotency_key, comment
        ) VALUES (?, ?, 'earn', 'daily_414', ?, ?, ?)
        """,
        (
            client_id,
            total,
            str(submission_id),
            f"daily_414:submission:{submission_id}",
            (
                f"4:14: {correct_count} правильных × {per_correct} JC"
                f" + {completion_amount} JC за завершение"
                f"{f' + {perfect_amount} JC за 10/10' if perfect_amount else ''}"
                f"{f' + {streak_bonus} JC за серию {streak} дней' if streak_bonus else ''}"
            ),
        ),
    )
    return {
        "total": total,
        "answers": answer_amount,
        "completion": completion_amount,
        "perfect": perfect_amount,
        "streak_bonus": streak_bonus,
        "streak_days": streak,
        "best_streak": best_streak,
    }
