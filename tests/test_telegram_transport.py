from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import connect, init_db, transaction
from app.services.clients import upsert_client
from app.telegram_notifications import (
    ensure_telegram_notification_schema,
    queue_manual_campaign,
)
from app.telegram_transport import (
    apply_delivery_result,
    dispatch_telegram_outbox_once,
    sign_transport_body,
    verify_transport_signature,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "telegram-transport.sqlite3"
    init_db(path)
    with transaction(path) as conn:
        ensure_telegram_notification_schema(conn)
    return path


def _settings(db_path: Path, *, enabled: bool = True):
    return SimpleNamespace(
        db_path=db_path,
        telegram_notifications_enabled=enabled,
        telegram_transport_url="https://transport.example/internal/telegram/dispatch",
        telegram_bridge_secret="test-bridge-secret",
        telegram_transport_timeout_seconds=5.0,
    )


def _queued_campaign(db_path: Path) -> tuple[int, int]:
    with transaction(db_path) as conn:
        client_id, _ = upsert_client(
            conn,
            {
                "app_user_id": "transport-user-1",
                "first_name": "Transport",
                "phone_raw": "9995550101",
            },
        )
        conn.execute(
            "UPDATE clients SET telegram_user_id='123456789' WHERE id=?",
            (client_id,),
        )
        cursor = conn.execute(
            """
            INSERT INTO telegram_notification_campaigns(
                title,category,message_text,audience_type
            ) VALUES ('Transport test','club_updates','Hello transport','all')
            """
        )
        campaign_id = int(cursor.lastrowid)
        assert queue_manual_campaign(conn, campaign_id=campaign_id) == 1
        conn.execute(
            "UPDATE telegram_notification_settings SET sending_enabled=1 WHERE id=1"
        )
    return campaign_id, client_id


def test_signature_round_trip_and_expiration():
    body = b'{"hello":"world"}'
    timestamp = "1787040000"
    signature = sign_transport_body("shared-secret", timestamp, body)

    assert verify_transport_signature(
        "shared-secret",
        body,
        timestamp,
        signature,
        now_timestamp=1787040001,
    )
    assert not verify_transport_signature(
        "shared-secret",
        body,
        timestamp,
        signature,
        now_timestamp=1787040400,
    )
    assert not verify_transport_signature(
        "wrong-secret",
        body,
        timestamp,
        signature,
        now_timestamp=1787040001,
    )


def test_dispatch_is_blocked_by_environment_flag(db_path):
    _queued_campaign(db_path)
    called = False

    def sender(url, secret, payload, timeout):
        nonlocal called
        called = True
        return {"accepted": True, "task_id": "unexpected"}

    result = dispatch_telegram_outbox_once(
        _settings(db_path, enabled=False),
        sender=sender,
    )

    assert result["ok"] is False
    assert result["reason"] == "environment_disabled"
    assert called is False
    with connect(db_path) as conn:
        status = conn.execute(
            "SELECT status FROM telegram_notification_outbox"
        ).fetchone()[0]
    assert status == "queued"


def test_dispatch_acceptance_and_delivery_callback(db_path):
    campaign_id, client_id = _queued_campaign(db_path)
    envelopes = []

    def sender(url, secret, payload, timeout):
        envelopes.append((url, secret, payload, timeout))
        return {"accepted": True, "task_id": "celery-task-1"}

    result = dispatch_telegram_outbox_once(
        _settings(db_path),
        sender=sender,
    )

    assert result == {
        "ok": True,
        "reason": "dispatched",
        "accepted": 1,
        "failed": 0,
        "skipped": 0,
        "considered": 1,
    }
    assert len(envelopes) == 1
    _, secret, envelope, timeout = envelopes[0]
    assert secret == "test-bridge-secret"
    assert timeout == 5.0
    assert envelope["chat_id"] == "123456789"
    assert envelope["text"] == "Hello transport"
    assert envelope["idempotency_key"].startswith(f"manual:{campaign_id}:client:")

    with connect(db_path) as conn:
        outbox = conn.execute(
            "SELECT * FROM telegram_notification_outbox WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()
        delivery = conn.execute(
            """
            SELECT status,provider_message_id
            FROM telegram_notification_deliveries
            WHERE outbox_id=?
            """,
            (outbox["id"],),
        ).fetchone()
        campaign_status = conn.execute(
            "SELECT status FROM telegram_notification_campaigns WHERE id=?",
            (campaign_id,),
        ).fetchone()[0]

    assert outbox["status"] == "sending"
    assert outbox["attempts"] == 1
    assert delivery["status"] == "accepted"
    assert delivery["provider_message_id"] == "celery-task-1"
    assert campaign_status == "sending"

    callback = apply_delivery_result(
        db_path,
        {
            "outbox_id": outbox["id"],
            "idempotency_key": outbox["idempotency_key"],
            "status": "sent",
            "telegram_message_id": "777",
        },
    )

    assert callback["ok"] is True
    assert callback["duplicate"] is False
    with connect(db_path) as conn:
        final_outbox = conn.execute(
            "SELECT status,last_error FROM telegram_notification_outbox WHERE id=?",
            (outbox["id"],),
        ).fetchone()
        final_campaign = conn.execute(
            "SELECT status FROM telegram_notification_campaigns WHERE id=?",
            (campaign_id,),
        ).fetchone()[0]
        sent_delivery = conn.execute(
            """
            SELECT status,provider_message_id
            FROM telegram_notification_deliveries
            WHERE outbox_id=? AND status='sent'
            """,
            (outbox["id"],),
        ).fetchone()

    assert client_id > 0
    assert final_outbox["status"] == "sent"
    assert final_outbox["last_error"] is None
    assert final_campaign == "sent"
    assert sent_delivery["provider_message_id"] == "777"


def test_delivery_callback_is_idempotent(db_path):
    campaign_id, _ = _queued_campaign(db_path)

    def sender(url, secret, payload, timeout):
        return {"accepted": True, "task_id": "celery-task-2"}

    dispatch_telegram_outbox_once(_settings(db_path), sender=sender)
    with connect(db_path) as conn:
        outbox = conn.execute(
            "SELECT id,idempotency_key FROM telegram_notification_outbox WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()

    payload = {
        "outbox_id": outbox["id"],
        "idempotency_key": outbox["idempotency_key"],
        "status": "sent",
        "telegram_message_id": "888",
    }
    first = apply_delivery_result(db_path, payload)
    second = apply_delivery_result(db_path, payload)

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    with connect(db_path) as conn:
        sent_count = conn.execute(
            """
            SELECT COUNT(*) FROM telegram_notification_deliveries
            WHERE outbox_id=? AND status='sent'
            """,
            (outbox["id"],),
        ).fetchone()[0]
    assert sent_count == 1
