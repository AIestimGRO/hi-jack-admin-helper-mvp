from __future__ import annotations

import sqlite3
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse

from app.db import transaction
from app.product_shell import _check_csrf, _require_master
from app.services.auth import audit


def unlink_telegram_identity(
    conn: sqlite3.Connection, client_id: int
) -> tuple[int | None, bool]:
    row = conn.execute(
        """
        SELECT c.id,c.telegram_id,c.telegram_user_id,ma.id AS account_id
        FROM clients c
        LEFT JOIN member_accounts ma ON ma.client_id=c.id
        WHERE c.id=?
        """,
        (client_id,),
    ).fetchone()
    if not row:
        raise ValueError("Игрок не найден")

    had_link = bool(
        str(row["telegram_id"] or "").strip()
        or str(row["telegram_user_id"] or "").strip()
    )
    if not had_link:
        raise ValueError("Telegram уже не привязан к этой учётной записи")

    conn.execute(
        """
        UPDATE clients
        SET telegram_id=NULL,telegram_user_id=NULL,updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (client_id,),
    )
    account_id = int(row["account_id"]) if row["account_id"] else None
    return account_id, True


def _redirect(
    message: str, *, client_id: int, error: bool = False
) -> RedirectResponse:
    values = {
        "client_id": str(client_id),
        "error" if error else "ok": message,
    }
    return RedirectResponse(
        f"/master/member-accounts?{urlencode(values)}",
        status_code=303,
    )


def install_admin_telegram_unlink(app: FastAPI) -> FastAPI:
    if getattr(app.state, "admin_telegram_unlink_installed", False):
        return app
    app.state.admin_telegram_unlink_installed = True
    settings = app.state.settings

    @app.post("/master/member-accounts/{client_id}/unlink-telegram")
    async def master_unlink_telegram(
        request: Request,
        client_id: int,
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                account_id, _ = unlink_telegram_identity(conn, client_id)

                if account_id and conn.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='member_account_security_events'
                    """
                ).fetchone():
                    conn.execute(
                        """
                        INSERT INTO member_account_security_events(
                            account_id,client_id,action
                        ) VALUES (?,?,?)
                        """,
                        (account_id, client_id, "telegram_unlinked_by_master"),
                    )

                audit(
                    conn,
                    admin_id=int(request.session.get("admin_id")),
                    admin_name=str(request.session.get("admin_name") or "master"),
                    action="member_telegram_unlinked",
                    entity_type="client",
                    entity_id=client_id,
                    details={"account_id": account_id},
                )
        except ValueError as exc:
            return _redirect(str(exc), client_id=client_id, error=True)

        return _redirect(
            "Telegram отвязан. Его можно привязать к другому ЛК",
            client_id=client_id,
        )

    return app


__all__ = ["install_admin_telegram_unlink", "unlink_telegram_identity"]
