from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.db import connect, init_db, transaction
from app.member_performance import _decorate_collection, _display_name
from app.services.clients import upsert_client
from app.telegram_notifications import ensure_telegram_notification_schema
from app.telegram_safety_hotfix import safe_audience_count, safe_queue_manual_campaign
from app.telegram_transport import dispatch_telegram_outbox_once


def _telegram_db(tmp_path: Path) -> Path:
    path = tmp_path / "performance-hotfix.sqlite3"
    init_db(path)
    with transaction(path) as conn:
        ensure_telegram_notification_schema(conn)
    return path


def _linked_client(conn, suffix: str, telegram_id: str) -> int:
    client_id, _ = upsert_client(
        conn,
        {
            "app_user_id": f"performance-{suffix}",
            "first_name": "Performance",
            "phone_raw": f"99933{suffix.zfill(5)}"[-10:],
        },
    )
    conn.execute(
        "UPDATE clients SET telegram_user_id=? WHERE id=?",
        (telegram_id, client_id),
    )
    return client_id


def test_display_name_prefers_nickname_over_first_name() -> None:
    member = {
        "nickname": "BUNNY BLESS",
        "first_name": "Alex",
        "username": "MIMalex",
    }
    assert _display_name(member) == "BUNNY BLESS"


def test_collection_is_server_prioritized_like_final_client_view() -> None:
    payload = {
        "active_count": 2,
        "total_count": 3,
        "items": [
            {"name": "Unlocked", "state": "active", "kind": "title", "selected": False},
            {"name": "Locked", "state": "locked", "kind": "achievement", "selected": False},
            {"name": "Selected", "state": "active", "kind": "title", "selected": True},
        ],
    }
    result = _decorate_collection(payload)
    assert [item["name"] for item in result["items"]] == [
        "Selected",
        "Unlocked",
        "Locked",
    ]
    assert result["unlocked_count"] == 2
    assert result["items"][2]["subtitle"] == "Не открыто · достижение"


def test_fast_member_templates_compile() -> None:
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
    assert templates.get_template("member_profile_fast.html") is not None
    assert templates.get_template("member_vault_fast.html") is not None
    assert templates.get_template("_vault_catalog_cards_fast.html") is not None


def test_future_scheduled_campaign_cannot_be_manually_queued(tmp_path: Path) -> None:
    db_path = _telegram_db(tmp_path)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with transaction(db_path) as conn:
        client_id = _linked_client(conn, "1", "234826011")
        campaign_id = int(
            conn.execute(
                """
                INSERT INTO telegram_notification_campaigns(
                    title,category,message_text,audience_type,audience_value,status,scheduled_at
                ) VALUES ('Future','club_updates','Later','client',?,'draft',?)
                """,
                (str(client_id), future),
            ).lastrowid
        )
        with pytest.raises(ValueError, match="Планировщик"):
            safe_queue_manual_campaign(conn, campaign_id=campaign_id)

    with connect(db_path) as conn:
        status = conn.execute(
            "SELECT status FROM telegram_notification_campaigns WHERE id=?",
            (campaign_id,),
        ).fetchone()[0]
        outbox = conn.execute(
            "SELECT COUNT(*) FROM telegram_notification_outbox WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()[0]
    assert status == "draft"
    assert outbox == 0


def test_invalid_oidc_subject_is_excluded_before_outbox(tmp_path: Path) -> None:
    db_path = _telegram_db(tmp_path)
    with transaction(db_path) as conn:
        valid_id = _linked_client(conn, "2", "234826011")
        _linked_client(conn, "3", "84672257577528328580")
        campaign_id = int(
            conn.execute(
                """
                INSERT INTO telegram_notification_campaigns(
                    title,category,message_text,audience_type,status
                ) VALUES ('All','club_updates','Hello','all','draft')
                """
            ).lastrowid
        )
        assert safe_audience_count(conn, "club_updates") == 1
        assert safe_queue_manual_campaign(conn, campaign_id=campaign_id) == 1

    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT client_id,telegram_chat_id FROM telegram_notification_outbox WHERE campaign_id=?",
            (campaign_id,),
        ).fetchall()
    assert [(row["client_id"], row["telegram_chat_id"]) for row in rows] == [
        (valid_id, "234826011")
    ]


def test_dispatch_honors_configured_rate_with_injected_clock(tmp_path: Path) -> None:
    db_path = _telegram_db(tmp_path)
    with transaction(db_path) as conn:
        _linked_client(conn, "4", "234826011")
        _linked_client(conn, "5", "234826012")
        campaign_id = int(
            conn.execute(
                """
                INSERT INTO telegram_notification_campaigns(
                    title,category,message_text,audience_type,status
                ) VALUES ('Throttle','club_updates','Hello','all','draft')
                """
            ).lastrowid
        )
        assert safe_queue_manual_campaign(conn, campaign_id=campaign_id) == 2
        conn.execute(
            """
            UPDATE telegram_notification_settings
            SET sending_enabled=1,rate_limit_per_second=2
            WHERE id=1
            """
        )

    settings = SimpleNamespace(
        db_path=db_path,
        telegram_notifications_enabled=True,
        telegram_bot_token="token",
        telegram_transport_timeout_seconds=1.0,
    )
    current = [100.0]
    sleeps: list[float] = []

    def clock() -> float:
        return current[0]

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        current[0] += seconds

    sent: list[str] = []

    def sender(token, chat_id, payload, timeout_seconds):
        sent.append(chat_id)
        return {"message_id": len(sent)}

    result = dispatch_telegram_outbox_once(
        settings,
        limit=2,
        sender=sender,
        sleeper=sleeper,
        clock=clock,
    )

    assert result["sent"] == 2
    assert sent == ["234826011", "234826012"]
    assert sleeps == pytest.approx([0.5])
