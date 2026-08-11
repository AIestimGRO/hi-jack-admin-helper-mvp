from __future__ import annotations

import json
import sqlite3

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.db import connect
from app.product_shell import _require_master
from app.services.login_security import ensure_login_security_schema


_EVENT_LABELS = {
    "login_success": "Успешный вход",
    "login_failed": "Неверный пароль",
    "login_locked": "Вход временно заблокирован",
    "login_blocked": "Попытка входа во время блокировки",
    "email_changed": "Почта изменена",
    "phone_changed": "Телефон изменён",
    "password_changed": "Пароль изменён",
    "account_deleted": "Аккаунт удалён",
    "birth_date_set": "Дата рождения сохранена",
}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def install_security_journal(app: FastAPI) -> FastAPI:
    if getattr(app.state, "security_journal_installed", False):
        return app
    app.state.security_journal_installed = True
    settings = app.state.settings

    @app.get("/api/master/member-security-events")
    async def master_member_security_events(request: Request, client_id: int):
        _require_master(request)
        events: list[dict[str, object]] = []
        with connect(settings.db_path) as conn:
            ensure_login_security_schema(conn)
            account = conn.execute(
                "SELECT id FROM member_accounts WHERE client_id=? LIMIT 1",
                (int(client_id),),
            ).fetchone()
            if not account:
                return JSONResponse({"events": []})
            account_id = int(account["id"])
            for row in conn.execute(
                """
                SELECT action,details,created_at
                FROM auth_security_events
                WHERE principal_type='member' AND principal_id=?
                ORDER BY id DESC LIMIT 50
                """,
                (account_id,),
            ).fetchall():
                try:
                    details = json.loads(str(row["details"] or "{}"))
                except json.JSONDecodeError:
                    details = {}
                events.append(
                    {
                        "action": str(row["action"]),
                        "label": _EVENT_LABELS.get(
                            str(row["action"]), str(row["action"])
                        ),
                        "created_at": str(row["created_at"] or ""),
                        "details": details,
                    }
                )
            if _table_exists(conn, "member_account_security_events"):
                for row in conn.execute(
                    """
                    SELECT action,created_at
                    FROM member_account_security_events
                    WHERE account_id=?
                    ORDER BY id DESC LIMIT 50
                    """,
                    (account_id,),
                ).fetchall():
                    events.append(
                        {
                            "action": str(row["action"]),
                            "label": _EVENT_LABELS.get(
                                str(row["action"]), str(row["action"])
                            ),
                            "created_at": str(row["created_at"] or ""),
                            "details": {},
                        }
                    )
        events.sort(key=lambda item: str(item["created_at"]), reverse=True)
        return JSONResponse({"events": events[:50]})

    return app


__all__ = ["install_security_journal"]
