from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI

import app.telegram_notifications as telegram_notifications
import app.telegram_scheduler as telegram_scheduler
from app.telegram_transport import TelegramPermanentError, validate_private_chat_id


def _future_scheduled(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed > datetime.now(timezone.utc)


def _sendable_chat_id(value: Any) -> str | None:
    try:
        return validate_private_chat_id(str(value or ""))
    except TelegramPermanentError:
        return None


def safe_audience_count(
    conn: sqlite3.Connection,
    category: str = "club_updates",
) -> int:
    column = telegram_notifications._CATEGORY_COLUMNS.get(  # noqa: SLF001
        category,
        "club_updates_enabled",
    )
    rows = conn.execute(
        f"""
        SELECT COALESCE(
                   NULLIF(c.telegram_user_id,''),
                   NULLIF(c.telegram_id,'')
               ) AS telegram_chat_id
        FROM clients c
        JOIN telegram_notification_preferences p ON p.client_id=c.id
        WHERE (
                COALESCE(c.telegram_user_id,'')<>''
                OR COALESCE(c.telegram_id,'')<>''
              )
          AND p.notifications_enabled=1
          AND p.{column}=1
          AND COALESCE(c.client_status,'existing')<>'deleted'
        """
    ).fetchall()
    return sum(1 for row in rows if _sendable_chat_id(row["telegram_chat_id"]))


def safe_queue_manual_campaign(
    conn: sqlite3.Connection,
    *,
    campaign_id: int,
) -> int:
    campaign = conn.execute(
        "SELECT * FROM telegram_notification_campaigns WHERE id=?",
        (int(campaign_id),),
    ).fetchone()
    if not campaign:
        raise ValueError("Рассылка не найдена")
    if campaign["status"] not in {"draft", "queued", "failed"}:
        raise ValueError("Эту рассылку уже нельзя поставить в очередь")
    if _future_scheduled(campaign["scheduled_at"]):
        raise ValueError(
            "Эта рассылка запланирована на будущее. "
            "Планировщик поставит её в очередь автоматически в указанное время."
        )

    category = str(campaign["category"] or "club_updates")
    column = telegram_notifications._CATEGORY_COLUMNS.get(  # noqa: SLF001
        category,
        "club_updates_enabled",
    )
    audience_type = str(campaign["audience_type"] or "all")
    params: list[Any] = []
    extra = ""

    if audience_type == "client":
        try:
            client_id = int(str(campaign["audience_value"] or ""))
        except ValueError as exc:
            raise ValueError("Не выбран получатель") from exc
        extra = " AND c.id=?"
        params.append(client_id)
    elif audience_type != "all":
        raise ValueError("Этот сегмент аудитории ещё не поддерживается")

    rows = conn.execute(
        f"""
        SELECT c.id,
               COALESCE(
                   NULLIF(c.telegram_user_id,''),
                   NULLIF(c.telegram_id,'')
               ) AS telegram_chat_id
        FROM clients c
        JOIN telegram_notification_preferences p ON p.client_id=c.id
        WHERE (
                COALESCE(c.telegram_user_id,'')<>''
                OR COALESCE(c.telegram_id,'')<>''
              )
          AND p.notifications_enabled=1
          AND p.{column}=1
          AND COALESCE(c.client_status,'existing')<>'deleted'
          {extra}
        """,
        params,
    ).fetchall()

    payload = {
        "text": str(campaign["message_text"]),
        "category": category,
        "button_text": str(campaign["button_text"] or ""),
        "button_url": str(campaign["button_url"] or ""),
    }
    queued = 0
    for row in rows:
        chat_id = _sendable_chat_id(row["telegram_chat_id"])
        if not chat_id:
            continue
        key = f"manual:{campaign_id}:client:{int(row['id'])}"
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO telegram_notification_outbox(
                campaign_id,category,client_id,telegram_chat_id,
                payload_json,idempotency_key,status
            ) VALUES (?,?,?,?,?,?,'queued')
            """,
            (
                int(campaign_id),
                category,
                int(row["id"]),
                chat_id,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                key,
            ),
        )
        if cursor.rowcount:
            queued += 1

    current_status = str(campaign["status"] or "draft")
    if queued:
        status = "queued"
    elif current_status == "draft":
        status = "failed"
    else:
        status = current_status
    conn.execute(
        """
        UPDATE telegram_notification_campaigns
        SET status=?,updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (status, int(campaign_id)),
    )
    return queued


def install_telegram_safety_hotfix(app: FastAPI) -> FastAPI:
    if getattr(app.state, "telegram_safety_hotfix_installed", False):
        return app
    app.state.telegram_safety_hotfix_installed = True

    telegram_notifications._audience_count = safe_audience_count  # noqa: SLF001
    telegram_notifications.queue_manual_campaign = safe_queue_manual_campaign
    telegram_scheduler.queue_manual_campaign = safe_queue_manual_campaign
    return app


__all__ = [
    "install_telegram_safety_hotfix",
    "safe_audience_count",
    "safe_queue_manual_campaign",
]
