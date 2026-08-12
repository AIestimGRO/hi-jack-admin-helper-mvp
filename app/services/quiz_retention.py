from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def cleanup_quiz_data(
    conn: sqlite3.Connection,
    *,
    detail_days: int,
    reward_days: int,
    action_log_days: int,
    force: bool = False,
) -> dict[str, int]:
    today = datetime.now(timezone.utc).date().isoformat()
    last = conn.execute("SELECT last_run_at FROM maintenance_log WHERE task='quiz_cleanup'").fetchone()
    if last and str(last[0])[:10] == today and not force:
        return {"skipped": 1}

    conn.execute(
        """
        UPDATE quiz_attempts SET status='expired', finished_at=CURRENT_TIMESTAMP, last_activity_at=CURRENT_TIMESTAMP
        WHERE status='in_progress' AND attempt_deadline_at IS NOT NULL AND attempt_deadline_at < ?
        """,
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
    )
    conn.execute(
        f"""
        UPDATE quiz_attempts SET status='expired', finished_at=CURRENT_TIMESTAMP, last_activity_at=CURRENT_TIMESTAMP
        WHERE status='in_progress' AND attempt_deadline_at IS NULL
          AND COALESCE(last_activity_at, created_at) < datetime('now', '-{int(detail_days)} days')
        """
    )
    conn.execute(
        "UPDATE quiz_reward_codes SET status='expired' WHERE status='issued' AND valid_until IS NOT NULL AND valid_until < ?",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
    )

    # Generic quiz details may be short-lived, but JACKSIDE submissions are the
    # immutable source for day/month/year ratings, streak history and final-table
    # records. Never remove issue-backed/daily_414 submissions through the generic
    # retention job. This also protects daily_414_finalists, whose submission FK
    # uses ON DELETE CASCADE.
    submissions = conn.execute(
        f"""
        DELETE FROM quiz_submissions
        WHERE created_at < datetime('now', '-{int(detail_days)} days')
          AND campaign_code NOT IN (
              SELECT code FROM quiz_campaigns WHERE campaign_type='daily_414'
          )
        """
    ).rowcount
    attempts = conn.execute(
        f"""
        DELETE FROM quiz_attempts
        WHERE status IN ('submitted', 'expired')
          AND created_at < datetime('now', '-{int(detail_days)} days')
          AND id NOT IN (SELECT attempt_id FROM quiz_submissions WHERE attempt_id IS NOT NULL)
        """
    ).rowcount
    rewards = conn.execute(
        f"""
        DELETE FROM quiz_reward_codes
        WHERE status IN ('used', 'expired', 'cancelled')
          AND created_at < datetime('now', '-{int(reward_days)} days')
          AND (valid_until IS NULL OR valid_until < datetime('now', '-{int(reward_days)} days'))
        """
    ).rowcount
    reward_events = conn.execute(
        f"DELETE FROM quiz_reward_events WHERE created_at < datetime('now', '-{int(action_log_days)} days')"
    ).rowcount
    email_codes = conn.execute(
        "DELETE FROM quiz_email_codes WHERE expires_at < datetime('now', '-1 day') OR used_at < datetime('now', '-1 day')"
    ).rowcount
    device_tokens = conn.execute(
        """
        DELETE FROM quiz_device_tokens
        WHERE datetime(expires_at)<CURRENT_TIMESTAMP
           OR (revoked_at IS NOT NULL AND revoked_at<datetime('now', '-31 days'))
        """
    ).rowcount
    member_email_codes = conn.execute(
        """
        DELETE FROM member_email_codes
        WHERE expires_at < datetime('now', '-1 day')
           OR used_at < datetime('now', '-1 day')
        """
    ).rowcount
    member_sessions = conn.execute(
        """
        DELETE FROM member_sessions
        WHERE datetime(expires_at)<CURRENT_TIMESTAMP
           OR (revoked_at IS NOT NULL AND revoked_at<datetime('now', '-31 days'))
        """
    ).rowcount
    conn.execute(
        """
        INSERT INTO maintenance_log(task, last_run_at) VALUES ('quiz_cleanup', CURRENT_TIMESTAMP)
        ON CONFLICT(task) DO UPDATE SET last_run_at=CURRENT_TIMESTAMP
        """
    )
    return {
        "submissions": submissions,
        "attempts": attempts,
        "rewards": rewards,
        "reward_events": reward_events,
        "email_codes": email_codes,
        "device_tokens": device_tokens,
        "member_email_codes": member_email_codes,
        "member_sessions": member_sessions,
    }
