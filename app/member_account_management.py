from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.db import connect, transaction
from app.product_shell import _check_csrf, _current_member, _require_master
from app.services.auth import audit
from app.services.member_accounts import hash_password, verify_password
from app.services.phone import display_phone, full_phone, normalize_phone
from app.services.quiz_identity import normalize_email

_SCHEMA_LOCK = threading.Lock()


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(str(row[1]) == column for row in conn.execute(f"PRAGMA table_info({table})"))


def ensure_member_account_management_schema(conn: sqlite3.Connection) -> None:
    if not _has_column(conn, "clients", "birth_date"):
        conn.execute("ALTER TABLE clients ADD COLUMN birth_date TEXT")
    if not _has_column(conn, "member_accounts", "birth_date_required"):
        conn.execute(
            "ALTER TABLE member_accounts ADD COLUMN birth_date_required INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_clients_birth_date
        ON clients(birth_date)
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_member_accounts_require_birth_date
        AFTER INSERT ON member_accounts
        BEGIN
            UPDATE member_accounts
            SET birth_date_required=1
            WHERE id=NEW.id;
        END
        """
    )


def normalize_birth_date(value: str, *, required: bool = True) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        if required:
            raise ValueError("Укажите дату рождения")
        return None
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("Укажите корректную дату рождения") from exc
    if parsed > datetime.now().date():
        raise ValueError("Дата рождения не может быть в будущем")
    if parsed < date(1900, 1, 1):
        raise ValueError("Проверьте дату рождения")
    return parsed.isoformat()


def assert_current_phone_available(
    conn: sqlite3.Connection, *, phone_local: str, client_id: int | None = None
) -> None:
    normalized = normalize_phone(phone_local)
    if not normalized:
        raise ValueError("Введите корректный российский номер телефона")
    if client_id is None:
        row = conn.execute(
            """
            SELECT c.id
            FROM clients c
            JOIN member_accounts ma ON ma.client_id=c.id
            WHERE c.phone_local=?
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT c.id
            FROM clients c
            WHERE c.phone_local=? AND c.id<>?
              AND COALESCE(c.client_status,'existing')<>'deleted'
            LIMIT 1
            """,
            (normalized, client_id),
        ).fetchone()
    if row:
        raise ValueError("Этот номер уже привязан к другому аккаунту")


def _member_redirect(message: str, *, error: bool = False) -> RedirectResponse:
    key = "error" if error else "ok"
    return RedirectResponse(
        f"/account?{urlencode({'tab': 'profile', key: message})}", status_code=303
    )


def _master_redirect(
    message: str,
    *,
    client_id: int | None = None,
    error: bool = False,
) -> RedirectResponse:
    values: dict[str, str] = {"error" if error else "ok": message}
    if client_id is not None:
        values["client_id"] = str(client_id)
    return RedirectResponse(
        f"/master/member-accounts?{urlencode(values)}", status_code=303
    )


def _identity_row(conn: sqlite3.Connection, client_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT c.*,ma.id AS account_id,ma.email AS account_email,
               ma.email_normalized AS account_email_normalized,
               ma.is_active AS account_active,ma.session_version,
               ma.last_login_at,ma.created_at AS account_created_at,
               ma.updated_at AS account_updated_at,ma.birth_date_required
        FROM clients c
        LEFT JOIN member_accounts ma ON ma.client_id=c.id
        WHERE c.id=?
        """,
        (client_id,),
    ).fetchone()


def _security_event(conn: sqlite3.Connection, *, account_id: int, client_id: int, action: str) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='member_account_security_events'"
    ).fetchone()
    if exists:
        conn.execute(
            """
            INSERT INTO member_account_security_events(account_id,client_id,action)
            VALUES (?,?,?)
            """,
            (account_id, client_id, action),
        )


def install_member_account_management(app: FastAPI) -> FastAPI:
    if getattr(app.state, "member_account_management_installed", False):
        return app
    app.state.member_account_management_installed = True
    app.state.member_account_management_schema_ready = False
    settings = app.state.settings
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

    @app.middleware("http")
    async def member_account_management_schema_middleware(request: Request, call_next):
        if not request.app.state.member_account_management_schema_ready:
            with _SCHEMA_LOCK:
                if not request.app.state.member_account_management_schema_ready:
                    with transaction(settings.db_path) as conn:
                        ensure_member_account_management_schema(conn)
                    request.app.state.member_account_management_schema_ready = True

        response = await call_next(request)

        if request.method == "GET" and request.url.path.startswith("/account"):
            member = _current_member(request, required=False)
            if member:
                with connect(settings.db_path) as conn:
                    row = conn.execute(
                        """
                        SELECT c.birth_date,ma.birth_date_required
                        FROM member_accounts ma
                        JOIN clients c ON c.id=ma.client_id
                        WHERE ma.id=?
                        """,
                        (int(member["id"]),),
                    ).fetchone()
                if row and int(row["birth_date_required"] or 0) and not row["birth_date"]:
                    draft_value = ""
                    try:
                        draft_value = str(request.session.get("member_registration_birth_date") or "")
                    except AssertionError:
                        draft_value = ""
                    if draft_value:
                        try:
                            birth_date = normalize_birth_date(draft_value)
                            with transaction(settings.db_path) as conn:
                                conn.execute(
                                    "UPDATE clients SET birth_date=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                    (birth_date, int(member["client_id"])),
                                )
                                conn.execute(
                                    "UPDATE member_accounts SET birth_date_required=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                    (int(member["id"]),),
                                )
                            request.session.pop("member_registration_birth_date", None)
                        except (ValueError, AssertionError):
                            pass
                    else:
                        allowed = {
                            "/account/birthday",
                            "/account/logout",
                            "/account/login",
                        }
                        if request.url.path not in allowed:
                            return RedirectResponse("/account/birthday", status_code=303)
        return response

    @app.post("/api/account/register/draft-extra")
    async def account_register_draft_extra(
        request: Request,
        birth_date: str = Form(...),
        phone: str = Form(...),
        csrf_token: str = Form(...),
    ):
        _check_csrf(request, csrf_token)
        try:
            normalized_birth_date = normalize_birth_date(birth_date)
            phone_local = normalize_phone(phone)
            if not phone_local:
                raise ValueError("Введите корректный российский номер телефона")
            with connect(settings.db_path) as conn:
                assert_current_phone_available(conn, phone_local=phone_local)
            request.session["member_registration_birth_date"] = normalized_birth_date
            return JSONResponse({"ok": True})
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)

    @app.get("/account/birthday", response_class=HTMLResponse)
    async def account_birthday_page(request: Request, error: str = ""):
        member = _current_member(request, required=True)
        with connect(settings.db_path) as conn:
            row = conn.execute(
                "SELECT birth_date FROM clients WHERE id=?",
                (int(member["client_id"]),),
            ).fetchone()
        if row and row["birth_date"]:
            return RedirectResponse("/account?tab=profile", status_code=303)
        return templates.TemplateResponse(
            request,
            "member_birthday.html",
            {
                "request": request,
                "member": member,
                "current_tab": "profile",
                "csrf_token": request.session.get("csrf", ""),
                "asset_version": "member-account-management-v1",
                "error": error,
            },
        )

    @app.post("/account/birthday")
    async def account_birthday_save(
        request: Request,
        birth_date: str = Form(...),
        csrf_token: str = Form(...),
    ):
        member = _current_member(request, required=True)
        _check_csrf(request, csrf_token)
        try:
            normalized = normalize_birth_date(birth_date)
            with transaction(settings.db_path) as conn:
                current = conn.execute(
                    "SELECT birth_date FROM clients WHERE id=?",
                    (int(member["client_id"]),),
                ).fetchone()
                if current and current["birth_date"]:
                    raise ValueError("Дата рождения уже сохранена. Для исправления обратитесь к администратору")
                conn.execute(
                    "UPDATE clients SET birth_date=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (normalized, int(member["client_id"])),
                )
                conn.execute(
                    "UPDATE member_accounts SET birth_date_required=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (int(member["id"]),),
                )
                _security_event(
                    conn,
                    account_id=int(member["id"]),
                    client_id=int(member["client_id"]),
                    action="birth_date_set",
                )
            request.session.pop("member_registration_birth_date", None)
        except ValueError as exc:
            return RedirectResponse(
                f"/account/birthday?{urlencode({'error': str(exc)})}", status_code=303
            )
        return _member_redirect("Дата рождения сохранена")

    @app.get("/api/account/identity-state")
    async def account_identity_state(request: Request):
        member = _current_member(request, required=True)
        with connect(settings.db_path) as conn:
            row = conn.execute(
                "SELECT birth_date FROM clients WHERE id=?",
                (int(member["client_id"]),),
            ).fetchone()
        return JSONResponse({"birth_date": str(row["birth_date"] or "") if row else ""})

    @app.post("/account/security/password/change")
    async def account_password_change(
        request: Request,
        current_password: str = Form(...),
        new_password: str = Form(...),
        new_password_confirmation: str = Form(...),
        csrf_token: str = Form(...),
    ):
        member = _current_member(request, required=True)
        _check_csrf(request, csrf_token)
        if new_password != new_password_confirmation:
            return _member_redirect("Новые пароли не совпадают", error=True)
        try:
            new_hash = hash_password(new_password)
            with transaction(settings.db_path) as conn:
                account = conn.execute(
                    "SELECT password_hash FROM member_accounts WHERE id=? AND is_active=1",
                    (int(member["id"]),),
                ).fetchone()
                if not account or not verify_password(current_password, str(account["password_hash"])):
                    raise ValueError("Текущий пароль указан неверно")
                conn.execute(
                    "UPDATE member_accounts SET password_hash=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (new_hash, int(member["id"])),
                )
                conn.execute(
                    """
                    UPDATE member_sessions SET revoked_at=CURRENT_TIMESTAMP
                    WHERE account_id=? AND id<>? AND revoked_at IS NULL
                    """,
                    (int(member["id"]), int(member["member_session_id"])),
                )
                _security_event(
                    conn,
                    account_id=int(member["id"]),
                    client_id=int(member["client_id"]),
                    action="password_changed",
                )
        except ValueError as exc:
            return _member_redirect(str(exc), error=True)
        return _member_redirect("Пароль изменён. Другие активные сессии закрыты")

    @app.get("/master/member-accounts", response_class=HTMLResponse)
    async def master_member_accounts(
        request: Request,
        q: str = "",
        client_id: int | None = None,
        ok: str = "",
        error: str = "",
    ):
        _require_master(request)
        search = " ".join(str(q or "").split())[:120]
        where = ""
        params: list[Any] = []
        if search:
            like = f"%{search}%"
            where = """
                WHERE CAST(c.id AS TEXT) LIKE ?
                   OR COALESCE(c.first_name,'') LIKE ?
                   OR COALESCE(c.nickname,'') LIKE ?
                   OR COALESCE(c.username,'') LIKE ?
                   OR COALESCE(c.phone_full,'') LIKE ?
                   OR COALESCE(c.phone_local,'') LIKE ?
                   OR COALESCE(ma.email,'') LIKE ?
                   OR COALESCE(c.app_user_id,'') LIKE ?
                   OR COALESCE(c.referrer_app_user_id,'') LIKE ?
            """
            params = [like] * 9
        with connect(settings.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT c.id,c.first_name,c.nickname,c.username,c.phone_full,c.phone_local,
                       c.birth_date,c.client_status,c.app_user_id,c.referrer_app_user_id,
                       c.created_at,ma.id AS account_id,ma.email AS account_email,
                       ma.is_active AS account_active,ma.last_login_at
                FROM clients c
                LEFT JOIN member_accounts ma ON ma.client_id=c.id
                {where}
                ORDER BY CASE WHEN ma.id IS NULL THEN 1 ELSE 0 END,c.updated_at DESC,c.id DESC
                LIMIT 200
                """,
                params,
            ).fetchall()
            selected = _identity_row(conn, client_id) if client_id else None
        return templates.TemplateResponse(
            request,
            "master_member_accounts.html",
            {
                "request": request,
                "rows": rows,
                "selected": selected,
                "q": search,
                "ok": ok,
                "error": error,
                "csrf_token": request.session.get("csrf", ""),
                "admin_name": request.session.get("admin_name", "Мастер"),
                "admin_role": request.session.get("admin_role", "master_admin"),
                "asset_version": "member-account-management-v1",
                "display_phone": display_phone,
            },
        )

    @app.post("/master/member-accounts/{client_id}/update")
    async def master_member_account_update(
        request: Request,
        client_id: int,
        first_name: str = Form(""),
        nickname: str = Form(""),
        username: str = Form(""),
        phone: str = Form(""),
        email: str = Form(""),
        birth_date: str = Form(""),
        app_user_id: str = Form(""),
        referrer_app_user_id: str = Form(""),
        telegram_id: str = Form(""),
        telegram_user_id: str = Form(""),
        source: str = Form(""),
        acquisition_campaign_code: str = Form(""),
        acquisition_source: str = Form(""),
        client_status: str = Form("existing"),
        account_active: bool = Form(False),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        try:
            phone_local = normalize_phone(phone) if str(phone or "").strip() else None
            if str(phone or "").strip() and not phone_local:
                raise ValueError("Некорректный номер телефона")
            normalized_email = normalize_email(email) if str(email or "").strip() else None
            normalized_birth_date = normalize_birth_date(birth_date, required=False)
            status = str(client_status or "existing").strip()[:40] or "existing"
            with transaction(settings.db_path) as conn:
                row = _identity_row(conn, client_id)
                if not row:
                    raise ValueError("Игрок не найден")
                if phone_local:
                    assert_current_phone_available(conn, phone_local=phone_local, client_id=client_id)
                account_id = int(row["account_id"]) if row["account_id"] else None
                if account_id and not normalized_email:
                    raise ValueError("Для зарегистрированного аккаунта почта обязательна")
                if normalized_email and account_id:
                    conflict = conn.execute(
                        "SELECT id FROM member_accounts WHERE email_normalized=? AND id<>? LIMIT 1",
                        (normalized_email, account_id),
                    ).fetchone()
                    if conflict:
                        raise ValueError("Эта почта уже используется другим аккаунтом")
                full = full_phone(phone_local) if phone_local else None
                old_account_email = str(row["account_email_normalized"] or "")
                conn.execute(
                    """
                    UPDATE clients SET
                        first_name=?,nickname=?,username=?,phone_raw=?,phone_full=?,phone_local=?,
                        email=?,email_normalized=?,birth_date=?,app_user_id=?,referrer_app_user_id=?,
                        telegram_id=?,telegram_user_id=?,source=?,acquisition_campaign_code=?,
                        acquisition_source=?,client_status=?,updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (
                        first_name.strip()[:100] or None,
                        nickname.strip()[:100] or None,
                        username.strip().lstrip("@")[:100] or None,
                        full,
                        full,
                        phone_local,
                        normalized_email,
                        normalized_email,
                        normalized_birth_date,
                        app_user_id.strip()[:120] or None,
                        referrer_app_user_id.strip()[:120] or None,
                        telegram_id.strip()[:120] or None,
                        telegram_user_id.strip()[:120] or None,
                        source.strip()[:120] or "manual",
                        acquisition_campaign_code.strip()[:120] or None,
                        acquisition_source.strip()[:120] or None,
                        status,
                        client_id,
                    ),
                )
                if account_id:
                    conn.execute(
                        """
                        UPDATE member_accounts SET
                            email=?,email_normalized=?,is_active=?,
                            birth_date_required=?,updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                        """,
                        (
                            normalized_email,
                            normalized_email,
                            1 if account_active else 0,
                            0 if normalized_birth_date else int(row["birth_date_required"] or 0),
                            account_id,
                        ),
                    )
                    if not account_active or old_account_email != str(normalized_email or ""):
                        conn.execute(
                            "UPDATE member_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE account_id=? AND revoked_at IS NULL",
                            (account_id,),
                        )
                audit(
                    conn,
                    admin_id=int(request.session.get("admin_id")),
                    admin_name=str(request.session.get("admin_name") or "master"),
                    action="member_account_update",
                    entity_type="client",
                    entity_id=client_id,
                    details={"account_id": account_id},
                )
        except (ValueError, sqlite3.IntegrityError) as exc:
            message = str(exc)
            if isinstance(exc, sqlite3.IntegrityError):
                message = "Значение уже используется другим игроком"
            return _master_redirect(message, client_id=client_id, error=True)
        return _master_redirect("Учётные данные обновлены", client_id=client_id)

    @app.post("/master/member-accounts/{client_id}/reset-password")
    async def master_member_account_reset_password(
        request: Request,
        client_id: int,
        new_password: str = Form(...),
        new_password_confirmation: str = Form(...),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        if new_password != new_password_confirmation:
            return _master_redirect("Пароли не совпадают", client_id=client_id, error=True)
        try:
            encoded = hash_password(new_password)
            with transaction(settings.db_path) as conn:
                row = _identity_row(conn, client_id)
                if not row or not row["account_id"]:
                    raise ValueError("У игрока нет зарегистрированного аккаунта")
                account_id = int(row["account_id"])
                conn.execute(
                    """
                    UPDATE member_accounts
                    SET password_hash=?,session_version=session_version+1,updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (encoded, account_id),
                )
                conn.execute("DELETE FROM member_sessions WHERE account_id=?", (account_id,))
                audit(
                    conn,
                    admin_id=int(request.session.get("admin_id")),
                    admin_name=str(request.session.get("admin_name") or "master"),
                    action="member_password_reset",
                    entity_type="member_account",
                    entity_id=account_id,
                    details={"client_id": client_id},
                )
        except ValueError as exc:
            return _master_redirect(str(exc), client_id=client_id, error=True)
        return _master_redirect("Пароль сброшен. Все пользовательские сессии закрыты", client_id=client_id)

    return app


__all__ = [
    "assert_current_phone_available",
    "ensure_member_account_management_schema",
    "install_member_account_management",
    "normalize_birth_date",
]
