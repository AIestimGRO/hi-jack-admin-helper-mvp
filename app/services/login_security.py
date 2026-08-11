from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Literal


PrincipalType = Literal["member", "admin"]

FAILURE_WINDOW_MINUTES = 15
FAILURES_BEFORE_LOCK = 5
MEMBER_LOCK_MINUTES = (15, 30, 60)
ADMIN_LOCK_MINUTES = (30, 60, 120)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _policy(principal_type: PrincipalType) -> tuple[int, ...]:
    return ADMIN_LOCK_MINUTES if principal_type == "admin" else MEMBER_LOCK_MINUTES


def ensure_login_security_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_login_state (
            principal_type TEXT NOT NULL CHECK(principal_type IN ('member','admin')),
            principal_id INTEGER NOT NULL,
            failed_count INTEGER NOT NULL DEFAULT 0,
            window_started_at TEXT,
            locked_until TEXT,
            lock_level INTEGER NOT NULL DEFAULT 0,
            last_failed_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(principal_type, principal_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_security_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            principal_type TEXT NOT NULL CHECK(principal_type IN ('member','admin')),
            principal_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_auth_security_events_principal
        ON auth_security_events(principal_type, principal_id, created_at DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_auth_security_events_created
        ON auth_security_events(created_at DESC)
        """
    )


def _event(
    conn: sqlite3.Connection,
    *,
    principal_type: PrincipalType,
    principal_id: int,
    action: str,
    details: dict[str, object] | None = None,
) -> None:
    ensure_login_security_schema(conn)
    conn.execute(
        """
        INSERT INTO auth_security_events(principal_type,principal_id,action,details)
        VALUES (?,?,?,?)
        """,
        (
            principal_type,
            int(principal_id),
            str(action),
            json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
        ),
    )


def login_is_locked(
    conn: sqlite3.Connection,
    *,
    principal_type: PrincipalType,
    principal_id: int,
    now: datetime | None = None,
) -> bool:
    ensure_login_security_schema(conn)
    current = (now or _now()).astimezone(timezone.utc)
    row = conn.execute(
        """
        SELECT * FROM auth_login_state
        WHERE principal_type=? AND principal_id=?
        """,
        (principal_type, int(principal_id)),
    ).fetchone()
    if not row:
        return False
    locked_until = _parse(row["locked_until"])
    if locked_until and locked_until > current:
        _event(
            conn,
            principal_type=principal_type,
            principal_id=principal_id,
            action="login_blocked",
            details={"locked_until": _stamp(locked_until)},
        )
        return True
    if locked_until:
        conn.execute(
            """
            UPDATE auth_login_state
            SET failed_count=0,window_started_at=NULL,locked_until=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE principal_type=? AND principal_id=?
            """,
            (principal_type, int(principal_id)),
        )
    return False


def record_login_failure(
    conn: sqlite3.Connection,
    *,
    principal_type: PrincipalType,
    principal_id: int,
    now: datetime | None = None,
) -> int | None:
    ensure_login_security_schema(conn)
    current = (now or _now()).astimezone(timezone.utc)
    row = conn.execute(
        """
        SELECT * FROM auth_login_state
        WHERE principal_type=? AND principal_id=?
        """,
        (principal_type, int(principal_id)),
    ).fetchone()

    failed_count = 0
    lock_level = 0
    window_started = current
    if row:
        failed_count = int(row["failed_count"] or 0)
        lock_level = int(row["lock_level"] or 0)
        previous_window = _parse(row["window_started_at"])
        if previous_window and current - previous_window <= timedelta(
            minutes=FAILURE_WINDOW_MINUTES
        ):
            window_started = previous_window
        else:
            failed_count = 0

    failed_count += 1
    if failed_count >= FAILURES_BEFORE_LOCK:
        policy = _policy(principal_type)
        new_level = min(lock_level + 1, len(policy))
        lock_minutes = policy[new_level - 1]
        locked_until = current + timedelta(minutes=lock_minutes)
        conn.execute(
            """
            INSERT INTO auth_login_state(
                principal_type,principal_id,failed_count,window_started_at,
                locked_until,lock_level,last_failed_at,updated_at
            ) VALUES (?,?,0,NULL,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(principal_type,principal_id) DO UPDATE SET
                failed_count=0,
                window_started_at=NULL,
                locked_until=excluded.locked_until,
                lock_level=excluded.lock_level,
                last_failed_at=excluded.last_failed_at,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                principal_type,
                int(principal_id),
                _stamp(locked_until),
                new_level,
                _stamp(current),
            ),
        )
        _event(
            conn,
            principal_type=principal_type,
            principal_id=principal_id,
            action="login_locked",
            details={
                "lock_level": new_level,
                "lock_minutes": lock_minutes,
                "locked_until": _stamp(locked_until),
            },
        )
        return lock_minutes

    conn.execute(
        """
        INSERT INTO auth_login_state(
            principal_type,principal_id,failed_count,window_started_at,
            locked_until,lock_level,last_failed_at,updated_at
        ) VALUES (?,?,?,?,NULL,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(principal_type,principal_id) DO UPDATE SET
            failed_count=excluded.failed_count,
            window_started_at=excluded.window_started_at,
            locked_until=NULL,
            lock_level=excluded.lock_level,
            last_failed_at=excluded.last_failed_at,
            updated_at=CURRENT_TIMESTAMP
        """,
        (
            principal_type,
            int(principal_id),
            failed_count,
            _stamp(window_started),
            lock_level,
            _stamp(current),
        ),
    )
    _event(
        conn,
        principal_type=principal_type,
        principal_id=principal_id,
        action="login_failed",
        details={
            "failed_count": failed_count,
            "failure_limit": FAILURES_BEFORE_LOCK,
            "window_minutes": FAILURE_WINDOW_MINUTES,
        },
    )
    return None


def record_login_success(
    conn: sqlite3.Connection,
    *,
    principal_type: PrincipalType,
    principal_id: int,
) -> None:
    ensure_login_security_schema(conn)
    conn.execute(
        """
        INSERT INTO auth_login_state(
            principal_type,principal_id,failed_count,window_started_at,
            locked_until,lock_level,last_failed_at,updated_at
        ) VALUES (?,?,0,NULL,NULL,0,NULL,CURRENT_TIMESTAMP)
        ON CONFLICT(principal_type,principal_id) DO UPDATE SET
            failed_count=0,
            window_started_at=NULL,
            locked_until=NULL,
            lock_level=0,
            last_failed_at=NULL,
            updated_at=CURRENT_TIMESTAMP
        """,
        (principal_type, int(principal_id)),
    )
    _event(
        conn,
        principal_type=principal_type,
        principal_id=principal_id,
        action="login_success",
    )


def security_state(
    conn: sqlite3.Connection,
    *,
    principal_type: PrincipalType,
    principal_id: int,
) -> sqlite3.Row | None:
    ensure_login_security_schema(conn)
    return conn.execute(
        """
        SELECT * FROM auth_login_state
        WHERE principal_type=? AND principal_id=?
        """,
        (principal_type, int(principal_id)),
    ).fetchone()


__all__ = [
    "ADMIN_LOCK_MINUTES",
    "FAILURES_BEFORE_LOCK",
    "FAILURE_WINDOW_MINUTES",
    "MEMBER_LOCK_MINUTES",
    "ensure_login_security_schema",
    "login_is_locked",
    "record_login_failure",
    "record_login_success",
    "security_state",
]
