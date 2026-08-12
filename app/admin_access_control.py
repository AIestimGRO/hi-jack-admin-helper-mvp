from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.db import connect, transaction
from app.product_shell import _check_csrf
from app.services.auth import audit, hash_pin, validate_username
from app.services.member_accounts import hash_password
from app.services.vault import redeem_reward


ACCESS_MASTER = "master"
ACCESS_QUIZ_MANAGER = "quiz_manager"
ACCESS_BARTENDER = "bartender"
ACCESS_ROLES = frozenset({ACCESS_MASTER, ACCESS_QUIZ_MANAGER, ACCESS_BARTENDER})
ACCESS_LABELS = {
    ACCESS_MASTER: "Мастер-администратор",
    ACCESS_QUIZ_MANAGER: "Квиз-администратор",
    ACCESS_BARTENDER: "Бармен",
}


def ensure_admin_access_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_access_profiles (
            admin_id INTEGER PRIMARY KEY REFERENCES admins(id) ON DELETE CASCADE,
            access_role TEXT NOT NULL
                CHECK(access_role IN ('quiz_manager','bartender')),
            created_by_admin_id INTEGER REFERENCES admins(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_admin_access_profiles_role
        ON admin_access_profiles(access_role, admin_id)
        """
    )


def effective_access_role(
    conn: sqlite3.Connection,
    *,
    admin_id: int,
    base_role: str | None = None,
) -> str:
    if base_role is None:
        admin = conn.execute(
            "SELECT role FROM admins WHERE id=?",
            (admin_id,),
        ).fetchone()
        if not admin:
            return ACCESS_BARTENDER
        base_role = str(admin["role"] or "admin")
    profile = conn.execute(
        "SELECT access_role FROM admin_access_profiles WHERE admin_id=?",
        (admin_id,),
    ).fetchone()
    profile_role = str(profile["access_role"] or "") if profile else ""
    if profile_role == ACCESS_QUIZ_MANAGER and base_role == "master_admin":
        return ACCESS_QUIZ_MANAGER
    if base_role == "master_admin":
        return ACCESS_MASTER
    return ACCESS_BARTENDER


def _role_from_request(request: Request) -> str:
    role = str(request.session.get("admin_access_role") or "")
    if role in ACCESS_ROLES:
        return role
    if request.session.get("admin_role") == "master_admin":
        return ACCESS_MASTER
    return ACCESS_BARTENDER


def _is_public_admin_asset(path: str) -> bool:
    return (
        path.startswith("/static/")
        or path.startswith("/quiz-media/")
        or path.startswith("/reward-media/")
        or path.startswith("/health")
        or path in {"/login", "/logout"}
    )


def bartender_path_allowed(path: str, method: str) -> bool:
    method = method.upper()
    if _is_public_admin_asset(path):
        return True
    if method == "GET" and (path == "/clients" or path.startswith("/clients/")):
        return True
    if method == "GET" and path.startswith("/api/clients/") and path.endswith("/qr"):
        return True
    if method == "POST" and path in {
        "/api/preferences/add",
        "/api/preferences/spend",
        "/api/preferences/set-discount",
        "/staff/redeem",
    }:
        return True
    if method == "GET" and path == "/staff/redeem":
        return True
    return False


def manager_path_allowed(path: str, method: str, query: dict[str, str] | None = None) -> bool:
    if bartender_path_allowed(path, method):
        return True
    method = method.upper()
    query = query or {}
    if _is_public_admin_asset(path):
        return True
    if path == "/" and method == "GET":
        return True
    if path.startswith("/clients/") or path == "/clients":
        return method in {"GET", "POST"}
    if path.startswith("/api/clients/"):
        return True
    if path.startswith("/admin/quiz") or path.startswith("/api/admin/quiz"):
        return True
    if path.startswith("/admin/vault") or path.startswith("/api/vault/"):
        return True
    if path.startswith("/admin/rewards") or path.startswith("/api/rewards/"):
        return True
    if path.startswith("/master/quiz-builder"):
        return True
    if path.startswith("/master/jackside-issues"):
        return True
    if path.startswith("/master/engagement-icons"):
        return True
    if path.startswith("/api/master/quiz"):
        return True
    if path.startswith("/api/master/jackside"):
        return True
    if path.startswith("/api/master/preferences"):
        return True
    if path.startswith("/api/master/engagement"):
        return True
    if path.startswith("/staff-access") or path.startswith("/api/staff-access"):
        return True
    if path.startswith("/staff-users") or path.startswith("/api/staff-users"):
        return True
    if path == "/logs" and method == "GET":
        return True
    if path == "/master" and method == "GET":
        return str(query.get("tab") or "preferences") in {
            "preferences",
            "campaigns",
            "analytics",
            "engagement",
        }
    return False


def access_path_allowed(
    access_role: str,
    *,
    path: str,
    method: str,
    query: dict[str, str] | None = None,
) -> bool:
    if access_role == ACCESS_MASTER:
        return True
    if access_role == ACCESS_QUIZ_MANAGER:
        return manager_path_allowed(path, method, query)
    return bartender_path_allowed(path, method)


def _require_access(request: Request, *roles: str) -> str:
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401, detail="authentication_required")
    role = _role_from_request(request)
    if role not in roles:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return role


def _staff_redirect(message: str, *, error: bool = False) -> RedirectResponse:
    return RedirectResponse(
        f"/staff-access?{urlencode({'error' if error else 'ok': message})}",
        status_code=303,
    )


def _user_redirect(message: str, *, error: bool = False) -> RedirectResponse:
    return RedirectResponse(
        f"/staff-users?{urlencode({'error' if error else 'ok': message})}",
        status_code=303,
    )


def _redeem_redirect(message: str, *, error: bool = False) -> RedirectResponse:
    return RedirectResponse(
        f"/staff/redeem?{urlencode({'error' if error else 'ok': message})}",
        status_code=303,
    )


def _manageable_by(actor_role: str, target_role: str) -> bool:
    if actor_role == ACCESS_MASTER:
        return target_role != ACCESS_MASTER
    if actor_role == ACCESS_QUIZ_MANAGER:
        return target_role == ACCESS_BARTENDER
    return False


def _staff_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT a.id,a.username,a.display_name,a.role,a.is_active,a.session_version,
               a.last_login_at,a.created_at,p.access_role
        FROM admins a
        LEFT JOIN admin_access_profiles p ON p.admin_id=a.id
        ORDER BY CASE WHEN a.role='master_admin' AND p.access_role IS NULL THEN 0
                      WHEN p.access_role='quiz_manager' THEN 1 ELSE 2 END,
                 a.is_active DESC,a.display_name COLLATE NOCASE,a.id
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["effective_role"] = effective_access_role(
            conn,
            admin_id=int(row["id"]),
            base_role=str(row["role"]),
        )
        item["role_label"] = ACCESS_LABELS[item["effective_role"]]
        result.append(item)
    return result


def install_admin_access_control(app: FastAPI) -> FastAPI:
    if getattr(app.state, "admin_access_control_installed", False):
        return app
    app.state.admin_access_control_installed = True
    settings = app.state.settings
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

    with transaction(settings.db_path) as conn:
        ensure_admin_access_schema(conn)

    @app.middleware("http")
    async def scoped_admin_access_middleware(request: Request, call_next):
        if not request.session.get("authenticated"):
            return await call_next(request)
        admin_id = request.session.get("admin_id")
        if not admin_id:
            return await call_next(request)
        with connect(settings.db_path) as conn:
            ensure_admin_access_schema(conn)
            admin = conn.execute(
                "SELECT role,is_active,session_version FROM admins WHERE id=?",
                (int(admin_id),),
            ).fetchone()
            if not admin or not int(admin["is_active"] or 0):
                request.session.clear()
                return RedirectResponse("/login", status_code=303)
            access_role = effective_access_role(
                conn,
                admin_id=int(admin_id),
                base_role=str(admin["role"]),
            )
        request.session["admin_access_role"] = access_role
        request.session["admin_access_label"] = ACCESS_LABELS[access_role]

        path = request.url.path
        if access_role == ACCESS_BARTENDER and path == "/" and request.method == "GET":
            return RedirectResponse("/clients", status_code=303)
        if access_path_allowed(
            access_role,
            path=path,
            method=request.method,
            query=dict(request.query_params),
        ):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"error": "forbidden_for_role"}, status_code=403)
        return HTMLResponse(
            "Доступ к этому разделу не входит в вашу роль администратора.",
            status_code=403,
        )

    @app.get("/staff-access", response_class=HTMLResponse)
    async def staff_access_page(
        request: Request,
        ok: str = "",
        error: str = "",
    ):
        actor_role = _require_access(request, ACCESS_MASTER, ACCESS_QUIZ_MANAGER)
        with connect(settings.db_path) as conn:
            rows = _staff_rows(conn)
        return templates.TemplateResponse(
            request,
            "staff_access.html",
            {
                "request": request,
                "rows": rows,
                "actor_role": actor_role,
                "ok": ok,
                "error": error,
                "csrf_token": request.session.get("csrf", ""),
                "admin_name": request.session.get("admin_name", "Администратор"),
                "admin_role": request.session.get("admin_role", "admin"),
                "asset_version": "staff-access-v1",
            },
        )

    @app.post("/api/staff-access/create")
    async def staff_access_create(
        request: Request,
        username: str = Form(...),
        display_name: str = Form(...),
        pin: str = Form(...),
        access_role: str = Form(...),
        csrf_token: str = Form(...),
    ):
        actor_role = _require_access(request, ACCESS_MASTER, ACCESS_QUIZ_MANAGER)
        _check_csrf(request, csrf_token)
        requested = str(access_role or "").strip()
        allowed = (
            {ACCESS_QUIZ_MANAGER, ACCESS_BARTENDER}
            if actor_role == ACCESS_MASTER
            else {ACCESS_BARTENDER}
        )
        if requested not in allowed:
            return _staff_redirect("Эту роль вы создавать не можете", error=True)
        try:
            clean_username = validate_username(username)
            clean_name = " ".join(str(display_name or "").split())[:80]
            if not clean_name:
                raise ValueError("Укажите имя сотрудника")
            encoded_pin = hash_pin(pin)
            base_role = "master_admin" if requested == ACCESS_QUIZ_MANAGER else "admin"
            with transaction(settings.db_path) as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO admins(username,display_name,pin_hash,role)
                    VALUES (?,?,?,?)
                    """,
                    (clean_username, clean_name, encoded_pin, base_role),
                )
                new_id = int(cursor.lastrowid)
                conn.execute(
                    """
                    INSERT INTO admin_access_profiles(admin_id,access_role,created_by_admin_id)
                    VALUES (?,?,?)
                    """,
                    (new_id, requested, int(request.session.get("admin_id"))),
                )
                audit(
                    conn,
                    admin_id=int(request.session.get("admin_id")),
                    admin_name=str(request.session.get("admin_name") or "admin"),
                    action="scoped_admin_created",
                    entity_type="admin",
                    entity_id=new_id,
                    details={"access_role": requested, "username": clean_username},
                )
        except sqlite3.IntegrityError:
            return _staff_redirect("Такой логин уже существует", error=True)
        except ValueError as exc:
            return _staff_redirect(str(exc), error=True)
        return _staff_redirect(f"Создан сотрудник: {ACCESS_LABELS[requested]}")

    @app.post("/api/staff-access/{admin_id}/reset-pin")
    async def staff_access_reset_pin(
        request: Request,
        admin_id: int,
        pin: str = Form(...),
        csrf_token: str = Form(...),
    ):
        actor_role = _require_access(request, ACCESS_MASTER, ACCESS_QUIZ_MANAGER)
        _check_csrf(request, csrf_token)
        try:
            encoded = hash_pin(pin)
            with transaction(settings.db_path) as conn:
                target = conn.execute(
                    "SELECT id,role,username FROM admins WHERE id=?",
                    (admin_id,),
                ).fetchone()
                if not target:
                    raise ValueError("Сотрудник не найден")
                target_role = effective_access_role(
                    conn,
                    admin_id=admin_id,
                    base_role=str(target["role"]),
                )
                if not _manageable_by(actor_role, target_role):
                    raise ValueError("Недостаточно прав для этого сотрудника")
                conn.execute(
                    """
                    UPDATE admins SET pin_hash=?,session_version=session_version+1,
                        updated_at=CURRENT_TIMESTAMP WHERE id=?
                    """,
                    (encoded, admin_id),
                )
                audit(
                    conn,
                    admin_id=int(request.session.get("admin_id")),
                    admin_name=str(request.session.get("admin_name") or "admin"),
                    action="scoped_admin_pin_reset",
                    entity_type="admin",
                    entity_id=admin_id,
                    details={"target_role": target_role},
                )
        except ValueError as exc:
            return _staff_redirect(str(exc), error=True)
        return _staff_redirect("PIN сотрудника изменён; старые сессии закрыты")

    @app.post("/api/staff-access/{admin_id}/toggle")
    async def staff_access_toggle(
        request: Request,
        admin_id: int,
        csrf_token: str = Form(...),
    ):
        actor_role = _require_access(request, ACCESS_MASTER, ACCESS_QUIZ_MANAGER)
        _check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                target = conn.execute(
                    "SELECT id,role,is_active FROM admins WHERE id=?",
                    (admin_id,),
                ).fetchone()
                if not target:
                    raise ValueError("Сотрудник не найден")
                target_role = effective_access_role(
                    conn,
                    admin_id=admin_id,
                    base_role=str(target["role"]),
                )
                if not _manageable_by(actor_role, target_role):
                    raise ValueError("Недостаточно прав для этого сотрудника")
                new_state = 0 if int(target["is_active"] or 0) else 1
                conn.execute(
                    """
                    UPDATE admins SET is_active=?,session_version=session_version+1,
                        updated_at=CURRENT_TIMESTAMP WHERE id=?
                    """,
                    (new_state, admin_id),
                )
                audit(
                    conn,
                    admin_id=int(request.session.get("admin_id")),
                    admin_name=str(request.session.get("admin_name") or "admin"),
                    action="scoped_admin_toggled",
                    entity_type="admin",
                    entity_id=admin_id,
                    details={"target_role": target_role, "is_active": bool(new_state)},
                )
        except ValueError as exc:
            return _staff_redirect(str(exc), error=True)
        return _staff_redirect("Доступ сотрудника обновлён")

    @app.get("/staff-users", response_class=HTMLResponse)
    async def staff_users_page(
        request: Request,
        q: str = "",
        ok: str = "",
        error: str = "",
    ):
        _require_access(request, ACCESS_MASTER, ACCESS_QUIZ_MANAGER)
        search = " ".join(str(q or "").split())[:120]
        params: list[Any] = []
        where = "WHERE ma.id IS NOT NULL"
        if search:
            like = f"%{search}%"
            where += """
                AND (CAST(c.id AS TEXT) LIKE ? OR COALESCE(c.first_name,'') LIKE ?
                     OR COALESCE(c.nickname,'') LIKE ? OR COALESCE(c.username,'') LIKE ?
                     OR COALESCE(c.phone_local,'') LIKE ? OR COALESCE(ma.email,'') LIKE ?)
            """
            params = [like] * 6
        with connect(settings.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT c.id,c.first_name,c.nickname,c.username,c.phone_local,c.client_status,
                       ma.id AS account_id,ma.email,ma.is_active,ma.last_login_at
                FROM clients c
                JOIN member_accounts ma ON ma.client_id=c.id
                {where}
                ORDER BY c.updated_at DESC,c.id DESC
                LIMIT 100
                """,
                params,
            ).fetchall()
        return templates.TemplateResponse(
            request,
            "staff_users.html",
            {
                "request": request,
                "rows": rows,
                "q": search,
                "ok": ok,
                "error": error,
                "csrf_token": request.session.get("csrf", ""),
                "admin_name": request.session.get("admin_name", "Администратор"),
                "admin_role": request.session.get("admin_role", "admin"),
                "asset_version": "staff-users-v1",
            },
        )

    @app.post("/api/staff-users/{client_id}/reset-password")
    async def staff_user_reset_password(
        request: Request,
        client_id: int,
        new_password: str = Form(...),
        new_password_confirmation: str = Form(...),
        csrf_token: str = Form(...),
    ):
        _require_access(request, ACCESS_MASTER, ACCESS_QUIZ_MANAGER)
        _check_csrf(request, csrf_token)
        if new_password != new_password_confirmation:
            return _user_redirect("Пароли не совпадают", error=True)
        try:
            encoded = hash_password(new_password)
            with transaction(settings.db_path) as conn:
                row = conn.execute(
                    "SELECT id FROM member_accounts WHERE client_id=? AND is_active=1",
                    (client_id,),
                ).fetchone()
                if not row:
                    raise ValueError("У игрока нет активного зарегистрированного ЛК")
                account_id = int(row["id"])
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
                    admin_name=str(request.session.get("admin_name") or "admin"),
                    action="member_password_reset_by_support_admin",
                    entity_type="member_account",
                    entity_id=account_id,
                    details={"client_id": client_id, "access_role": _role_from_request(request)},
                )
        except ValueError as exc:
            return _user_redirect(str(exc), error=True)
        return _user_redirect("Пароль изменён. Все прежние сессии пользователя закрыты")

    @app.get("/staff/redeem", response_class=HTMLResponse)
    async def staff_redeem_page(
        request: Request,
        ok: str = "",
        error: str = "",
    ):
        _require_access(request, ACCESS_MASTER, ACCESS_QUIZ_MANAGER, ACCESS_BARTENDER)
        return templates.TemplateResponse(
            request,
            "staff_redeem.html",
            {
                "request": request,
                "ok": ok,
                "error": error,
                "csrf_token": request.session.get("csrf", ""),
                "admin_name": request.session.get("admin_name", "Администратор"),
                "admin_role": request.session.get("admin_role", "admin"),
                "asset_version": "staff-redeem-v1",
            },
        )

    @app.post("/staff/redeem")
    async def staff_redeem(
        request: Request,
        code: str = Form(...),
        csrf_token: str = Form(...),
    ):
        _require_access(request, ACCESS_MASTER, ACCESS_QUIZ_MANAGER, ACCESS_BARTENDER)
        _check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                reward = redeem_reward(
                    conn,
                    code=code,
                    admin_id=int(request.session.get("admin_id")),
                    admin_name=str(request.session.get("admin_name") or "admin"),
                )
                catalog = conn.execute(
                    "SELECT title FROM vault_catalog_rewards WHERE id=?",
                    (int(reward["catalog_reward_id"]),),
                ).fetchone()
                title = str(catalog["title"] or "Награда") if catalog else "Награда"
        except ValueError as exc:
            messages = {
                "vault_reward_not_found": "Карта не найдена",
                "vault_reward_not_activated": "Игрок ещё не активировал карту",
                "vault_reward_activation_expired": "Короткий код уже истёк",
                "vault_reward_redeemed": "Карта уже погашена",
                "vault_reward_expired": "Срок карты истёк",
                "vault_reward_cancelled": "Карта отменена",
            }
            return _redeem_redirect(messages.get(str(exc), str(exc)), error=True)
        return _redeem_redirect(f"Карта погашена: {title}")

    return app


__all__ = [
    "ACCESS_BARTENDER",
    "ACCESS_MASTER",
    "ACCESS_QUIZ_MANAGER",
    "ACCESS_LABELS",
    "access_path_allowed",
    "effective_access_role",
    "ensure_admin_access_schema",
    "install_admin_access_control",
]
