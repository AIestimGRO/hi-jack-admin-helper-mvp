from __future__ import annotations

import sqlite3

from app.services import jackside_engagement as engagement


def update_referral_activity_status(
    conn: sqlite3.Connection,
    *,
    invited_client_id: int,
    submission_id: int,
    timezone_name: str,
) -> sqlite3.Row | None:
    del submission_id
    progress = conn.execute(
        "SELECT * FROM referral_qualification_progress WHERE invited_client_id=?",
        (int(invited_client_id),),
    ).fetchone()
    if not progress:
        return None

    days = engagement._completed_issue_dates(
        conn,
        client_id=int(invited_client_id),
        timezone_name=timezone_name,
    )
    count = len(days)
    first = days[0].isoformat() if count >= 1 else None
    second = days[1].isoformat() if count >= 2 else None
    active_date = days[2].isoformat() if count >= engagement.QUALIFY_DAYS else None
    newly_active = count >= engagement.QUALIFY_DAYS and not progress["qualified_at"]

    conn.execute(
        """
        UPDATE referral_qualification_progress
        SET distinct_completed_days=?, first_completed_date=?, second_completed_date=?,
            qualified_date=COALESCE(qualified_date, ?),
            qualified_at=CASE WHEN qualified_at IS NULL AND ? IS NOT NULL
                              THEN CURRENT_TIMESTAMP ELSE qualified_at END,
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (count, first, second, active_date, active_date, int(progress["id"])),
    )

    refreshed = conn.execute(
        "SELECT * FROM referral_qualification_progress WHERE id=?",
        (int(progress["id"]),),
    ).fetchone()
    if newly_active and refreshed:
        engagement._notify(
            conn,
            client_id=int(refreshed["referrer_client_id"]),
            notification_type="referral_qualified",
            title="Реферал активен",
            body="Приглашённый игрок завершил JACKSIDE в три разные даты.",
            entity_type="referral_progress",
            entity_id=int(refreshed["id"]),
        )
        engagement._notify(
            conn,
            client_id=int(refreshed["invited_client_id"]),
            notification_type="referral_qualified_invited",
            title="Статус активного реферала получен",
            body="Ты завершил JACKSIDE в три разные даты.",
            entity_type="referral_progress",
            entity_id=int(refreshed["id"]),
        )

    engagement.refresh_member_engagement(
        conn,
        client_id=int(invited_client_id),
        timezone_name=timezone_name,
    )
    if newly_active:
        engagement.refresh_member_engagement(
            conn,
            client_id=int(progress["referrer_client_id"]),
            timezone_name=timezone_name,
        )
    return conn.execute(
        "SELECT * FROM referral_qualification_progress WHERE id=?",
        (int(progress["id"]),),
    ).fetchone()


def apply_referral_status_policy() -> None:
    engagement.process_referral_qualification = update_referral_activity_status
