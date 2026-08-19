from __future__ import annotations

from datetime import date, datetime
from typing import Any

import sqlite3

from app import jackside_multi_issue as multi_issue
from app.jackside_flexible_labels import normalize_builtin_perfect_labels
from app.services import jackside_copy as copy_service
from app.services import jackside_issues as issue_service


_PREVIOUS_EFFECTIVE_SCHEDULE = issue_service.effective_campaign_schedule
_PREVIOUS_VALIDATE_ISSUE = issue_service.validate_issue_for_publish
_PREVIOUS_RESCHEDULE = multi_issue.reschedule_future_issue


def effective_campaign_schedule_compat(
    conn: sqlite3.Connection,
    campaign: sqlite3.Row | dict[str, Any],
    *,
    timezone_name: str = "Europe/Moscow",
) -> dict[str, Any]:
    payload = _PREVIOUS_EFFECTIVE_SCHEDULE(
        conn, campaign, timezone_name=timezone_name
    )
    if str(payload.get("campaign_type") or "") != "daily_414":
        return payload
    code = str(payload.get("code") or "")
    if not code:
        return payload
    issue = issue_service.get_issue_by_campaign(conn, code)
    if issue and str(issue["status"] or "") == "draft":
        try:
            payload["is_active"] = int(campaign["is_active"] or 0)
        except (KeyError, IndexError, TypeError):
            pass
    return payload


def validate_issue_for_publish_compat(
    conn: sqlite3.Connection,
    issue: sqlite3.Row | dict[str, Any],
) -> list[str]:
    normalize_builtin_perfect_labels(conn)
    current = issue
    if str(issue["rules_version"] or "") == copy_service.DEFAULT_RULES_VERSION:
        active = issue_service.ensure_default_rules(conn)
        if str(active["version"] or "") != str(issue["rules_version"] or ""):
            conn.execute(
                """
                UPDATE jackside_issues
                SET rules_version_id=?, rules_version=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (int(active["id"]), str(active["version"]), int(issue["id"])),
            )
            refreshed = issue_service.get_issue(conn, int(issue["id"]))
            if refreshed:
                current = refreshed
    return _PREVIOUS_VALIDATE_ISSUE(conn, current)


def reschedule_future_issue_compat(
    conn: sqlite3.Connection,
    *,
    issue_id: int,
    issue_date_value: date,
    starts_at: datetime,
    title: str | None,
    timezone_name: str,
) -> sqlite3.Row:
    exact_start = issue_service._timestamp(issue_service._as_utc(starts_at))
    duplicate = conn.execute(
        """
        SELECT id FROM jackside_issues
        WHERE starts_at=? AND id<>? AND status<>'cancelled'
        ORDER BY id DESC LIMIT 1
        """,
        (exact_start, int(issue_id)),
    ).fetchone()
    if duplicate:
        raise ValueError("На это время уже существует выпуск JACKSIDE")
    return _PREVIOUS_RESCHEDULE(
        conn,
        issue_id=issue_id,
        issue_date_value=issue_date_value,
        starts_at=starts_at,
        title=title,
        timezone_name=timezone_name,
    )


def result_copy_for_score_compat(
    correct_count: int,
    *,
    final_eligible: bool = True,
) -> dict[str, str]:
    """Keep historic result codes without presenting a fixed ten-question scale."""
    score = max(0, int(correct_count))
    if score <= 3:
        code = "0_3"
    elif score <= 6:
        code = "4_6"
    elif score <= 8:
        code = "7_8"
    elif score == 9:
        code = "9"
    else:
        code = "10"
    if final_eligible:
        message = (
            "Основная часть завершена. JACKCOIN за правильные ответы уже начислены. "
            "После закрытия общего таймера система определит участников финального стола."
        )
    else:
        message = (
            "Основная часть завершена. Этот заход не вошёл в отбор финального стола, "
            "JACKCOIN за завершённую игру уже на балансе."
        )
    return {
        "code": code,
        "title": "Основной раунд завершён",
        "message": message,
    }


issue_service.effective_campaign_schedule = effective_campaign_schedule_compat
issue_service.validate_issue_for_publish = validate_issue_for_publish_compat
multi_issue.reschedule_future_issue = reschedule_future_issue_compat
copy_service.result_copy_for_score = result_copy_for_score_compat


__all__ = [
    "effective_campaign_schedule_compat",
    "reschedule_future_issue_compat",
    "result_copy_for_score_compat",
    "validate_issue_for_publish_compat",
]