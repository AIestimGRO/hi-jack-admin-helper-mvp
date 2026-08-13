from __future__ import annotations

from datetime import date, datetime

import sqlite3

from app import jackside_multi_issue as multi_issue
from app.services import jackside_issues as issue_service


_ORIGINAL_RESCHEDULE = multi_issue.reschedule_future_issue


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def reschedule_with_snapshot_cleanup(
    conn: sqlite3.Connection,
    *,
    issue_id: int,
    issue_date_value: date,
    starts_at: datetime,
    title: str | None,
    timezone_name: str,
) -> sqlite3.Row:
    before = issue_service.get_issue(conn, int(issue_id))
    old_day = str(before["issue_date"]) if before else ""
    updated = _ORIGINAL_RESCHEDULE(
        conn,
        issue_id=issue_id,
        issue_date_value=issue_date_value,
        starts_at=starts_at,
        title=title,
        timezone_name=timezone_name,
    )
    new_day = issue_date_value.isoformat()
    if (
        old_day
        and old_day != new_day
        and _table_exists(conn, "jackcoin_economy_snapshots")
    ):
        remaining = conn.execute(
            """
            SELECT 1 FROM jackside_issues
            WHERE issue_date=? AND id<>? AND status<>'cancelled'
            LIMIT 1
            """,
            (old_day, int(issue_id)),
        ).fetchone()
        if not remaining:
            conn.execute(
                """
                DELETE FROM jackcoin_economy_snapshots
                WHERE entity_type='jackside_day' AND entity_id=?
                """,
                (old_day,),
            )
    return updated


multi_issue.reschedule_future_issue = reschedule_with_snapshot_cleanup


__all__ = ["reschedule_with_snapshot_cleanup"]
