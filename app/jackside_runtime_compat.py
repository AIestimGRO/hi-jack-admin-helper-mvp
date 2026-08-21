from __future__ import annotations

from datetime import date, datetime
from typing import Any

import sqlite3

from app import jackside_multi_issue as multi_issue
from app.jackside_flexible_labels import normalize_builtin_perfect_labels
from app.services import daily_414_final as final_service
from app.services import jackside_copy as copy_service
from app.services import jackside_issues as issue_service


_PREVIOUS_EFFECTIVE_SCHEDULE = issue_service.effective_campaign_schedule
_PREVIOUS_VALIDATE_ISSUE = issue_service.validate_issue_for_publish
_PREVIOUS_ENSURE_DEFAULT_RULES = issue_service.ensure_default_rules
_PREVIOUS_RESCHEDULE = multi_issue.reschedule_future_issue
_PREVIOUS_SEED_FINALISTS = final_service.seed_finalists
_PREVIOUS_RESULT_COPY = copy_service.result_copy_for_score


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


def ensure_default_rules_compat(conn: sqlite3.Connection) -> sqlite3.Row:
    """Backward-compatible entry point; policy lives in jackside_multi_runtime."""
    return _PREVIOUS_ENSURE_DEFAULT_RULES(conn)


def validate_issue_for_publish_compat(
    conn: sqlite3.Connection,
    issue: sqlite3.Row | dict[str, Any],
) -> list[str]:
    """Compatibility wrapper around the canonical JACKSIDE validation policy."""
    normalize_builtin_perfect_labels(conn)
    _PREVIOUS_ENSURE_DEFAULT_RULES(conn)
    current = issue
    try:
        issue_id = int(issue["id"])
    except (KeyError, IndexError, TypeError, ValueError):
        issue_id = 0
    if issue_id:
        refreshed = issue_service.get_issue(conn, issue_id)
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


def seed_finalists_compat(
    conn: sqlite3.Connection,
    *,
    final_table: sqlite3.Row,
) -> None:
    """Backward-compatible name for the canonical final seeding function."""
    _PREVIOUS_SEED_FINALISTS(conn, final_table=final_table)


def result_copy_for_score_compat(
    correct_count: int,
    *,
    final_eligible: bool = True,
) -> dict[str, str]:
    """Backward-compatible name for the centralized JACKSIDE result copy."""
    return _PREVIOUS_RESULT_COPY(correct_count, final_eligible=final_eligible)


# Keep only compatibility that still has a separate transport/runtime purpose.
# Rules, finalist selection and result copy now live in their canonical modules.
issue_service.effective_campaign_schedule = effective_campaign_schedule_compat
issue_service.validate_issue_for_publish = validate_issue_for_publish_compat
multi_issue.reschedule_future_issue = reschedule_future_issue_compat


__all__ = [
    "effective_campaign_schedule_compat",
    "ensure_default_rules_compat",
    "reschedule_future_issue_compat",
    "result_copy_for_score_compat",
    "seed_finalists_compat",
    "validate_issue_for_publish_compat",
]
