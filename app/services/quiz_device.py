from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any


DEVICE_COOKIE_NAME = "hjc_quiz_device"
DEVICE_MAX_AGE_SECONDS = 10 * 365 * 24 * 60 * 60


def device_token_hash(secret_key: str, token: str) -> str:
    return hmac.new(
        secret_key.encode("utf-8"),
        f"quiz-device:{token}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def issue_or_refresh_device(
    conn: sqlite3.Connection,
    *,
    secret_key: str,
    client_id: int,
    current_token: str = "",
) -> str:
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(seconds=DEVICE_MAX_AGE_SECONDS)).isoformat(timespec="seconds")
    token = str(current_token or "").strip()
    if token:
        token_hash = device_token_hash(secret_key, token)
        row = conn.execute(
            """
            SELECT id, client_id FROM quiz_device_tokens
            WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?
            """,
            (token_hash, now.isoformat(timespec="seconds")),
        ).fetchone()
        if row and int(row["client_id"]) == int(client_id):
            conn.execute(
                "UPDATE quiz_device_tokens SET last_used_at=?, expires_at=? WHERE id=?",
                (now.isoformat(timespec="seconds"), expires_at, row["id"]),
            )
            return token
        if row:
            conn.execute(
                "UPDATE quiz_device_tokens SET revoked_at=CURRENT_TIMESTAMP WHERE id=?",
                (row["id"],),
            )

    token = secrets.token_urlsafe(32)
    conn.execute(
        """
        INSERT INTO quiz_device_tokens(token_hash, client_id, expires_at, last_used_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            device_token_hash(secret_key, token),
            int(client_id),
            expires_at,
            now.isoformat(timespec="seconds"),
        ),
    )
    return token


def remembered_client(
    conn: sqlite3.Connection,
    *,
    secret_key: str,
    token: str,
    touch: bool = False,
) -> sqlite3.Row | None:
    token = str(token or "").strip()
    if not token:
        return None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row = conn.execute(
        """
        SELECT
            qdt.id AS device_token_id,
            qdt.client_id,
            c.phone_raw,
            c.phone_local,
            c.username,
            c.first_name,
            c.nickname,
            c.email,
            c.telegram_user_id
        FROM quiz_device_tokens qdt
        JOIN clients c ON c.id=qdt.client_id
        WHERE qdt.token_hash=? AND qdt.revoked_at IS NULL AND qdt.expires_at>?
        """,
        (device_token_hash(secret_key, token), now),
    ).fetchone()
    if row and touch:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=DEVICE_MAX_AGE_SECONDS)
        ).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE quiz_device_tokens SET last_used_at=?, expires_at=? WHERE id=?",
            (now, expires_at, row["device_token_id"]),
        )
    return row


def forget_device(conn: sqlite3.Connection, *, secret_key: str, token: str) -> None:
    token = str(token or "").strip()
    if not token:
        return
    conn.execute(
        """
        UPDATE quiz_device_tokens SET revoked_at=CURRENT_TIMESTAMP
        WHERE token_hash=? AND revoked_at IS NULL
        """,
        (device_token_hash(secret_key, token),),
    )


def remembered_display_name(client: sqlite3.Row | dict[str, Any]) -> str:
    first_name = str(client["first_name"] or "").strip()
    if first_name:
        return first_name
    nickname = str(client["nickname"] or "").strip()
    if nickname:
        return nickname
    username = str(client["username"] or "").strip().lstrip("@")
    if username:
        return f"@{username}"
    return "участник Hi, Jack"
