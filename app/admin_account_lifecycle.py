from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse

from app.account_security import anonymize_account
from app.db import transaction
from app.product_shell import _check_csrf, _require_master
from app.services.auth import audit


IGNORABLE_CLIENT_TABLES = {
    "client_preferences",
    "client_phone_aliases",
}


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def linked_client_records(conn: sqlite3.Connection, client_id: int) -> list[tuple[str, str, int]]:
    """Return meaningful rows that still point at a client.

    Preference defaults and the legacy phone-alias table are intentionally ignored so
    an otherwise empty imported/manual client can still be removed completely.
    """
    linked: list[tuple[str, str, int]] = []
    tables = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    for table_row in tables:
        table = str(table_row["name"])
        if table in {"clients", "member_accounts"} | IGNORABLE_CLIENT_TABLES:
            continue
        columns = conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
        client_columns = [
            str(column["name"])
            for column in columns
            if str(column["name"]) == "client_id"
            or str(column["name"]).endswith("_client_id")
        ]
        for column in client_columns:
            count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_identifier(table)} "
                    f"WHERE {_quote_identifier(column)}=?",
                    (client_id,),
                ).fetchone()[0]
            )
            if count:
                linked.append((table, column, count))
    return linked


def _redirect(message: str, *, client_id: int | None = None, error: bool = False) -> RedirectResponse:
    values = {"error" if error else "ok": message}
    if client_id is not None:
        values["client_id"] = str(client_id)
    return RedirectResponse(
        f"/master/member-accounts?{urlencode(values)}",
        status_code=303,
    )


def _delete_avatar_file(settings, avatar_path: str | None) -> None:
    value = str(avatar_path or "")
    prefix = "/reward-media/profile-avatars/"
    if not value.startswith(prefix):
        return
    target = (
        Path(settings.db_path).resolve().parent
        / "reward-media"
        / "profile-avatars"
        / Path(value).name
    )
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


def install_admin_account_lifecycle(app: FastAPI) -> FastAPI:
    if getattr(app.state, "admin_account_lifecycle_installed", False):
        return app
    app.state.admin_account_lifecycle_installed = True
    settings = app.state.settings

    @app.post("/master/member-accounts/{client_id}/delete-account")
    async def master_delete_member_account(
        request: Request,
        client_id: int,
        confirmation: str = Form(...),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        if " ".join(str(confirmation or "").split()).upper() != "УДАЛИТЬ ЛК":
            return _redirect(
                "Для удаления ЛК введите УДАЛИТЬ ЛК",
                client_id=client_id,
                error=True,
            )
        avatar_path: str | None = None
        try:
            with transaction(settings.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT c.client_status, ma.id AS account_id
                    FROM clients c
                    LEFT JOIN member_accounts ma ON ma.client_id=c.id
                    WHERE c.id=?
                    """,
                    (client_id,),
                ).fetchone()
                if not row:
                    raise ValueError("Игрок не найден")
                if not row["account_id"]:
                    raise ValueError("У игрока нет зарегистрированного ЛК")
                if str(row["client_status"] or "") == "deleted":
                    raise ValueError("ЛК уже удалён и данные обезличены")
                account_id = int(row["account_id"])
                avatar_path = anonymize_account(
                    conn,
                    account_id=account_id,
                    client_id=client_id,
                )
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auth_login_state'"
                ).fetchone():
                    conn.execute(
                        "DELETE FROM auth_login_state WHERE principal_type='member' AND principal_id=?",
                        (account_id,),
                    )
                audit(
                    conn,
                    admin_id=int(request.session.get("admin_id")),
                    admin_name=str(request.session.get("admin_name") or "master"),
                    action="member_account_anonymized_by_master",
                    entity_type="client",
                    entity_id=client_id,
                    details={"account_id": account_id},
                )
        except ValueError as exc:
            return _redirect(str(exc), client_id=client_id, error=True)
        _delete_avatar_file(settings, avatar_path)
        return _redirect(
            "ЛК удалён: доступ закрыт, персональные данные обезличены, история сохранена",
            client_id=client_id,
        )

    @app.post("/master/member-accounts/{client_id}/delete-empty")
    async def master_delete_empty_client(
        request: Request,
        client_id: int,
        confirmation: str = Form(...),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        if " ".join(str(confirmation or "").split()).upper() != "УДАЛИТЬ ЗАПИСЬ":
            return _redirect(
                "Для полного удаления введите УДАЛИТЬ ЗАПИСЬ",
                client_id=client_id,
                error=True,
            )
        try:
            with transaction(settings.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT c.id, ma.id AS account_id
                    FROM clients c
                    LEFT JOIN member_accounts ma ON ma.client_id=c.id
                    WHERE c.id=?
                    """,
                    (client_id,),
                ).fetchone()
                if not row:
                    raise ValueError("Игрок не найден")
                if row["account_id"]:
                    raise ValueError(
                        "Полностью удалить запись с ЛК нельзя. Используйте безопасное удаление ЛК"
                    )
                linked = linked_client_records(conn, client_id)
                if linked:
                    total = sum(item[2] for item in linked)
                    raise ValueError(
                        f"Полное удаление запрещено: найдено связанных записей — {total}. "
                        "Историю клуба нужно сохранить"
                    )
                audit(
                    conn,
                    admin_id=int(request.session.get("admin_id")),
                    admin_name=str(request.session.get("admin_name") or "master"),
                    action="empty_client_deleted",
                    entity_type="client",
                    entity_id=client_id,
                    details={},
                )
                conn.execute("DELETE FROM client_phone_aliases WHERE client_id=?", (client_id,))
                conn.execute("DELETE FROM client_preferences WHERE client_id=?", (client_id,))
                conn.execute("DELETE FROM clients WHERE id=?", (client_id,))
        except (ValueError, sqlite3.IntegrityError) as exc:
            message = str(exc)
            if isinstance(exc, sqlite3.IntegrityError):
                message = "Запись связана с другими данными и не может быть удалена полностью"
            return _redirect(message, client_id=client_id, error=True)
        return _redirect("Пустая запись клиента удалена полностью")

    return app


__all__ = [
    "install_admin_account_lifecycle",
    "linked_client_records",
]
