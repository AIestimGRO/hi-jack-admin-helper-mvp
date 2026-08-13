from __future__ import annotations

from typing import Any

import sqlite3

from app.services import jackside_issues as issue_service


_PREVIOUS_EFFECTIVE_SCHEDULE = issue_service.effective_campaign_schedule


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


issue_service.effective_campaign_schedule = effective_campaign_schedule_compat


__all__ = ["effective_campaign_schedule_compat"]
