from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import init_db, transaction
from app.product_shell import _ensure_product_shell_schema
from app.profile_experience import title_collection_payload


def _client(conn, username: str = "profile_player") -> int:
    cursor = conn.execute(
        "INSERT INTO clients(username, source) VALUES (?, 'test')",
        (username,),
    )
    return int(cursor.lastrowid)


def test_title_collection_lists_active_first_and_all_remaining_locked(tmp_path: Path) -> None:
    db_path = tmp_path / "profile-experience.sqlite3"
    init_db(db_path)
    _ensure_product_shell_schema(db_path)

    with transaction(db_path) as conn:
        client_id = _client(conn)
        active_definition = conn.execute(
            """
            SELECT id FROM title_definitions
            WHERE is_enabled=1 AND title_type='permanent'
            ORDER BY priority,id LIMIT 1
            """
        ).fetchone()
        assert active_definition is not None
        conn.execute(
            "INSERT INTO member_titles(client_id,title_definition_id,selected) VALUES (?,?,1)",
            (client_id, int(active_definition["id"])),
        )
        payload = title_collection_payload(conn, client_id=client_id)

    assert payload["active_count"] == 1
    assert payload["total_count"] > payload["active_count"]
    assert payload["items"][0]["state"] == "active"
    assert payload["items"][0]["selected"] is True
    assert all(item["state"] == "locked" for item in payload["items"][payload["active_count"] :])


def test_expired_temporary_title_returns_to_locked_collection(tmp_path: Path) -> None:
    db_path = tmp_path / "profile-expired-title.sqlite3"
    init_db(db_path)
    _ensure_product_shell_schema(db_path)
    now = datetime.now(timezone.utc)

    with transaction(db_path) as conn:
        client_id = _client(conn, "expired_player")
        definition = conn.execute(
            """
            SELECT id FROM title_definitions
            WHERE is_enabled=1 AND title_type='temporary'
            ORDER BY priority,id LIMIT 1
            """
        ).fetchone()
        assert definition is not None
        period_id = conn.execute(
            """
            INSERT INTO temporary_title_periods(title_definition_id,period_key,starts_at,ends_at)
            VALUES (?,?,?,?)
            """,
            (
                int(definition["id"]),
                "profile-expired-test",
                (now - timedelta(days=8)).isoformat(timespec="seconds"),
                (now - timedelta(days=1)).isoformat(timespec="seconds"),
            ),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO member_titles(client_id,title_definition_id,temporary_period_id,expires_at)
            VALUES (?,?,?,?)
            """,
            (
                client_id,
                int(definition["id"]),
                int(period_id),
                (now - timedelta(days=1)).isoformat(timespec="seconds"),
            ),
        )
        payload = title_collection_payload(conn, client_id=client_id)

    matching = [
        item for item in payload["items"]
        if item["kind"] == "title" and item["definition_id"] == int(definition["id"])
    ]
    assert len(matching) == 1
    assert matching[0]["state"] == "locked"
    assert matching[0]["temporary"] is True
