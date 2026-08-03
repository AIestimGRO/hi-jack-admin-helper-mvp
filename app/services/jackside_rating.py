from __future__ import annotations

import sqlite3
from fractions import Fraction
from typing import Any


def jackside_leaderboard(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Rank quiz participants by lifetime answer accuracy.

    Accuracy is the primary key. Equal accuracy is resolved by total correct
    answers, completed games, the freshest result and finally the client id.
    """
    rows = conn.execute(
        """
        SELECT c.id AS client_id,
               COALESCE(NULLIF(c.first_name, ''), NULLIF(c.nickname, ''),
                        CASE WHEN NULLIF(c.username, '') IS NOT NULL
                             THEN '@' || c.username END,
                        'Игрок HJ #' || c.id) AS display_name,
               COUNT(qs.id) AS completed_count,
               COALESCE(SUM(qs.correct_count), 0) AS correct_total,
               COALESCE(SUM(qs.max_correct_count), 0) AS question_total,
               MAX(qs.created_at) AS last_result_at
        FROM clients c
        JOIN quiz_submissions qs ON qs.client_id=c.id
        WHERE qs.max_correct_count > 0
        GROUP BY c.id
        """
    ).fetchall()
    entries: list[dict[str, Any]] = []
    for row in rows:
        correct_total = int(row["correct_total"] or 0)
        question_total = int(row["question_total"] or 0)
        accuracy_value = round(correct_total * 100 / question_total, 1)
        entries.append(
            {
                "client_id": int(row["client_id"]),
                "display_name": str(row["display_name"]),
                "completed_count": int(row["completed_count"] or 0),
                "correct_total": correct_total,
                "question_total": question_total,
                "accuracy": (
                    int(accuracy_value)
                    if float(accuracy_value).is_integer()
                    else accuracy_value
                ),
                "last_result_at": str(row["last_result_at"] or ""),
                "_ratio": Fraction(correct_total, question_total),
            }
        )
    entries.sort(
        key=lambda item: (
            item["_ratio"],
            item["correct_total"],
            item["completed_count"],
            item["last_result_at"],
            -item["client_id"],
        ),
        reverse=True,
    )
    for place, entry in enumerate(entries, start=1):
        entry["place"] = place
        entry.pop("_ratio", None)
    return entries
