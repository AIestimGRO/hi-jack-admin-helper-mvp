from pathlib import Path

import pytest

from app.db import connect, init_db, transaction
from app.services.clients import upsert_client
from app.telegram_notifications import (
    ensure_telegram_notification_schema,
    queue_manual_campaign,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "telegram-notifications.sqlite3"
    init_db(path)
    with transaction(path) as conn:
        ensure_telegram_notification_schema(conn)
    return path


def _create_campaign(conn, *, category: str = "club_updates") -> int:
    cursor = conn.execute(
        """
        INSERT INTO telegram_notification_campaigns(
            title,category,message_text,audience_type
        ) VALUES ('Test',?,'Hello','all')
        """,
        (category,),
    )
    return int(cursor.lastrowid)


def test_linking_telegram_enables_notifications_by_default(db_path):
    with transaction(db_path) as conn:
        client_id, _ = upsert_client(
            conn,
            {
                "app_user_id": "tg-default-1",
                "first_name": "Alex",
                "phone_raw": "9991234567",
            },
        )
        assert conn.execute(
            "SELECT 1 FROM telegram_notification_preferences WHERE client_id=?",
            (client_id,),
        ).fetchone() is None

        conn.execute(
            "UPDATE clients SET telegram_user_id=? WHERE id=?",
            ("123456789", client_id),
        )
        row = conn.execute(
            """
            SELECT notifications_enabled,tournaments_enabled,jackside_enabled,
                   rewards_enabled,club_updates_enabled,marketing_enabled
            FROM telegram_notification_preferences
            WHERE client_id=?
            """,
            (client_id,),
        ).fetchone()

    assert tuple(row) == (1, 1, 1, 1, 1, 1)


def test_linking_legacy_telegram_id_enables_notifications_by_default(db_path):
    with transaction(db_path) as conn:
        client_id, _ = upsert_client(
            conn,
            {
                "app_user_id": "tg-legacy-default-1",
                "first_name": "Legacy",
                "phone_raw": "9991234571",
            },
        )
        conn.execute(
            "UPDATE clients SET telegram_id=? WHERE id=?",
            ("987654321", client_id),
        )
        row = conn.execute(
            """
            SELECT notifications_enabled,tournaments_enabled,jackside_enabled,
                   rewards_enabled,club_updates_enabled,marketing_enabled
            FROM telegram_notification_preferences
            WHERE client_id=?
            """,
            (client_id,),
        ).fetchone()

    assert tuple(row) == (1, 1, 1, 1, 1, 1)


def test_relinking_telegram_restores_default_subscription(db_path):
    with transaction(db_path) as conn:
        client_id, _ = upsert_client(
            conn,
            {
                "app_user_id": "tg-relink-1",
                "first_name": "Nina",
                "phone_raw": "9991234568",
            },
        )
        conn.execute(
            "UPDATE clients SET telegram_user_id=? WHERE id=?",
            ("222222", client_id),
        )
        conn.execute(
            """
            UPDATE telegram_notification_preferences
            SET notifications_enabled=0,tournaments_enabled=0,
                jackside_enabled=0,rewards_enabled=0,
                club_updates_enabled=0,marketing_enabled=0,
                unsubscribed_at=CURRENT_TIMESTAMP
            WHERE client_id=?
            """,
            (client_id,),
        )
        conn.execute(
            "UPDATE clients SET telegram_user_id=NULL WHERE id=?",
            (client_id,),
        )
        conn.execute(
            "UPDATE clients SET telegram_user_id=? WHERE id=?",
            ("333333", client_id),
        )
        row = conn.execute(
            """
            SELECT notifications_enabled,tournaments_enabled,jackside_enabled,
                   rewards_enabled,club_updates_enabled,marketing_enabled,
                   unsubscribed_at
            FROM telegram_notification_preferences
            WHERE client_id=?
            """,
            (client_id,),
        ).fetchone()

    assert tuple(row[:6]) == (1, 1, 1, 1, 1, 1)
    assert row["unsubscribed_at"] is None


def test_manual_campaign_queue_is_idempotent_and_skips_opted_out_users(db_path):
    with transaction(db_path) as conn:
        first_id, _ = upsert_client(
            conn,
            {
                "app_user_id": "tg-queue-1",
                "first_name": "First",
                "phone_raw": "9991234569",
            },
        )
        second_id, _ = upsert_client(
            conn,
            {
                "app_user_id": "tg-queue-2",
                "first_name": "Second",
                "phone_raw": "9991234570",
            },
        )
        conn.execute(
            "UPDATE clients SET telegram_user_id='444444' WHERE id=?",
            (first_id,),
        )
        conn.execute(
            "UPDATE clients SET telegram_user_id='555555' WHERE id=?",
            (second_id,),
        )
        conn.execute(
            """
            UPDATE telegram_notification_preferences
            SET notifications_enabled=0
            WHERE client_id=?
            """,
            (second_id,),
        )
        campaign_id = _create_campaign(conn)

        assert queue_manual_campaign(conn, campaign_id=campaign_id) == 1
        assert queue_manual_campaign(conn, campaign_id=campaign_id) == 0

        rows = conn.execute(
            """
            SELECT client_id,telegram_chat_id,idempotency_key,status
            FROM telegram_notification_outbox
            WHERE campaign_id=?
            """,
            (campaign_id,),
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["client_id"] == first_id
    assert rows[0]["telegram_chat_id"] == "444444"
    assert rows[0]["idempotency_key"] == f"manual:{campaign_id}:client:{first_id}"
    assert rows[0]["status"] == "queued"


def test_manual_campaign_queues_legacy_telegram_id(db_path):
    with transaction(db_path) as conn:
        client_id, _ = upsert_client(
            conn,
            {
                "app_user_id": "tg-legacy-queue-1",
                "first_name": "Legacy Queue",
                "phone_raw": "9991234572",
            },
        )
        conn.execute(
            "UPDATE clients SET telegram_id='666666' WHERE id=?",
            (client_id,),
        )
        campaign_id = _create_campaign(conn)

        assert queue_manual_campaign(conn, campaign_id=campaign_id) == 1
        row = conn.execute(
            """
            SELECT client_id,telegram_chat_id
            FROM telegram_notification_outbox
            WHERE campaign_id=?
            """,
            (campaign_id,),
        ).fetchone()

    assert row["client_id"] == client_id
    assert row["telegram_chat_id"] == "666666"


def test_manual_campaign_respects_category_opt_out(db_path):
    with transaction(db_path) as conn:
        client_id, _ = upsert_client(
            conn,
            {
                "app_user_id": "tg-category-opt-out-1",
                "first_name": "Category Opt Out",
                "phone_raw": "9991234573",
            },
        )
        conn.execute(
            "UPDATE clients SET telegram_user_id='777777' WHERE id=?",
            (client_id,),
        )
        conn.execute(
            """
            UPDATE telegram_notification_preferences
            SET tournaments_enabled=0
            WHERE client_id=?
            """,
            (client_id,),
        )
        campaign_id = _create_campaign(conn, category="tournaments")

        assert queue_manual_campaign(conn, campaign_id=campaign_id) == 0
        count = conn.execute(
            "SELECT COUNT(*) FROM telegram_notification_outbox WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()[0]

    assert count == 0


def test_foundation_starts_with_sending_disabled(db_path):
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT sending_enabled,default_notifications_enabled,
                   rate_limit_per_second
            FROM telegram_notification_settings
            WHERE id=1
            """
        ).fetchone()

    assert tuple(row) == (0, 1, 20)
