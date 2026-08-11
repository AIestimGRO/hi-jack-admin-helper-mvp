from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from app.services.auth import authenticate, hash_pin
from app.services.login_security import (
    ADMIN_LOCK_MINUTES,
    FAILURE_WINDOW_MINUTES,
    MEMBER_LOCK_MINUTES,
    login_is_locked,
    record_login_failure,
    record_login_success,
    security_state,
)
from app.services.member_accounts import authenticate_account, hash_password


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE member_accounts (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL,
            email_normalized TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            last_login_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE admins (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            pin_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            session_version INTEGER NOT NULL DEFAULT 1,
            last_login_at TEXT
        )
        """
    )
    return conn


def test_member_login_locks_after_five_failures_and_success_resets() -> None:
    conn = make_conn()
    conn.execute(
        """
        INSERT INTO member_accounts(id,email,email_normalized,password_hash)
        VALUES (1,'player@example.test','player@example.test',?)
        """,
        (hash_password("CorrectPassword2026"),),
    )

    for _ in range(5):
        assert (
            authenticate_account(
                conn,
                email="player@example.test",
                password="WrongPassword2026",
            )
            is None
        )

    state = security_state(conn, principal_type="member", principal_id=1)
    assert state is not None
    assert int(state["lock_level"]) == 1
    assert state["locked_until"]
    assert (
        authenticate_account(
            conn,
            email="player@example.test",
            password="CorrectPassword2026",
        )
        is None
    )

    conn.execute(
        "UPDATE auth_login_state SET locked_until='2000-01-01T00:00:00+00:00' WHERE principal_type='member' AND principal_id=1"
    )
    account = authenticate_account(
        conn,
        email="player@example.test",
        password="CorrectPassword2026",
    )
    assert account is not None
    state = security_state(conn, principal_type="member", principal_id=1)
    assert int(state["failed_count"]) == 0
    assert int(state["lock_level"]) == 0
    assert state["locked_until"] is None

    actions = [
        row["action"]
        for row in conn.execute(
            "SELECT action FROM auth_security_events WHERE principal_type='member' AND principal_id=1 ORDER BY id"
        ).fetchall()
    ]
    assert actions.count("login_failed") == 4
    assert "login_locked" in actions
    assert "login_blocked" in actions
    assert actions[-1] == "login_success"


def test_member_lock_escalates_15_30_60_minutes() -> None:
    conn = make_conn()
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)

    for expected_level, expected_minutes in enumerate(MEMBER_LOCK_MINUTES, start=1):
        for offset in range(5):
            result = record_login_failure(
                conn,
                principal_type="member",
                principal_id=7,
                now=now + timedelta(seconds=offset),
            )
        assert result == expected_minutes
        state = security_state(conn, principal_type="member", principal_id=7)
        assert int(state["lock_level"]) == expected_level
        locked_until = datetime.fromisoformat(str(state["locked_until"]))
        now = locked_until + timedelta(seconds=1)
        assert not login_is_locked(
            conn,
            principal_type="member",
            principal_id=7,
            now=now,
        )

    for offset in range(5):
        result = record_login_failure(
            conn,
            principal_type="member",
            principal_id=7,
            now=now + timedelta(seconds=offset),
        )
    assert result == MEMBER_LOCK_MINUTES[-1]
    state = security_state(conn, principal_type="member", principal_id=7)
    assert int(state["lock_level"]) == len(MEMBER_LOCK_MINUTES)


def test_failures_outside_window_do_not_accumulate() -> None:
    conn = make_conn()
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
    for index in range(4):
        record_login_failure(
            conn,
            principal_type="member",
            principal_id=9,
            now=now + timedelta(minutes=index),
        )
    record_login_failure(
        conn,
        principal_type="member",
        principal_id=9,
        now=now + timedelta(minutes=FAILURE_WINDOW_MINUTES + 1),
    )
    state = security_state(conn, principal_type="member", principal_id=9)
    assert int(state["failed_count"]) == 1
    assert state["locked_until"] is None


def test_admin_login_uses_stronger_lock_policy() -> None:
    conn = make_conn()
    conn.execute(
        """
        INSERT INTO admins(
            id,username,display_name,pin_hash,role,is_active,session_version
        ) VALUES (1,'master','Master',?,'master_admin',1,1)
        """,
        (hash_pin("2468"),),
    )

    for _ in range(5):
        assert authenticate(conn, "master", "9999") is None

    state = security_state(conn, principal_type="admin", principal_id=1)
    assert int(state["lock_level"]) == 1
    locked_until = datetime.fromisoformat(str(state["locked_until"]))
    created = conn.execute(
        """
        SELECT created_at FROM auth_security_events
        WHERE principal_type='admin' AND principal_id=1 AND action='login_locked'
        ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    assert created is not None
    created_at = datetime.fromisoformat(str(created["created_at"])).replace(
        tzinfo=timezone.utc
    )
    delta_minutes = (locked_until - created_at).total_seconds() / 60
    assert ADMIN_LOCK_MINUTES[0] - 1 <= delta_minutes <= ADMIN_LOCK_MINUTES[0] + 1
    assert authenticate(conn, "master", "2468") is None


def test_success_resets_escalation_level() -> None:
    conn = make_conn()
    now = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
    for offset in range(5):
        record_login_failure(
            conn,
            principal_type="member",
            principal_id=12,
            now=now + timedelta(seconds=offset),
        )
    conn.execute(
        "UPDATE auth_login_state SET locked_until='2000-01-01T00:00:00+00:00' WHERE principal_type='member' AND principal_id=12"
    )
    record_login_success(conn, principal_type="member", principal_id=12)
    state = security_state(conn, principal_type="member", principal_id=12)
    assert int(state["lock_level"]) == 0
    assert int(state["failed_count"]) == 0
