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
    TELEGRAM_PRIVATE_USER_ID_MAX,
    TelegramPermanentError,
    TelegramRateLimitError,
    TelegramTransportError,
    _default_send_message,
    dispatch_telegram_outbox_once,
    validate_private_chat_id,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "telegram-transport.sqlite3"
    init_db(path)
    with transaction(path) as conn:
        ensure_telegram_notification_schema(conn)
    return path


def _settings(
    db_path: Path,
    *,
    enabled: bool = True,
    bot_token: str = "123456:test-token",
):
    return SimpleNamespace(
        db_path=db_path,
        telegram_notifications_enabled=enabled,
        telegram_bot_token=bot_token,
        telegram_transport_timeout_seconds=10.0,
    )


def _queued_campaign(
    db_path: Path,
    *,
    button_text: str | None = None,
    button_url: str | None = None,
) -> tuple[int, int]:
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
                title,category,message_text,audience_type,button_text,button_url
            ) VALUES ('Transport test','club_updates','Hello transport','all',?,?)
            """,
            (button_text, button_url),
        )
        campaign_id = int(cursor.lastrowid)
        assert queue_manual_campaign(conn, campaign_id=campaign_id) == 1
        conn.execute(
            "UPDATE telegram_notification_settings SET sending_enabled=1 WHERE id=1"
        )
    return campaign_id, client_id


def test_dispatch_is_blocked_by_environment_flag(db_path):
    _queued_campaign(db_path)
    called = False

    def sender(token, chat_id, payload, timeout):
        nonlocal called
        called = True
        return {"message_id": 1}

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


def test_dispatch_is_blocked_without_bot_token(db_path):
    _queued_campaign(db_path)

    result = dispatch_telegram_outbox_once(
        _settings(db_path, bot_token=""),
        sender=lambda *args: {"message_id": 1},
    )

    assert result["ok"] is False
    assert result["reason"] == "bot_token_missing"


def test_direct_dispatch_marks_message_sent(db_path):
    campaign_id, client_id = _queued_campaign(
        db_path,
        button_text="Open",
        button_url="https://club-v2.hijackpoker.ru/",
    )
    calls = []

    def sender(token, chat_id, payload, timeout):
        calls.append((token, chat_id, payload, timeout))
        return {"message_id": 777}

    result = dispatch_telegram_outbox_once(_settings(db_path), sender=sender)

    assert result == {
        "ok": True,
        "reason": "dispatched",
        "sent": 1,
        "failed": 0,
        "retrying": 0,
        "skipped": 0,
        "considered": 1,
    }
    assert len(calls) == 1
    token, chat_id, payload, timeout = calls[0]
    assert token == "123456:test-token"
    assert chat_id == "123456789"
    assert payload["text"] == "Hello transport"
    assert payload["button_text"] == "Open"
    assert payload["button_url"] == "https://club-v2.hijackpoker.ru/"
    assert timeout == 10.0

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

    assert client_id > 0
    assert outbox["status"] == "sent"
    assert outbox["attempts"] == 1
    assert outbox["last_error"] is None
    assert delivery["status"] == "sent"
    assert delivery["provider_message_id"] == "777"
    assert campaign_status == "sent"


def test_permanent_telegram_error_fails_without_retry(db_path):
    campaign_id, _ = _queued_campaign(db_path)

    def sender(token, chat_id, payload, timeout):
        raise TelegramPermanentError("Forbidden: bot was blocked by the user")

    result = dispatch_telegram_outbox_once(_settings(db_path), sender=sender)

    assert result["failed"] == 1
    assert result["retrying"] == 0
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT status,attempts,next_attempt_at,last_error
            FROM telegram_notification_outbox
            WHERE campaign_id=?
            """,
            (campaign_id,),
        ).fetchone()
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] is None
    assert "blocked" in row["last_error"]


def test_transient_telegram_error_is_retried(db_path):
    campaign_id, _ = _queued_campaign(db_path)

    def sender(token, chat_id, payload, timeout):
        raise TelegramTransportError("telegram_network_error")

    result = dispatch_telegram_outbox_once(_settings(db_path), sender=sender)

    assert result["failed"] == 0
    assert result["retrying"] == 1
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT status,attempts,next_attempt_at,last_error
            FROM telegram_notification_outbox
            WHERE campaign_id=?
            """,
            (campaign_id,),
        ).fetchone()
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] is not None
    assert row["last_error"] == "telegram_network_error"


def test_rate_limit_uses_retry_queue(db_path):
    campaign_id, _ = _queued_campaign(db_path)

    def sender(token, chat_id, payload, timeout):
        raise TelegramRateLimitError("Too Many Requests", retry_after=60)

    result = dispatch_telegram_outbox_once(_settings(db_path), sender=sender)

    assert result["retrying"] == 1
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT status,next_attempt_at,last_error
            FROM telegram_notification_outbox
            WHERE campaign_id=?
            """,
            (campaign_id,),
        ).fetchone()
    assert row["status"] == "queued"
    assert row["next_attempt_at"] is not None
    assert row["last_error"] == "Too Many Requests"


def test_private_chat_id_rejects_stale_oidc_subject_before_network():
    stale_subject = "1234123412341234123"

    with pytest.raises(
        TelegramPermanentError, match="telegram_private_chat_id_invalid"
    ):
        validate_private_chat_id(stale_subject)

    with pytest.raises(
        TelegramPermanentError, match="telegram_private_chat_id_invalid"
    ):
        _default_send_message(
            "123456:test-token",
            stale_subject,
            {"text": "must not leave the process"},
            0.1,
        )


def test_private_chat_id_accepts_documented_user_id_range():
    assert validate_private_chat_id("987654321") == "987654321"
    assert validate_private_chat_id(str(TELEGRAM_PRIVATE_USER_ID_MAX)) == str(
        TELEGRAM_PRIVATE_USER_ID_MAX
    )
    with pytest.raises(TelegramPermanentError):
        validate_private_chat_id(str(TELEGRAM_PRIVATE_USER_ID_MAX + 1))


def test_dispatch_honors_configured_rate_limit(db_path):
    with transaction(db_path) as conn:
        first_id, _ = upsert_client(
            conn,
            {
                "app_user_id": "transport-rate-1",
                "first_name": "Rate One",
                "phone_raw": "9995550102",
            },
        )
        second_id, _ = upsert_client(
            conn,
            {
                "app_user_id": "transport-rate-2",
                "first_name": "Rate Two",
                "phone_raw": "9995550103",
            },
        )
        conn.execute(
            "UPDATE clients SET telegram_user_id='234826011' WHERE id=?",
            (first_id,),
        )
        conn.execute(
            "UPDATE clients SET telegram_user_id='234826012' WHERE id=?",
            (second_id,),
        )
        campaign_id = int(
            conn.execute(
                """
                INSERT INTO telegram_notification_campaigns(
                    title,category,message_text,audience_type
                ) VALUES ('Rate test','club_updates','Hello rate','all')
                """
            ).lastrowid
        )
        assert queue_manual_campaign(conn, campaign_id=campaign_id) == 2
        conn.execute(
            """
            UPDATE telegram_notification_settings
            SET sending_enabled=1,rate_limit_per_second=2
            WHERE id=1
            """
        )

    current = [100.0]
    sleeps: list[float] = []
    sent: list[str] = []

    def clock() -> float:
        return current[0]

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        current[0] += seconds

    def sender(token, chat_id, payload, timeout):
        sent.append(chat_id)
        return {"message_id": len(sent)}

    result = dispatch_telegram_outbox_once(
        _settings(db_path),
        limit=2,
        sender=sender,
        sleeper=sleeper,
        clock=clock,
    )

    assert result["sent"] == 2
    assert sent == ["234826011", "234826012"]
    assert sleeps == pytest.approx([0.5])
