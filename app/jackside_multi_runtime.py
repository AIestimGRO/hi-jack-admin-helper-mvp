from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import sqlite3

from app.services import jackside_issues as issue_service
from app.services.daily_414 import DAILY_414_FINAL_TABLE_DELAY_SECONDS


_ORIGINAL_EFFECTIVE_CAMPAIGN_SCHEDULE = issue_service.effective_campaign_schedule


def _final_window_end(
    conn: sqlite3.Connection,
    *,
    issue: sqlite3.Row | dict[str, Any],
    campaign_code: str,
) -> datetime | None:
    starts = issue_service._parse_dt(
        str(issue["starts_at"]) if issue["starts_at"] else None
    )
    if not starts:
        return None
    fallback = min(300, max(5, int(issue["final_question_time_seconds"] or 30)))
    rows = conn.execute(
        """
        SELECT time_limit_seconds
        FROM quiz_questions
        WHERE campaign_code=? AND game_round='final' AND IFNULL(is_active,1)=1
        ORDER BY position,id
        """,
        (campaign_code,),
    ).fetchall()
    durations: list[int] = []
    for row in rows:
        try:
            seconds = int(row["time_limit_seconds"] or fallback)
        except (TypeError, ValueError):
            seconds = fallback
        durations.append(min(300, max(5, seconds)))
    if not durations:
        durations = [fallback]
    return starts + timedelta(
        seconds=DAILY_414_FINAL_TABLE_DELAY_SECONDS + sum(durations)
    )


def _runtime_status(
    conn: sqlite3.Connection,
    *,
    issue: sqlite3.Row | dict[str, Any],
    campaign: sqlite3.Row | dict[str, Any],
    now: datetime,
    timezone_name: str,
) -> str:
    code = str(campaign["code"] or "")
    version = max(1, int(campaign["current_version"] or 1))
    final_table = conn.execute(
        """
        SELECT * FROM daily_414_final_tables
        WHERE campaign_code=? AND campaign_version=?
        """,
        (code, version),
    ).fetchone()
    runtime = issue_service.compute_issue_status(
        issue,
        now=now,
        final_table=final_table,
        timezone_name=timezone_name,
    )
    final_end = _final_window_end(
        conn, issue=issue, campaign_code=code
    )
    if final_end and issue_service._as_utc(now) >= final_end:
        if runtime in {"waiting_final", "final_live"}:
            return "closed"
    return runtime


def effective_campaign_schedule_multi(
    conn: sqlite3.Connection,
    campaign: sqlite3.Row | dict[str, Any],
    *,
    timezone_name: str = "Europe/Moscow",
) -> dict[str, Any]:
    """Hide a finished release from member campaign lists without mutating history."""
    payload = _ORIGINAL_EFFECTIVE_CAMPAIGN_SCHEDULE(
        conn, campaign, timezone_name=timezone_name
    )
    if str(payload.get("campaign_type") or "") != "daily_414":
        return payload
    code = str(payload.get("code") or "")
    if not code:
        return payload
    issue = issue_service.get_issue_by_campaign(conn, code)
    if not issue:
        return payload
    runtime = _runtime_status(
        conn,
        issue=issue,
        campaign=payload,
        now=datetime.now(timezone.utc),
        timezone_name=timezone_name,
    )
    if runtime in {"closed", "cancelled", "technical_review", "draft"}:
        payload["is_active"] = 0
    return payload


def _featured_key(
    status: str,
    starts_at: datetime | None,
    now: datetime,
    issue_id: int,
) -> tuple[int, float, float, int]:
    priority = {
        "main_live": 0,
        "final_live": 0,
        "waiting_final": 1,
        "lobby": 2,
        "scheduled": 3,
    }.get(status, 50)
    if starts_at is None:
        return (priority, float("inf"), float("inf"), issue_id)
    return (
        priority,
        abs((starts_at - now).total_seconds()),
        starts_at.timestamp(),
        issue_id,
    )


def current_featured_issue_runtime(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    timezone_name: str = "Europe/Moscow",
) -> dict[str, Any] | None:
    """Choose the running release, otherwise the nearest next release."""
    now_utc = issue_service._as_utc(now)
    candidates: list[tuple[tuple[int, float, float, int], dict[str, Any]]] = []
    for issue in issue_service.list_issues(conn, limit=100):
        if str(issue["status"] or "") == "draft":
            continue
        campaign = conn.execute(
            "SELECT * FROM quiz_campaigns WHERE code=?",
            (issue["campaign_code"],),
        ).fetchone()
        if not campaign or not int(campaign["is_active"] or 0):
            continue
        status = _runtime_status(
            conn,
            issue=issue,
            campaign=campaign,
            now=now_utc,
            timezone_name=timezone_name,
        )
        if status not in {
            "main_live",
            "final_live",
            "waiting_final",
            "lobby",
            "scheduled",
        }:
            continue
        starts = issue_service._parse_dt(
            str(issue["starts_at"]) if issue["starts_at"] else None
        )
        payload = issue_service.resolve_issue_for_campaign(
            conn,
            campaign,
            now=now_utc,
            timezone_name=timezone_name,
        )
        payload["status"] = status
        candidates.append(
            (_featured_key(status, starts, now_utc, int(issue["id"])), payload)
        )
    if candidates:
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    legacy: list[tuple[tuple[int, float, float, int], dict[str, Any]]] = []
    rows = conn.execute(
        """
        SELECT * FROM quiz_campaigns
        WHERE campaign_type='daily_414' AND is_active=1
          AND archived_at IS NULL AND deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM jackside_issues ji WHERE ji.campaign_code=quiz_campaigns.code
          )
        """
    ).fetchall()
    for campaign in rows:
        payload = issue_service.legacy_issue_from_campaign(
            conn, campaign, now=now_utc, timezone_name=timezone_name
        )
        status = str(payload.get("status") or "")
        if status not in {
            "main_live",
            "final_live",
            "waiting_final",
            "lobby",
            "scheduled",
        }:
            continue
        starts = None
        raw = payload.get("starts_at")
        if raw:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            starts = parsed.astimezone(timezone.utc)
        legacy.append(
            (
                _featured_key(status, starts, now_utc, int(campaign["id"])),
                payload,
            )
        )
    if legacy:
        legacy.sort(key=lambda item: item[0])
        return legacy[0][1]
    return None


def apply_runtime_overrides() -> None:
    issue_service.effective_campaign_schedule = effective_campaign_schedule_multi
    issue_service.current_featured_issue = current_featured_issue_runtime


apply_runtime_overrides()

# The admin IA templates use a lightweight version token for these assets.
# Bump it in one place so browsers do not retain the pre-multi-release JS.
try:
    from app import admin_information_architecture as admin_ia

    admin_ia.ASSET_VERSION = "admin-ia-v4"
except ImportError:
    pass


__all__ = [
    "apply_runtime_overrides",
    "current_featured_issue_runtime",
    "effective_campaign_schedule_multi",
]
