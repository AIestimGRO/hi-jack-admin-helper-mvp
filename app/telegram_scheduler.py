from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.db import connect, transaction
from app.product_shell import _check_csrf, _require_master
from app.services.auth import audit
from app.telegram_notifications import TELEGRAM_CATEGORIES, queue_manual_campaign
from app.telegram_transport import dispatch_telegram_outbox_once, transport_status

MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")
UTC = timezone.utc


def _utc_sql(value: datetime) -> str:
    normalized = value.astimezone(UTC).replace(tzinfo=None, microsecond=0)
    return normalized.strftime("%Y-%m-%d %H:%M:%S")


def parse_moscow_schedule(
    value: str,
    *,
    now_utc: datetime | None = None,
) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Укажите дату и время отправки по МСК")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("Некорректная дата или время отправки") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    scheduled_utc = parsed.replace(tzinfo=MOSCOW_TIMEZONE).astimezone(UTC)
    current = now_utc or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    else:
        current = current.astimezone(UTC)
    if scheduled_utc <= current:
        raise ValueError("Время отправки должно быть в будущем")
    return _utc_sql(scheduled_utc)


def display_moscow_schedule(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "—"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(MOSCOW_TIMEZONE).strftime("%d.%m.%Y %H:%M МСК")


def moscow_input_value(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(MOSCOW_TIMEZONE).strftime("%Y-%m-%dT%H:%M")


def _redirect(message: str, *, error: bool = False) -> RedirectResponse:
    values = {"error" if error else "ok": message}
    return RedirectResponse(
        f"/master/telegram/scheduler?{urlencode(values)}",
        status_code=303,
    )


def _scheduled_rows(db_path: Path) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM telegram_notification_outbox o
                    WHERE o.campaign_id=c.id) AS outbox_count,
                   (SELECT COUNT(*) FROM telegram_notification_deliveries d
                    WHERE d.campaign_id=c.id AND d.status='sent') AS sent_count
            FROM telegram_notification_campaigns c
            WHERE c.scheduled_at IS NOT NULL
            ORDER BY
                CASE WHEN c.status='draft' THEN 0 ELSE 1 END,
                c.scheduled_at ASC,
                c.id ASC
            LIMIT 250
            """
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["scheduled_msk"] = display_moscow_schedule(item.get("scheduled_at"))
        item["scheduled_input"] = moscow_input_value(item.get("scheduled_at"))
        item["is_planned"] = item.get("status") == "draft"
        result.append(item)
    return result


def release_due_scheduled_campaigns(
    db_path: Path,
    *,
    now_utc: datetime | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    current = now_utc or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    due_at = _utc_sql(current)
    safe_limit = max(1, min(int(limit), 250))
    campaign_ids: list[int] = []
    queued_messages = 0

    with transaction(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id
            FROM telegram_notification_campaigns
            WHERE status='draft'
              AND scheduled_at IS NOT NULL
              AND scheduled_at<=?
            ORDER BY scheduled_at,id
            LIMIT ?
            """,
            (due_at, safe_limit),
        ).fetchall()
        for row in rows:
            campaign_id = int(row["id"])
            queued = queue_manual_campaign(conn, campaign_id=campaign_id)
            if queued == 0:
                conn.execute(
                    """
                    UPDATE telegram_notification_campaigns
                    SET status='failed',updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status='queued'
                    """,
                    (campaign_id,),
                )
            campaign_ids.append(campaign_id)
            queued_messages += int(queued)

    return {
        "campaigns_released": len(campaign_ids),
        "messages_queued": queued_messages,
        "campaign_ids": campaign_ids,
    }


def run_scheduled_delivery_once(
    settings: Any,
    *,
    campaign_limit: int = 100,
    dispatch_limit: int = 100,
    sender: Callable[[str, str, dict[str, Any], float], dict[str, Any]] | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    gate = transport_status(settings)
    if not bool(gate.get("ready")):
        return {
            "ok": False,
            "reason": str(gate.get("reason") or "transport_not_ready"),
            "campaigns_released": 0,
            "messages_queued": 0,
            "dispatch": None,
        }

    released = release_due_scheduled_campaigns(
        Path(settings.db_path),
        now_utc=now_utc,
        limit=campaign_limit,
    )
    dispatch = dispatch_telegram_outbox_once(
        settings,
        limit=dispatch_limit,
        sender=sender,
    )
    return {
        "ok": bool(dispatch.get("ok")),
        "reason": str(dispatch.get("reason") or "dispatched"),
        **released,
        "dispatch": dispatch,
    }


def install_telegram_scheduler(app: FastAPI) -> FastAPI:
    if getattr(app.state, "telegram_scheduler_installed", False):
        return app
    app.state.telegram_scheduler_installed = True
    settings = app.state.settings
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

    @app.get("/master/telegram/scheduler", response_class=HTMLResponse)
    async def master_telegram_scheduler(
        request: Request,
        ok: str = "",
        error: str = "",
    ):
        _require_master(request)
        now_msk = datetime.now(MOSCOW_TIMEZONE)
        return templates.TemplateResponse(
            request,
            "admin_telegram_scheduler.html",
            {
                "request": request,
                "csrf_token": str(request.session.get("csrf") or ""),
                "admin_name": request.session.get("admin_name", "Мастер"),
                "admin_role": request.session.get("admin_role", "master_admin"),
                "asset_version": "telegram-scheduler-v1",
                "telegram_categories": TELEGRAM_CATEGORIES,
                "scheduled_campaigns": _scheduled_rows(Path(settings.db_path)),
                "schedule_min_msk": now_msk.strftime("%Y-%m-%dT%H:%M"),
                "ok": ok,
                "error": error,
            },
        )

    @app.post("/master/telegram/scheduler")
    async def master_create_scheduled_telegram_campaign(
        request: Request,
        title: str = Form(...),
        category: str = Form("club_updates"),
        message_text: str = Form(...),
        audience_type: str = Form("all"),
        audience_value: str = Form(""),
        button_text: str = Form(""),
        button_url: str = Form(""),
        scheduled_at_msk: str = Form(...),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        clean_title = " ".join(str(title or "").split())[:120]
        clean_message = str(message_text or "").strip()
        category_codes = {code for code, _ in TELEGRAM_CATEGORIES}
        if not clean_title:
            return _redirect("Укажите название рассылки", error=True)
        if not clean_message:
            return _redirect("Введите текст сообщения", error=True)
        if len(clean_message) > 4096:
            return _redirect("Текст сообщения слишком длинный", error=True)
        if category not in category_codes:
            category = "club_updates"
        if audience_type not in {"all", "client"}:
            audience_type = "all"
        audience_value = str(audience_value or "").strip()[:120]
        if audience_type == "client":
            try:
                int(audience_value)
            except ValueError:
                return _redirect("Для персональной рассылки укажите ID пользователя", error=True)
        try:
            scheduled_at = parse_moscow_schedule(scheduled_at_msk)
        except ValueError as exc:
            return _redirect(str(exc), error=True)

        with transaction(settings.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO telegram_notification_campaigns(
                    title,category,message_text,audience_type,audience_value,
                    button_text,button_url,status,scheduled_at,created_by_admin_id
                ) VALUES (?,?,?,?,?,?,?,'draft',?,?)
                """,
                (
                    clean_title,
                    category,
                    clean_message,
                    audience_type,
                    audience_value or None,
                    str(button_text or "").strip()[:64] or None,
                    str(button_url or "").strip()[:1000] or None,
                    scheduled_at,
                    request.session.get("admin_id"),
                ),
            )
            campaign_id = int(cursor.lastrowid)
            audit(
                conn,
                admin_id=int(request.session.get("admin_id")),
                admin_name=str(request.session.get("admin_name") or "master"),
                action="telegram_campaign_scheduled",
                entity_type="telegram_notification_campaign",
                entity_id=campaign_id,
                details={
                    "category": category,
                    "audience_type": audience_type,
                    "scheduled_at_msk": scheduled_at_msk,
                    "scheduled_at_utc": scheduled_at,
                },
            )
        return _redirect(
            f"Рассылка запланирована на {display_moscow_schedule(scheduled_at)}"
        )

    @app.post("/master/telegram/scheduler/{campaign_id}/reschedule")
    async def master_reschedule_telegram_campaign(
        request: Request,
        campaign_id: int,
        scheduled_at_msk: str = Form(...),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        try:
            scheduled_at = parse_moscow_schedule(scheduled_at_msk)
        except ValueError as exc:
            return _redirect(str(exc), error=True)
        with transaction(settings.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE telegram_notification_campaigns
                SET scheduled_at=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='draft' AND scheduled_at IS NOT NULL
                """,
                (scheduled_at, campaign_id),
            )
            if not cursor.rowcount:
                return _redirect("Эту рассылку уже нельзя перенести", error=True)
            audit(
                conn,
                admin_id=int(request.session.get("admin_id")),
                admin_name=str(request.session.get("admin_name") or "master"),
                action="telegram_campaign_rescheduled",
                entity_type="telegram_notification_campaign",
                entity_id=campaign_id,
                details={
                    "scheduled_at_msk": scheduled_at_msk,
                    "scheduled_at_utc": scheduled_at,
                },
            )
        return _redirect(
            f"Новое время: {display_moscow_schedule(scheduled_at)}"
        )

    @app.post("/master/telegram/scheduler/{campaign_id}/cancel")
    async def master_cancel_scheduled_telegram_campaign(
        request: Request,
        campaign_id: int,
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE telegram_notification_campaigns
                SET status='cancelled',updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='draft' AND scheduled_at IS NOT NULL
                """,
                (campaign_id,),
            )
            if not cursor.rowcount:
                return _redirect("Эту рассылку уже нельзя отменить", error=True)
            audit(
                conn,
                admin_id=int(request.session.get("admin_id")),
                admin_name=str(request.session.get("admin_name") or "master"),
                action="telegram_campaign_schedule_cancelled",
                entity_type="telegram_notification_campaign",
                entity_id=campaign_id,
                details={},
            )
        return _redirect("Запланированная рассылка отменена")

    return app


__all__ = [
    "display_moscow_schedule",
    "install_telegram_scheduler",
    "parse_moscow_schedule",
    "release_due_scheduled_campaigns",
    "run_scheduled_delivery_once",
]
