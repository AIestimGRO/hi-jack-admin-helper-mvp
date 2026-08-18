from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import connect, init_db, transaction
from app.services.clients import upsert_client
from app.telegram_notifications import ensure_telegram_notification_schema
from app.telegram_scheduler import (
    parse_moscow_schedule,
    release_due_scheduled_campaigns,
    run_scheduled_delivery_once,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "telegram-scheduler.sqlite3"
    init_db(path)
    with transaction(path) as conn:
        ensure_telegram_notification_schema(conn)
    return path


def _linked_client(conn, suffix: str, telegram_id: str = "123456789") -> int:
    client_id, _ = upsert_client(
        conn,
        {
            "app_user_id": f"scheduler-{suffix}",
            "first_name": "Scheduler",
            "phone_raw": f"99912{suffix.zfill(5)}"[-10:],
        },
    )
    conn.execute(
        "UPDATE clients SET telegram_user_id=? WHERE id=?",
        (telegram_id, client_id),
    )
    return client_id


def _scheduled_campaign(
    conn,
    *,
    scheduled_at: str,
    audience_value: str | None = None,
) -> int:
    audience_type = "client" if audience_value else "all"
    cursor = conn.execute(
        """
        INSERT INTO telegram_notification_campaigns(
            title,category,message_text,audience_type,audience_value,
            status,scheduled_at
        ) VALUES ('Scheduled','club_updates','Hello',?,?, 'draft',?)
        """,
        (audience_type, audience_value, scheduled_at),
    )
    return int(cursor.lastrowid)


def test_parse_moscow_schedule_converts_to_utc():
    result = parse_moscow_schedule(
        "2026-08-18T18:14",
        now_utc=datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc),
    )
    assert result == "2026-08-18 15:14:00"


def test_parse_moscow_schedule_rejects_past_time():
    with pytest.raises(ValueError, match="будущем"):
        parse_moscow_schedule(
            "2026-08-18T14:00",
            now_utc=datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc),
        )


def test_release_due_campaigns_only_releases_due_rows(db_path):
    with transaction(db_path) as conn:
        client_id = _linked_client(conn, "1")
        due_id = _scheduled_campaign(
            conn,
            scheduled_at="2026-08-18 12:00:00",
            audience_value=str(client_id),
        )
        future_id = _scheduled_campaign(
            conn,
            scheduled_at="2026-08-18 16:00:00",
            audience_value=str(client_id),
        )

    result = release_due_scheduled_campaigns(
        db_path,
        now_utc=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
    )

    assert result["campaigns_released"] == 1
    assert result["messages_queued"] == 1
    assert result["campaign_ids"] == [due_id]

    with connect(db_path) as conn:
        due_status = conn.execute(
            "SELECT status FROM telegram_notification_campaigns WHERE id=?",
            (due_id,),
        ).fetchone()[0]
        future_status = conn.execute(
            "SELECT status FROM telegram_notification_campaigns WHERE id=?",
            (future_id,),
        ).fetchone()[0]
        outbox = conn.execute(
            "SELECT client_id,status FROM telegram_notification_outbox WHERE campaign_id=?",
            (due_id,),
        ).fetchone()

    assert due_status == "queued"
    assert future_status == "draft"
    assert outbox["client_id"] == client_id
    assert outbox["status"] == "queued"

    second = release_due_scheduled_campaigns(
        db_path,
        now_utc=datetime(2026, 8, 18, 13, 1, tzinfo=timezone.utc),
    )
    assert second["campaigns_released"] == 0
    assert second["messages_queued"] == 0


def test_scheduler_gate_does_not_release_due_campaigns(db_path):
    with transaction(db_path) as conn:
        client_id = _linked_client(conn, "2")
        campaign_id = _scheduled_campaign(
            conn,
            scheduled_at="2026-08-18 12:00:00",
            audience_value=str(client_id),
        )

    settings = SimpleNamespace(
        db_path=db_path,
        telegram_notifications_enabled=False,
        telegram_bot_token="token",
        telegram_transport_timeout_seconds=1.0,
    )
    result = run_scheduled_delivery_once(
        settings,
        now_utc=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
    )

    assert result["ok"] is False
    assert result["reason"] == "environment_disabled"
    assert result["campaigns_released"] == 0

    with connect(db_path) as conn:
        status = conn.execute(
            "SELECT status FROM telegram_notification_campaigns WHERE id=?",
            (campaign_id,),
        ).fetchone()[0]
        outbox_count = conn.execute(
            "SELECT COUNT(*) FROM telegram_notification_outbox"
        ).fetchone()[0]

    assert status == "draft"
    assert outbox_count == 0


def test_ready_scheduler_releases_and_sends_one_campaign(db_path):
    with transaction(db_path) as conn:
        client_id = _linked_client(conn, "3", telegram_id="234826011")
        campaign_id = _scheduled_campaign(
            conn,
            scheduled_at="2026-08-18 12:00:00",
            audience_value=str(client_id),
        )
        conn.execute(
            "UPDATE telegram_notification_settings SET sending_enabled=1 WHERE id=1"
        )

    settings = SimpleNamespace(
        db_path=db_path,
        telegram_notifications_enabled=True,
        telegram_bot_token="token",
        telegram_transport_timeout_seconds=1.0,
    )
    calls = []

    def sender(token, chat_id, payload, timeout_seconds):
        calls.append((token, chat_id, payload, timeout_seconds))
        return {"message_id": 77}

    result = run_scheduled_delivery_once(
        settings,
        sender=sender,
        now_utc=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
    )

    assert result["ok"] is True
    assert result["campaigns_released"] == 1
    assert result["messages_queued"] == 1
    assert result["dispatch"]["sent"] == 1
    assert len(calls) == 1
    assert calls[0][1] == "234826011"

    with connect(db_path) as conn:
        campaign_status = conn.execute(
            "SELECT status FROM telegram_notification_campaigns WHERE id=?",
            (campaign_id,),
        ).fetchone()[0]
        outbox_status = conn.execute(
            "SELECT status FROM telegram_notification_outbox WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()[0]
        provider_id = conn.execute(
            """
            SELECT provider_message_id
            FROM telegram_notification_deliveries
            WHERE campaign_id=? AND status='sent'
            """,
            (campaign_id,),
        ).fetchone()[0]

    assert campaign_status == "sent"
    assert outbox_status == "sent"
    assert provider_id == "77"
