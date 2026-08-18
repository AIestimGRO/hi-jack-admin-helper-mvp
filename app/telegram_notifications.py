from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.db import connect, transaction
from app.product_shell import _check_csrf, _current_member, _require_master
from app.services.auth import audit


TELEGRAM_CATEGORIES = (
    ("tournaments", "Турниры"),
    ("jackside", "JACKSIDE"),
    ("rewards", "Награды и JACKCOINS"),
    ("club_updates", "Важные уведомления клуба"),
    ("marketing", "Новости и предложения"),
)

TELEGRAM_TABS = {
    "campaigns",
    "new",
    "automation",
    "templates",
    "history",
    "users",
    "settings",
}

_CATEGORY_COLUMNS = {
    "tournaments": "tournaments_enabled",
    "jackside": "jackside_enabled",
    "rewards": "rewards_enabled",
    "club_updates": "club_updates_enabled",
    "marketing": "marketing_enabled",
}


def ensure_telegram_notification_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS telegram_notification_preferences (
            client_id INTEGER PRIMARY KEY
                REFERENCES clients(id) ON DELETE CASCADE,
            notifications_enabled INTEGER NOT NULL DEFAULT 1
                CHECK(notifications_enabled IN (0,1)),
            tournaments_enabled INTEGER NOT NULL DEFAULT 1
                CHECK(tournaments_enabled IN (0,1)),
            jackside_enabled INTEGER NOT NULL DEFAULT 1
                CHECK(jackside_enabled IN (0,1)),
            rewards_enabled INTEGER NOT NULL DEFAULT 1
                CHECK(rewards_enabled IN (0,1)),
            club_updates_enabled INTEGER NOT NULL DEFAULT 1
                CHECK(club_updates_enabled IN (0,1)),
            marketing_enabled INTEGER NOT NULL DEFAULT 1
                CHECK(marketing_enabled IN (0,1)),
            subscribed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            unsubscribed_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS telegram_notification_settings (
            id INTEGER PRIMARY KEY CHECK(id=1),
            sending_enabled INTEGER NOT NULL DEFAULT 0
                CHECK(sending_enabled IN (0,1)),
            default_notifications_enabled INTEGER NOT NULL DEFAULT 1
                CHECK(default_notifications_enabled IN (0,1)),
            rate_limit_per_second INTEGER NOT NULL DEFAULT 20
                CHECK(rate_limit_per_second BETWEEN 1 AND 30),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        INSERT OR IGNORE INTO telegram_notification_settings(
            id,sending_enabled,default_notifications_enabled,rate_limit_per_second
        ) VALUES (1,0,1,20);

        CREATE TABLE IF NOT EXISTS telegram_notification_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'club_updates',
            body_text TEXT NOT NULL DEFAULT '',
            button_text TEXT,
            button_url TEXT,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS telegram_notification_campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'club_updates',
            message_text TEXT NOT NULL,
            audience_type TEXT NOT NULL DEFAULT 'all',
            audience_value TEXT,
            button_text TEXT,
            button_url TEXT,
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK(status IN ('draft','queued','sending','sent','cancelled','failed')),
            scheduled_at TEXT,
            created_by_admin_id INTEGER REFERENCES admins(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS ix_telegram_notification_campaigns_status
        ON telegram_notification_campaigns(status,scheduled_at,created_at);

        CREATE TABLE IF NOT EXISTS telegram_notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER
                REFERENCES telegram_notification_campaigns(id) ON DELETE CASCADE,
            event_key TEXT,
            event_type TEXT,
            category TEXT NOT NULL DEFAULT 'club_updates',
            client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            telegram_chat_id TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK(status IN ('queued','sending','sent','failed','skipped')),
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS ix_telegram_notification_outbox_due
        ON telegram_notification_outbox(status,next_attempt_at,created_at);

        CREATE TABLE IF NOT EXISTS telegram_notification_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outbox_id INTEGER REFERENCES telegram_notification_outbox(id) ON DELETE SET NULL,
            campaign_id INTEGER
                REFERENCES telegram_notification_campaigns(id) ON DELETE SET NULL,
            client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
            status TEXT NOT NULL,
            provider_message_id TEXT,
            error_code TEXT,
            error_text TEXT,
            delivered_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS ix_telegram_notification_deliveries_campaign
        ON telegram_notification_deliveries(campaign_id,created_at);

        CREATE TABLE IF NOT EXISTS telegram_notification_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            event_type TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'club_updates',
            template_id INTEGER
                REFERENCES telegram_notification_templates(id) ON DELETE SET NULL,
            is_enabled INTEGER NOT NULL DEFAULT 0 CHECK(is_enabled IN (0,1)),
            lead_minutes INTEGER,
            config_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS ix_telegram_notification_rules_event
        ON telegram_notification_rules(is_enabled,event_type);
        """
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO telegram_notification_preferences(client_id)
        SELECT id FROM clients
        WHERE COALESCE(telegram_user_id,'')<>'' OR COALESCE(telegram_id,'')<>''
        """
    )

    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS trg_telegram_notifications_default_after_insert
        AFTER INSERT ON clients
        WHEN COALESCE(NEW.telegram_user_id,'')<>'' OR COALESCE(NEW.telegram_id,'')<>''
        BEGIN
            INSERT OR IGNORE INTO telegram_notification_preferences(client_id)
            VALUES (NEW.id);
        END;

        CREATE TRIGGER IF NOT EXISTS trg_telegram_notifications_default_after_link
        AFTER UPDATE OF telegram_user_id,telegram_id ON clients
        WHEN
            COALESCE(OLD.telegram_user_id,'')='' AND
            COALESCE(OLD.telegram_id,'')='' AND
            (
                COALESCE(NEW.telegram_user_id,'')<>'' OR
                COALESCE(NEW.telegram_id,'')<>''
            )
        BEGIN
            INSERT INTO telegram_notification_preferences(
                client_id,
                notifications_enabled,
                tournaments_enabled,
                jackside_enabled,
                rewards_enabled,
                club_updates_enabled,
                marketing_enabled,
                subscribed_at,
                unsubscribed_at,
                updated_at
            ) VALUES (
                NEW.id,1,1,1,1,1,1,CURRENT_TIMESTAMP,NULL,CURRENT_TIMESTAMP
            )
            ON CONFLICT(client_id) DO UPDATE SET
                notifications_enabled=1,
                tournaments_enabled=1,
                jackside_enabled=1,
                rewards_enabled=1,
                club_updates_enabled=1,
                marketing_enabled=1,
                subscribed_at=CURRENT_TIMESTAMP,
                unsubscribed_at=NULL,
                updated_at=CURRENT_TIMESTAMP;
        END;
        """
    )

    templates = (
        (
            "tournament_reminder",
            "Напоминание о турнире",
            "tournaments",
            "Скоро начинается турнир {{ tournament_title }}.",
        ),
        (
            "jackside_starting",
            "JACKSIDE скоро начнётся",
            "jackside",
            "JACKSIDE начинается скоро. Один стол, одна попытка.",
        ),
        (
            "reward_received",
            "Получена награда",
            "rewards",
            "У вас новая награда Hi, Jack Club.",
        ),
    )
    conn.executemany(
        """
        INSERT OR IGNORE INTO telegram_notification_templates(
            code,title,category,body_text
        ) VALUES (?,?,?,?)
        """,
        templates,
    )

    rules = (
        (
            "tournament_start_reminder",
            "Напомнить перед турниром",
            "tournament_starting",
            "tournaments",
            "tournament_reminder",
            120,
        ),
        (
            "jackside_start_reminder",
            "Напомнить перед JACKSIDE",
            "jackside_starting",
            "jackside",
            "jackside_starting",
            30,
        ),
        (
            "reward_received",
            "Сообщить о новой награде",
            "reward_received",
            "rewards",
            "reward_received",
            None,
        ),
    )
    for code, name, event_type, category, template_code, lead_minutes in rules:
        template = conn.execute(
            "SELECT id FROM telegram_notification_templates WHERE code=?",
            (template_code,),
        ).fetchone()
        conn.execute(
            """
            INSERT OR IGNORE INTO telegram_notification_rules(
                code,name,event_type,category,template_id,is_enabled,lead_minutes
            ) VALUES (?,?,?,?,?,0,?)
            """,
            (
                code,
                name,
                event_type,
                category,
                int(template["id"]) if template else None,
                lead_minutes,
            ),
        )


def telegram_preferences(
    conn: sqlite3.Connection, client_id: int
) -> sqlite3.Row:
    conn.execute(
        """
        INSERT OR IGNORE INTO telegram_notification_preferences(client_id)
        VALUES (?)
        """,
        (client_id,),
    )
    row = conn.execute(
        "SELECT * FROM telegram_notification_preferences WHERE client_id=?",
        (client_id,),
    ).fetchone()
    if not row:
        raise RuntimeError("telegram_preferences_unavailable")
    return row


def _display_datetime(value: Any, timezone_name: str) -> str:
    if value is None or not str(value).strip():
        return "—"
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ZoneInfo(timezone_name)).strftime("%d.%m.%Y %H:%M")


def _admin_context(request: Request, **values: Any) -> dict[str, Any]:
    return {
        "request": request,
        "csrf_token": str(request.session.get("csrf") or ""),
        "admin_name": request.session.get("admin_name", "Мастер"),
        "admin_role": request.session.get("admin_role", "master_admin"),
        "asset_version": "telegram-notifications-v2",
        "telegram_categories": TELEGRAM_CATEGORIES,
        **values,
    }


def _redirect(
    message: str, *, tab: str = "campaigns", error: bool = False
) -> RedirectResponse:
    values = {"tab": tab, "error" if error else "ok": message}
    return RedirectResponse(
        f"/master/telegram?{urlencode(values)}",
        status_code=303,
    )


def _campaign_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT c.*,
                   a.display_name AS admin_name,
                   (SELECT COUNT(*) FROM telegram_notification_outbox o
                    WHERE o.campaign_id=c.id) AS queued_count,
                   (SELECT COUNT(*) FROM telegram_notification_deliveries d
                    WHERE d.campaign_id=c.id AND d.status='sent') AS sent_count,
                   (SELECT COUNT(*) FROM telegram_notification_deliveries d
                    WHERE d.campaign_id=c.id AND d.status='failed') AS failed_count
            FROM telegram_notification_campaigns c
            LEFT JOIN admins a ON a.id=c.created_by_admin_id
            ORDER BY c.created_at DESC,c.id DESC
            LIMIT 100
            """
        ).fetchall()
    )


def _linked_users(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT c.id,c.first_name,c.nickname,c.username,c.phone_local,
                   c.telegram_user_id,c.telegram_id,
                   COALESCE(p.notifications_enabled,1) AS notifications_enabled,
                   COALESCE(p.tournaments_enabled,1) AS tournaments_enabled,
                   COALESCE(p.jackside_enabled,1) AS jackside_enabled,
                   COALESCE(p.rewards_enabled,1) AS rewards_enabled,
                   COALESCE(p.club_updates_enabled,1) AS club_updates_enabled,
                   COALESCE(p.marketing_enabled,1) AS marketing_enabled,
                   p.updated_at AS preferences_updated_at
            FROM clients c
            LEFT JOIN telegram_notification_preferences p ON p.client_id=c.id
            WHERE COALESCE(c.telegram_user_id,'')<>''
               OR COALESCE(c.telegram_id,'')<>''
            ORDER BY COALESCE(c.updated_at,c.created_at) DESC,c.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    )


def _audience_count(conn: sqlite3.Connection, category: str = "club_updates") -> int:
    column = _CATEGORY_COLUMNS.get(category, "club_updates_enabled")
    row = conn.execute(
        f"""
        SELECT COUNT(*)
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
    ).fetchone()
    return int(row[0]) if row else 0


def queue_manual_campaign(
    conn: sqlite3.Connection, *, campaign_id: int
) -> int:
    campaign = conn.execute(
        "SELECT * FROM telegram_notification_campaigns WHERE id=?",
        (campaign_id,),
    ).fetchone()
    if not campaign:
        raise ValueError("Рассылка не найдена")
    if campaign["status"] not in {"draft", "queued", "failed"}:
        raise ValueError("Эту рассылку уже нельзя поставить в очередь")

    category = str(campaign["category"] or "club_updates")
    column = _CATEGORY_COLUMNS.get(category, "club_updates_enabled")
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
        key = f"manual:{campaign_id}:client:{int(row['id'])}"
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO telegram_notification_outbox(
                campaign_id,category,client_id,telegram_chat_id,
                payload_json,idempotency_key,status
            ) VALUES (?,?,?,?,?,?,'queued')
            """,
            (
                campaign_id,
                category,
                int(row["id"]),
                str(row["telegram_chat_id"]),
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                key,
            ),
        )
        if cursor.rowcount:
            queued += 1

    conn.execute(
        """
        UPDATE telegram_notification_campaigns
        SET status='queued',updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (campaign_id,),
    )
    return queued


def install_telegram_notifications(app: FastAPI) -> FastAPI:
    if getattr(app.state, "telegram_notifications_installed", False):
        return app
    app.state.telegram_notifications_installed = True
    settings = app.state.settings
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
    templates.env.globals["display_datetime"] = lambda value: _display_datetime(
        value, settings.timezone_name
    )

    with transaction(settings.db_path) as conn:
        ensure_telegram_notification_schema(conn)

    @app.get("/master/telegram", response_class=HTMLResponse)
    async def master_telegram_workspace(
        request: Request,
        tab: str = Query("campaigns"),
        ok: str = Query(""),
        error: str = Query(""),
    ):
        _require_master(request)
        tab = tab if tab in TELEGRAM_TABS else "campaigns"
        with connect(settings.db_path) as conn:
            system_settings = conn.execute(
                "SELECT * FROM telegram_notification_settings WHERE id=1"
            ).fetchone()
            campaigns = _campaign_rows(conn)
            rules = conn.execute(
                """
                SELECT r.*,t.title AS template_title
                FROM telegram_notification_rules r
                LEFT JOIN telegram_notification_templates t ON t.id=r.template_id
                ORDER BY r.id
                """
            ).fetchall()
            template_rows = conn.execute(
                """
                SELECT * FROM telegram_notification_templates
                ORDER BY is_active DESC,title,id
                """
            ).fetchall()
            deliveries = conn.execute(
                """
                SELECT d.*,c.title AS campaign_title,
                       cl.first_name,cl.nickname,cl.username
                FROM telegram_notification_deliveries d
                LEFT JOIN telegram_notification_campaigns c ON c.id=d.campaign_id
                LEFT JOIN clients cl ON cl.id=d.client_id
                ORDER BY d.created_at DESC,d.id DESC LIMIT 100
                """
            ).fetchall()
            users = _linked_users(conn)
            stats = {
                "linked": int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM clients
                        WHERE COALESCE(telegram_user_id,'')<>''
                           OR COALESCE(telegram_id,'')<>''
                        """
                    ).fetchone()[0]
                ),
                "enabled": int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM clients c
                        JOIN telegram_notification_preferences p ON p.client_id=c.id
                        WHERE (
                                COALESCE(c.telegram_user_id,'')<>''
                                OR COALESCE(c.telegram_id,'')<>''
                              )
                          AND p.notifications_enabled=1
                        """
                    ).fetchone()[0]
                ),
                "queued": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM telegram_notification_outbox WHERE status='queued'"
                    ).fetchone()[0]
                ),
                "failed": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM telegram_notification_outbox WHERE status='failed'"
                    ).fetchone()[0]
                ),
            }
            audience_counts = {
                code: _audience_count(conn, code) for code, _ in TELEGRAM_CATEGORIES
            }

        return templates.TemplateResponse(
            request,
            "admin_telegram_workspace.html",
            _admin_context(
                request,
                tab=tab,
                ok=ok,
                error=error,
                telegram_system=system_settings,
                environment_enabled=bool(
                    getattr(settings, "telegram_notifications_enabled", False)
                ),
                delivery_transport="hi_jack_club / Celery",
                campaigns=campaigns,
                rules=rules,
                templates=template_rows,
                deliveries=deliveries,
                users=users,
                stats=stats,
                audience_counts=audience_counts,
            ),
        )

    @app.post("/master/telegram/campaigns")
    async def master_create_telegram_campaign(
        request: Request,
        title: str = Form(...),
        category: str = Form("club_updates"),
        message_text: str = Form(...),
        audience_type: str = Form("all"),
        audience_value: str = Form(""),
        button_text: str = Form(""),
        button_url: str = Form(""),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        clean_title = " ".join(str(title or "").split())[:120]
        clean_message = str(message_text or "").strip()
        category_codes = {code for code, _ in TELEGRAM_CATEGORIES}

        if not clean_title:
            return _redirect("Укажите название рассылки", tab="new", error=True)
        if not clean_message:
            return _redirect("Введите текст сообщения", tab="new", error=True)
        if len(clean_message) > 4096:
            return _redirect(
                "Текст сообщения слишком длинный", tab="new", error=True
            )
        if category not in category_codes:
            category = "club_updates"
        if audience_type not in {"all", "client"}:
            audience_type = "all"
        if audience_type == "client" and not str(audience_value or "").strip():
            return _redirect("Выберите пользователя", tab="new", error=True)

        with transaction(settings.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO telegram_notification_campaigns(
                    title,category,message_text,audience_type,audience_value,
                    button_text,button_url,created_by_admin_id
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    clean_title,
                    category,
                    clean_message,
                    audience_type,
                    str(audience_value or "").strip()[:120] or None,
                    str(button_text or "").strip()[:64] or None,
                    str(button_url or "").strip()[:1000] or None,
                    request.session.get("admin_id"),
                ),
            )
            campaign_id = int(cursor.lastrowid)
            audit(
                conn,
                admin_id=int(request.session.get("admin_id")),
                admin_name=str(request.session.get("admin_name") or "master"),
                action="telegram_campaign_created",
                entity_type="telegram_notification_campaign",
                entity_id=campaign_id,
                details={"category": category, "audience_type": audience_type},
            )
        return _redirect("Черновик рассылки сохранён", tab="campaigns")

    @app.post("/master/telegram/campaigns/{campaign_id}/queue")
    async def master_queue_telegram_campaign(
        request: Request,
        campaign_id: int,
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                count = queue_manual_campaign(conn, campaign_id=campaign_id)
                audit(
                    conn,
                    admin_id=int(request.session.get("admin_id")),
                    admin_name=str(request.session.get("admin_name") or "master"),
                    action="telegram_campaign_queued",
                    entity_type="telegram_notification_campaign",
                    entity_id=campaign_id,
                    details={"new_outbox_rows": count},
                )
        except ValueError as exc:
            return _redirect(str(exc), error=True)
        return _redirect(
            f"Рассылка поставлена в очередь: {count} новых получателей"
        )

    @app.post("/master/telegram/settings")
    async def master_telegram_settings(
        request: Request,
        sending_enabled: bool = Form(False),
        rate_limit_per_second: int = Form(20),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        rate = max(1, min(30, int(rate_limit_per_second)))
        with transaction(settings.db_path) as conn:
            conn.execute(
                """
                UPDATE telegram_notification_settings
                SET sending_enabled=?,rate_limit_per_second=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=1
                """,
                (1 if sending_enabled else 0, rate),
            )
            audit(
                conn,
                admin_id=int(request.session.get("admin_id")),
                admin_name=str(request.session.get("admin_name") or "master"),
                action="telegram_notification_settings_updated",
                entity_type="telegram_notification_settings",
                entity_id=1,
                details={
                    "sending_enabled": bool(sending_enabled),
                    "rate_limit_per_second": rate,
                },
            )
        return _redirect("Настройки Telegram сохранены", tab="settings")

    @app.get("/api/master/telegram/audience-preview")
    async def master_telegram_audience_preview(
        request: Request,
        category: str = Query("club_updates"),
    ):
        _require_master(request)
        with connect(settings.db_path) as conn:
            count = _audience_count(conn, category)
        return JSONResponse({"category": category, "count": count})

    @app.get("/api/account/telegram-notifications")
    async def account_telegram_notifications(request: Request):
        member = _current_member(request, required=True)
        with transaction(settings.db_path) as conn:
            client = conn.execute(
                "SELECT telegram_user_id,telegram_id FROM clients WHERE id=?",
                (int(member["client_id"]),),
            ).fetchone()
            row = telegram_preferences(conn, int(member["client_id"]))
        return JSONResponse(
            {
                "connected": bool(
                    client
                    and (
                        str(client["telegram_user_id"] or "").strip()
                        or str(client["telegram_id"] or "").strip()
                    )
                ),
                "notifications_enabled": bool(row["notifications_enabled"]),
                "categories": {
                    "tournaments": bool(row["tournaments_enabled"]),
                    "jackside": bool(row["jackside_enabled"]),
                    "rewards": bool(row["rewards_enabled"]),
                    "club_updates": bool(row["club_updates_enabled"]),
                    "marketing": bool(row["marketing_enabled"]),
                },
                "csrf_token": str(request.session.get("csrf") or ""),
            }
        )

    @app.post("/api/account/telegram-notifications")
    async def account_update_telegram_notifications(
        request: Request,
        notifications_enabled: bool = Form(False),
        tournaments_enabled: bool = Form(False),
        jackside_enabled: bool = Form(False),
        rewards_enabled: bool = Form(False),
        club_updates_enabled: bool = Form(False),
        marketing_enabled: bool = Form(False),
        csrf_token: str = Form(...),
    ):
        member = _current_member(request, required=True)
        _check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            client = conn.execute(
                "SELECT telegram_user_id,telegram_id FROM clients WHERE id=?",
                (int(member["client_id"]),),
            ).fetchone()
            if not client or not (
                str(client["telegram_user_id"] or "").strip()
                or str(client["telegram_id"] or "").strip()
            ):
                raise HTTPException(status_code=409, detail="telegram_not_connected")

            telegram_preferences(conn, int(member["client_id"]))
            conn.execute(
                """
                UPDATE telegram_notification_preferences
                SET notifications_enabled=?,
                    tournaments_enabled=?,
                    jackside_enabled=?,
                    rewards_enabled=?,
                    club_updates_enabled=?,
                    marketing_enabled=?,
                    subscribed_at=CASE
                        WHEN notifications_enabled=0 AND ?=1
                        THEN CURRENT_TIMESTAMP ELSE subscribed_at END,
                    unsubscribed_at=CASE
                        WHEN ?=0 THEN CURRENT_TIMESTAMP ELSE NULL END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE client_id=?
                """,
                (
                    1 if notifications_enabled else 0,
                    1 if tournaments_enabled else 0,
                    1 if jackside_enabled else 0,
                    1 if rewards_enabled else 0,
                    1 if club_updates_enabled else 0,
                    1 if marketing_enabled else 0,
                    1 if notifications_enabled else 0,
                    1 if notifications_enabled else 0,
                    int(member["client_id"]),
                ),
            )
        return JSONResponse({"ok": True})

    return app


__all__ = [
    "TELEGRAM_CATEGORIES",
    "ensure_telegram_notification_schema",
    "install_telegram_notifications",
    "queue_manual_campaign",
    "telegram_preferences",
]
