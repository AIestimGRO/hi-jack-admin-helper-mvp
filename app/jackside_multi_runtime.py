from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import sqlite3

from app.services import jackside_issues as issue_service


_ORIGINAL_EFFECTIVE_CAMPAIGN_SCHEDULE = issue_service.effective_campaign_schedule


def effective_campaign_schedule_multi(
    conn: sqlite3.Connection,
    campaign: sqlite3.Row | dict[str, Any],
    *,
    timezone_name: str = "Europe/Moscow",
) -> dict[str, Any]:
    """Hide a finished issue from active/upcoming campaign lists without mutating history."""
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

    version = max(1, int(payload.get("current_version") or 1))
    final_table = conn.execute(
        """
        SELECT * FROM daily_414_final_tables
        WHERE campaign_code=? AND campaign_version=?
        """,
        (code, version),
    ).fetchone()
    runtime = issue_service.compute_issue_status(
        issue,
        now=datetime.now(timezone.utc),
        final_table=final_table,
        timezone_name=timezone_name,
    )
    if runtime in {"closed", "cancelled", "technical_review", "draft"}:
        payload["is_active"] = 0
    return payload


def apply_runtime_overrides() -> None:
    issue_service.effective_campaign_schedule = effective_campaign_schedule_multi


apply_runtime_overrides()

# The admin IA templates use a lightweight version token for these assets.
# Bump it in one place so browsers do not retain the pre-multi-release JS.
try:
    from app import admin_information_architecture as admin_ia

    admin_ia.ASSET_VERSION = "admin-ia-v4"
except ImportError:
    pass


__all__ = ["apply_runtime_overrides", "effective_campaign_schedule_multi"]
