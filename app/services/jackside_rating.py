from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from math import sqrt
from typing import Any


RATING_WINDOW_DAYS = 30
MIN_RATED_GAMES = 3
MIN_RATED_ANSWERS = 30
FULL_ACTIVITY_GAMES = 8
ACCURACY_WEIGHT = 0.85
ACTIVITY_WEIGHT = 0.15
WILSON_Z = 1.96


def _as_utc_naive(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    parsed = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str)
        else value
    )
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _wilson_lower_bound(correct: int, total: int) -> float:
    """Return the conservative 95% lower bound of answer accuracy."""
    if total <= 0:
        return 0.0
    ratio = correct / total
    z_squared = WILSON_Z**2
    numerator = ratio + z_squared / (2 * total) - WILSON_Z * sqrt(
        (ratio * (1 - ratio) + z_squared / (4 * total)) / total
    )
    return 100 * numerator / (1 + z_squared / total)


def _display_percent(value: float) -> int | float:
    rounded = round(value, 1)
    return int(rounded) if rounded.is_integer() else rounded


def jackside_leaderboard(
    conn: sqlite3.Connection,
    *,
    as_of: datetime | str | None = None,
) -> list[dict[str, Any]]:
    """Rank recent quiz participation with accuracy and consistency.

    Only the latest 30 days count. Players receive a place after three games
    and 30 assessed answers; everyone else remains visible in calibration.
    The ranked score is 85% Wilson-adjusted accuracy and 15% activity, with
    full activity credit reached at eight completed games.
    """
    reference = _as_utc_naive(as_of)
    cutoff = reference - timedelta(days=RATING_WINDOW_DAYS)
    rows = conn.execute(
        """
        SELECT c.id AS client_id,
               COALESCE(NULLIF(c.first_name, ''), NULLIF(c.nickname, ''),
                        CASE WHEN NULLIF(c.username, '') IS NOT NULL
                             THEN '@' || c.username END,
                        'Игрок HJ #' || c.id) AS display_name,
               COUNT(qs.id) AS completed_count,
               COUNT(DISTINCT date(qs.created_at)) AS active_days,
               COALESCE(SUM(qs.correct_count), 0) AS correct_total,
               COALESCE(SUM(qs.max_correct_count), 0) AS question_total,
               MAX(qs.created_at) AS last_result_at
        FROM clients c
        JOIN quiz_submissions qs ON qs.client_id=c.id
        LEFT JOIN quiz_campaigns qc ON qc.code=qs.campaign_code
        WHERE qs.max_correct_count > 0
          AND qs.created_at >= ?
          AND qs.created_at <= ?
          AND (
            qc.campaign_type IS NULL
            OR qc.campaign_type != 'daily_414'
            OR IFNULL(qs.main_round_completed, 1)=1
          )
        GROUP BY c.id
        """,
        (
            cutoff.strftime("%Y-%m-%d %H:%M:%S"),
            reference.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    ).fetchall()
    ranked: list[dict[str, Any]] = []
    calibrating: list[dict[str, Any]] = []
    for row in rows:
        completed_count = int(row["completed_count"] or 0)
        active_days = int(row["active_days"] or 0)
        correct_total = int(row["correct_total"] or 0)
        question_total = int(row["question_total"] or 0)
        accuracy_value = correct_total * 100 / question_total
        confirmed_accuracy = _wilson_lower_bound(correct_total, question_total)
        activity_score = min(completed_count / FULL_ACTIVITY_GAMES, 1) * 100
        rating_score = (
            confirmed_accuracy * ACCURACY_WEIGHT
            + activity_score * ACTIVITY_WEIGHT
        )
        is_calibrating = (
            completed_count < MIN_RATED_GAMES
            or question_total < MIN_RATED_ANSWERS
        )
        entry = {
            "client_id": int(row["client_id"]),
            "display_name": str(row["display_name"]),
            "completed_count": completed_count,
            "active_days": active_days,
            "correct_total": correct_total,
            "question_total": question_total,
            "accuracy": _display_percent(accuracy_value),
            "confirmed_accuracy": _display_percent(confirmed_accuracy),
            "activity_score": _display_percent(activity_score),
            "rating_score": _display_percent(rating_score),
            "last_result_at": str(row["last_result_at"] or ""),
            "status": "calibration" if is_calibrating else "ranked",
            "calibration_games_left": max(0, MIN_RATED_GAMES - completed_count),
            "calibration_answers_left": max(0, MIN_RATED_ANSWERS - question_total),
            "calibration_progress": round(
                min(
                    completed_count / MIN_RATED_GAMES,
                    question_total / MIN_RATED_ANSWERS,
                    1,
                )
                * 100
            ),
            "_confirmed_accuracy_raw": confirmed_accuracy,
            "_rating_score_raw": rating_score,
        }
        (calibrating if is_calibrating else ranked).append(entry)

    ranked.sort(
        key=lambda item: (
            item["_rating_score_raw"],
            item["active_days"],
            item["correct_total"],
            item["last_result_at"],
            -item["client_id"],
        ),
        reverse=True,
    )
    for place, entry in enumerate(ranked, start=1):
        entry["place"] = place
    calibrating.sort(
        key=lambda item: (
            item["calibration_progress"],
            item["_confirmed_accuracy_raw"],
            item["active_days"],
            item["correct_total"],
            item["last_result_at"],
            -item["client_id"],
        ),
        reverse=True,
    )
    for entry in calibrating:
        entry["place"] = None
    entries = ranked + calibrating
    for entry in entries:
        entry.pop("_confirmed_accuracy_raw", None)
        entry.pop("_rating_score_raw", None)
    return entries
