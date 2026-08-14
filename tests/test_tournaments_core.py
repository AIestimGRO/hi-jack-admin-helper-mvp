from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from app.tournaments_core import (
    cancel_client_registration,
    ensure_tournament_schema,
    register_client_for_tournament,
    tournament_payload,
)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE admins(id INTEGER PRIMARY KEY);
        CREATE TABLE clients(
            id INTEGER PRIMARY KEY,
            first_name TEXT,
            nickname TEXT,
            username TEXT,
            phone_local TEXT,
            app_user_id TEXT
        );
        CREATE TABLE member_accounts(
            id INTEGER PRIMARY KEY,
            client_id INTEGER NOT NULL REFERENCES clients(id)
        );
        """
    )
    return conn


def _add_tournament(conn: sqlite3.Connection, *, max_slots: int = 1, registration_open: int = 1) -> int:
    cursor = conn.execute(
        """
        INSERT INTO club_tournaments(
            title,starts_at,max_slots,registration_open,is_published,status
        ) VALUES ('Friday Main','2030-08-16T15:00:00+00:00',?,?,1,'scheduled')
        """,
        (max_slots, registration_open),
    )
    return int(cursor.lastrowid)


def test_schema_upgrade_is_additive_and_preserves_existing_tournament():
    conn = _db()
    conn.execute(
        """
        CREATE TABLE club_tournaments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            starts_at TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            format_text TEXT NOT NULL DEFAULT '',
            buy_in_text TEXT NOT NULL DEFAULT '',
            max_slots INTEGER NOT NULL DEFAULT 0,
            registration_open INTEGER NOT NULL DEFAULT 1,
            is_published INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'scheduled',
            created_by_admin_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "INSERT INTO club_tournaments(title,starts_at) VALUES ('Legacy','2030-01-01T12:00:00+00:00')"
    )

    ensure_tournament_schema(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(club_tournaments)")}
    assert {
        "stack_text",
        "levels_text",
        "reentry_text",
        "addon_text",
        "guarantee_text",
        "late_registration_text",
        "venue_text",
        "registration_opens_at",
        "registration_closes_at",
        "published_at",
    }.issubset(columns)
    assert conn.execute("SELECT title FROM club_tournaments").fetchone()[0] == "Legacy"
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='club_tournament_registrations'"
    ).fetchone()


def test_capacity_uses_waitlist_and_cancellation_promotes_first_waiting_player():
    conn = _db()
    ensure_tournament_schema(conn)
    conn.executemany(
        "INSERT INTO clients(id,first_name) VALUES (?,?)",
        [(1, "One"), (2, "Two")],
    )
    conn.executemany(
        "INSERT INTO member_accounts(id,client_id) VALUES (?,?)",
        [(11, 1), (22, 2)],
    )
    tournament_id = _add_tournament(conn, max_slots=1)
    now = datetime(2029, 1, 1, tzinfo=timezone.utc)

    first = register_client_for_tournament(
        conn,
        tournament_id=tournament_id,
        client_id=1,
        account_id=11,
        now=now,
    )
    second = register_client_for_tournament(
        conn,
        tournament_id=tournament_id,
        client_id=2,
        account_id=22,
        now=now,
    )
    assert first["status"] == "registered"
    assert second["status"] == "waitlist"

    cancel_client_registration(
        conn,
        tournament_id=tournament_id,
        client_id=1,
        now=now,
    )
    promoted = conn.execute(
        "SELECT status FROM club_tournament_registrations WHERE tournament_id=? AND client_id=2",
        (tournament_id,),
    ).fetchone()
    assert promoted["status"] == "registered"

    tournament = conn.execute(
        "SELECT * FROM club_tournaments WHERE id=?", (tournament_id,)
    ).fetchone()
    payload = tournament_payload(
        conn,
        tournament,
        client_id=2,
        timezone_name="Europe/Moscow",
        now=now,
    )
    assert payload["registered_count"] == 1
    assert payload["waitlist_count"] == 0
    assert payload["my_registration_status"] == "registered"
    assert payload["seats_left"] == 0


def test_registration_is_idempotent_and_respects_closed_registration():
    conn = _db()
    ensure_tournament_schema(conn)
    conn.execute("INSERT INTO clients(id,first_name) VALUES (1,'One')")
    conn.execute("INSERT INTO member_accounts(id,client_id) VALUES (11,1)")
    tournament_id = _add_tournament(conn, max_slots=5)
    now = datetime(2029, 1, 1, tzinfo=timezone.utc)

    first = register_client_for_tournament(
        conn,
        tournament_id=tournament_id,
        client_id=1,
        account_id=11,
        now=now,
    )
    second = register_client_for_tournament(
        conn,
        tournament_id=tournament_id,
        client_id=1,
        account_id=11,
        now=now,
    )
    assert first["id"] == second["id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM club_tournament_registrations WHERE tournament_id=?",
        (tournament_id,),
    ).fetchone()[0] == 1

    conn.execute(
        "UPDATE club_tournaments SET registration_open=0 WHERE id=?",
        (tournament_id,),
    )
    conn.execute(
        "UPDATE club_tournament_registrations SET status='cancelled' WHERE id=?",
        (int(first["id"]),),
    )
    with pytest.raises(ValueError, match="registration_closed"):
        register_client_for_tournament(
            conn,
            tournament_id=tournament_id,
            client_id=1,
            account_id=11,
            now=now,
        )
