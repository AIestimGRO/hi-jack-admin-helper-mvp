from __future__ import annotations

import csv
import hmac
import io
import re
import secrets
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import qrcode
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import BASE_DIR, Settings
from app.db import connect, init_db, transaction
from app.services.clients import ensure_preferences
from app.services.auth import audit, authenticate, bootstrap_master, hash_pin, validate_username
from app.services.import_clients import FIELD_LABELS, detect_mapping, import_rows, read_tabular
from app.services.phone import display_phone
from app.services.preferences import change_counter, set_percent


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.validate()
        init_db(settings.db_path)
        with transaction(settings.db_path) as conn:
            bootstrap_master(
                conn,
                username=settings.master_login,
                display_name=settings.admin_name,
                pin=settings.admin_pin,
            )
        (BASE_DIR / "data" / "uploads").mkdir(parents=True, exist_ok=True)
        yield

    app = FastAPI(title="Hi Jack Club Admin Helper", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key or "development-secret-key-change-before-use",
        session_cookie="hjc_admin_session",
        max_age=settings.session_hours * 3600,
        same_site="lax",
        https_only=settings.secure_cookie,
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
    templates.env.globals["display_phone"] = display_phone

    @app.middleware("http")
    async def private_cache_control(request: Request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "private, no-store")
        return response

    def csrf(request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(32)
            request.session["csrf"] = token
        return token

    def context(request: Request, **values: Any) -> dict[str, Any]:
        return {
            "request": request,
            "csrf_token": csrf(request),
            "admin_name": request.session.get("admin_name", settings.admin_name),
            "admin_role": request.session.get("admin_role", ""),
            **values,
        }

    def authenticated(request: Request) -> bool:
        return bool(request.session.get("authenticated"))

    def require_auth(request: Request, api: bool = False) -> None:
        valid = authenticated(request)
        if valid:
            admin_id = request.session.get("admin_id")
            with connect(settings.db_path) as conn:
                current_admin = conn.execute(
                    "SELECT display_name, role, session_version FROM admins WHERE id = ? AND is_active = 1",
                    (admin_id,),
                ).fetchone()
            valid = bool(
                current_admin
                and current_admin["session_version"] == request.session.get("admin_session_version")
            )
            if valid:
                request.session["admin_name"] = current_admin["display_name"]
                request.session["admin_role"] = current_admin["role"]
        if not valid:
            request.session.clear()
            if api:
                raise HTTPException(status_code=401, detail="authentication_required")
            raise HTTPException(status_code=303, detail="login_required", headers={"Location": "/login"})

    def require_master(request: Request, api: bool = False) -> None:
        require_auth(request, api=api)
        if request.session.get("admin_role") != "master_admin":
            raise HTTPException(status_code=403, detail="Доступ только для мастер-администратора")

    def check_csrf(request: Request, token: str) -> None:
        expected = request.session.get("csrf", "")
        if not expected or not hmac.compare_digest(expected, token):
            raise HTTPException(status_code=403, detail="invalid_csrf_token")

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        if exc.status_code == 303 and exc.headers and exc.headers.get("Location"):
            return RedirectResponse(exc.headers["Location"], status_code=303)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        return templates.TemplateResponse(request, "error.html", context(request, status=exc.status_code, message=exc.detail), status_code=exc.status_code)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if authenticated(request):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", context(request, error=None))

    @app.post("/login")
    async def login(request: Request, username: str = Form(...), pin: str = Form(...), csrf_token: str = Form(...)):
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            admin = authenticate(conn, username, pin)
        if not admin:
            return templates.TemplateResponse(request, "login.html", context(request, error="Неверный логин или PIN"), status_code=401)
        request.session.clear()
        request.session.update({
            "authenticated": True,
            "admin_id": int(admin["id"]),
            "admin_name": admin["display_name"],
            "admin_role": admin["role"],
            "admin_session_version": admin["session_version"],
            "csrf": secrets.token_urlsafe(32),
        })
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request, csrf_token: str = Form(...)):
        require_auth(request)
        check_csrf(request, csrf_token)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        require_auth(request)
        with connect(settings.db_path) as conn:
            counts = {
                "clients": conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0],
                "operations": conn.execute("SELECT COUNT(*) FROM preference_log").fetchone()[0],
                "imports": conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0],
            }
            recent = conn.execute(
                """
                SELECT pl.*, c.first_name, c.nickname, pt.title, pt.kind
                FROM preference_log pl
                JOIN clients c ON c.id = pl.client_id
                JOIN preference_types pt ON pt.id = pl.preference_type_id
                ORDER BY pl.id DESC LIMIT 8
                """
            ).fetchall()
        return templates.TemplateResponse(request, "dashboard.html", context(request, counts=counts, recent=recent))

    @app.get("/master", response_class=HTMLResponse)
    async def master_page(request: Request, ok: str = "", error: str = ""):
        require_master(request)
        with connect(settings.db_path) as conn:
            preference_types = conn.execute(
                """
                SELECT pt.*, COUNT(cp.id) AS client_count,
                    COALESCE(SUM(CASE WHEN pt.kind='counter' THEN cp.balance_int ELSE 0 END), 0) AS total_balance
                FROM preference_types pt
                LEFT JOIN client_preferences cp ON cp.preference_type_id = pt.id
                GROUP BY pt.id ORDER BY pt.position, pt.id
                """
            ).fetchall()
            admins = conn.execute(
                "SELECT id, username, display_name, role, is_active, last_login_at, created_at FROM admins ORDER BY role DESC, display_name"
            ).fetchall()
            admin_audit = conn.execute(
                "SELECT * FROM admin_audit_log ORDER BY id DESC LIMIT 100"
            ).fetchall()
        return templates.TemplateResponse(
            request,
            "master.html",
            context(
                request,
                preference_types=preference_types,
                admins=admins,
                admin_audit=admin_audit,
                ok=ok,
                error=error,
            ),
        )

    def master_redirect(message: str, *, error: bool = False) -> RedirectResponse:
        parameter = "error" if error else "ok"
        return RedirectResponse(f"/master?{parameter}={message}", status_code=303)

    @app.post("/api/master/preferences/create")
    async def create_preference(
        request: Request,
        title: str = Form(...),
        kind: str = Form(...),
        code: str = Form(""),
        position: int = Form(100),
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        title = title.strip()
        code = code.strip().lower() or f"custom_{uuid.uuid4().hex[:10]}"
        if len(title) < 2 or len(title) > 80:
            return master_redirect("Название должно содержать от 2 до 80 символов", error=True)
        if kind not in {"counter", "percent"}:
            return master_redirect("Некорректный тип преференции", error=True)
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,49}", code):
            return master_redirect("Код: 3–50 латинских символов, цифр или _", error=True)
        position = max(0, min(position, 9999))
        try:
            with transaction(settings.db_path) as conn:
                cursor = conn.execute(
                    "INSERT INTO preference_types(code, title, kind, position, created_by_admin_id) VALUES (?, ?, ?, ?, ?)",
                    (code, title, kind, position, request.session["admin_id"]),
                )
                audit(
                    conn,
                    admin_id=request.session["admin_id"],
                    admin_name=request.session["admin_name"],
                    action="create",
                    entity_type="preference_type",
                    entity_id=int(cursor.lastrowid),
                    details={"code": code, "title": title, "kind": kind, "position": position},
                )
        except sqlite3.IntegrityError:
            return master_redirect("Преференция с таким кодом уже существует", error=True)
        return master_redirect("Преференция создана")

    @app.post("/api/master/preferences/{preference_id}/update")
    async def update_preference(
        request: Request,
        preference_id: int,
        title: str = Form(...),
        position: int = Form(100),
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        title = title.strip()
        if len(title) < 2 or len(title) > 80:
            return master_redirect("Некорректное название", error=True)
        position = max(0, min(position, 9999))
        with transaction(settings.db_path) as conn:
            row = conn.execute("SELECT * FROM preference_types WHERE id = ?", (preference_id,)).fetchone()
            if not row:
                return master_redirect("Преференция не найдена", error=True)
            conn.execute(
                "UPDATE preference_types SET title = ?, position = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (title, position, preference_id),
            )
            audit(
                conn,
                admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="update", entity_type="preference_type", entity_id=preference_id,
                details={"old_title": row["title"], "new_title": title, "old_position": row["position"], "new_position": position},
            )
        return master_redirect("Преференция обновлена")

    @app.post("/api/master/preferences/{preference_id}/toggle")
    async def toggle_preference(request: Request, preference_id: int, csrf_token: str = Form(...)):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            row = conn.execute("SELECT * FROM preference_types WHERE id = ?", (preference_id,)).fetchone()
            if not row:
                return master_redirect("Преференция не найдена", error=True)
            new_state = 0 if row["is_active"] else 1
            conn.execute("UPDATE preference_types SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_state, preference_id))
            audit(
                conn,
                admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="activate" if new_state else "deactivate", entity_type="preference_type", entity_id=preference_id,
                details={"code": row["code"], "title": row["title"]},
            )
        return master_redirect("Преференция включена" if new_state else "Преференция отключена")

    @app.post("/api/master/admins/create")
    async def create_admin(
        request: Request,
        username: str = Form(...),
        display_name: str = Form(...),
        pin: str = Form(...),
        role: str = Form("admin"),
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        display_name = display_name.strip()
        try:
            username = validate_username(username)
            pin_hash = hash_pin(pin)
        except ValueError as exc:
            return master_redirect("Проверьте логин и PIN: логин от 3 символов, PIN от 4", error=True)
        if not display_name or len(display_name) > 80 or role not in {"admin", "master_admin"}:
            return master_redirect("Некорректные данные администратора", error=True)
        try:
            with transaction(settings.db_path) as conn:
                cursor = conn.execute(
                    "INSERT INTO admins(username, display_name, pin_hash, role) VALUES (?, ?, ?, ?)",
                    (username, display_name, pin_hash, role),
                )
                audit(
                    conn,
                    admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                    action="create", entity_type="admin", entity_id=int(cursor.lastrowid),
                    details={"username": username, "display_name": display_name, "role": role},
                )
        except sqlite3.IntegrityError:
            return master_redirect("Такой логин уже существует", error=True)
        return master_redirect("Администратор создан")

    @app.post("/api/master/admins/{admin_id}/toggle")
    async def toggle_admin(request: Request, admin_id: int, csrf_token: str = Form(...)):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        if admin_id == request.session.get("admin_id"):
            return master_redirect("Нельзя отключить собственный аккаунт", error=True)
        with transaction(settings.db_path) as conn:
            row = conn.execute("SELECT * FROM admins WHERE id = ?", (admin_id,)).fetchone()
            if not row:
                return master_redirect("Администратор не найден", error=True)
            new_state = 0 if row["is_active"] else 1
            if row["role"] == "master_admin" and not new_state:
                active_masters = conn.execute("SELECT COUNT(*) FROM admins WHERE role='master_admin' AND is_active=1").fetchone()[0]
                if active_masters <= 1:
                    return master_redirect("Нельзя отключить последнего мастер-администратора", error=True)
            conn.execute("UPDATE admins SET is_active = ?, session_version = session_version + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_state, admin_id))
            audit(
                conn,
                admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="activate" if new_state else "deactivate", entity_type="admin", entity_id=admin_id,
                details={"username": row["username"]},
            )
        return master_redirect("Администратор включён" if new_state else "Администратор отключён")

    @app.post("/api/master/admins/{admin_id}/reset-pin")
    async def reset_admin_pin(request: Request, admin_id: int, pin: str = Form(...), csrf_token: str = Form(...)):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        try:
            pin_hash = hash_pin(pin)
        except ValueError:
            return master_redirect("PIN должен содержать от 4 до 128 символов", error=True)
        with transaction(settings.db_path) as conn:
            row = conn.execute("SELECT username FROM admins WHERE id = ?", (admin_id,)).fetchone()
            if not row:
                return master_redirect("Администратор не найден", error=True)
            conn.execute("UPDATE admins SET pin_hash = ?, session_version = session_version + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (pin_hash, admin_id))
            audit(
                conn,
                admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="reset_pin", entity_type="admin", entity_id=admin_id,
                details={"username": row["username"]},
            )
        return master_redirect("PIN обновлён")

    def search_clients(q: str, limit: int = 50):
        query = q.strip()
        digits = "".join(character for character in query if character.isdigit())
        like = f"%{query}%"
        phone_like = f"%{digits[-10:]}%" if digits else like
        with connect(settings.db_path) as conn:
            return conn.execute(
                """
                SELECT c.*,
                    COALESCE(MAX(CASE WHEN pt.code='free_entry' THEN cp.balance_int END), 0) AS free_entry,
                    COALESCE(MAX(CASE WHEN pt.code='free_reentry' THEN cp.balance_int END), 0) AS free_reentry,
                    COALESCE(MAX(CASE WHEN pt.code='free_addon' THEN cp.balance_int END), 0) AS free_addon
                FROM clients c
                LEFT JOIN client_preferences cp ON cp.client_id = c.id
                LEFT JOIN preference_types pt ON pt.id = cp.preference_type_id
                WHERE ? = '' OR c.phone_local LIKE ? OR COALESCE(c.phone_raw,'') LIKE ?
                    OR COALESCE(c.nickname,'') LIKE ? OR COALESCE(c.username,'') LIKE ?
                    OR COALESCE(c.first_name,'') LIKE ? OR COALESCE(c.app_user_id,'') LIKE ?
                    OR COALESCE(c.telegram_id,'') LIKE ?
                GROUP BY c.id ORDER BY c.updated_at DESC LIMIT ?
                """,
                (query, phone_like, like, like, like, like, like, like, limit),
            ).fetchall()

    @app.get("/clients", response_class=HTMLResponse)
    async def clients_page(request: Request, q: str = ""):
        require_auth(request)
        clients = search_clients(q) if q else []
        return templates.TemplateResponse(request, "clients.html", context(request, clients=clients, q=q))

    @app.get("/api/clients/search")
    async def clients_api(request: Request, q: str = ""):
        require_auth(request, api=True)
        rows = search_clients(q, limit=25)
        return [{key: row[key] for key in row.keys()} for row in rows]

    def load_client(client_id: int):
        with connect(settings.db_path) as conn:
            client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
            if not client:
                raise HTTPException(status_code=404, detail="Клиент не найден")
            ensure_preferences(conn, client_id)
            prefs = conn.execute(
                """
                SELECT cp.*, pt.code, pt.title, pt.kind
                FROM client_preferences cp JOIN preference_types pt ON pt.id = cp.preference_type_id
                WHERE cp.client_id = ? AND pt.is_active = 1 ORDER BY pt.id
                """,
                (client_id,),
            ).fetchall()
            logs = conn.execute(
                """
                SELECT pl.*, pt.title, pt.kind FROM preference_log pl
                JOIN preference_types pt ON pt.id = pl.preference_type_id
                WHERE pl.client_id = ? ORDER BY pl.id DESC LIMIT 100
                """,
                (client_id,),
            ).fetchall()
        return client, prefs, logs

    @app.get("/clients/{client_id:int}", response_class=HTMLResponse)
    async def client_detail(request: Request, client_id: int, ok: str = "", error: str = ""):
        require_auth(request)
        client, prefs, logs = load_client(client_id)
        return templates.TemplateResponse(
            request,
            "client_detail.html",
            context(request, client=client, preferences=prefs, logs=logs, ok=ok, error=error),
        )

    @app.get("/api/clients/{client_id}/qr")
    async def client_qr(request: Request, client_id: int):
        require_auth(request, api=True)
        with connect(settings.db_path) as conn:
            row = conn.execute("SELECT phone_local FROM clients WHERE id = ?", (client_id,)).fetchone()
        if not row or not row["phone_local"]:
            raise HTTPException(status_code=404, detail="qr_phone_unavailable")
        image = qrcode.make(row["phone_local"], border=3, box_size=12)
        output = io.BytesIO()
        image.save(output, format="PNG")
        return Response(output.getvalue(), media_type="image/png", headers={"Cache-Control": "private, no-store"})

    @app.post("/api/preferences/add")
    async def preference_add(
        request: Request,
        client_id: int = Form(...),
        code: str = Form(...),
        amount: int = Form(...),
        reason: str = Form(...),
        comment: str = Form(""),
        csrf_token: str = Form(...),
    ):
        require_auth(request, api=True)
        check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                ensure_preferences(conn, client_id)
                change_counter(conn, client_id=client_id, code=code, delta=abs(amount), reason=reason, comment=comment, admin_name=request.session["admin_name"])
        except ValueError as exc:
            return RedirectResponse(f"/clients/{client_id}?error={exc}", status_code=303)
        return RedirectResponse(f"/clients/{client_id}?ok=Начисление+сохранено", status_code=303)

    @app.post("/api/preferences/spend")
    async def preference_spend(
        request: Request,
        client_id: int = Form(...),
        code: str = Form(...),
        amount: int = Form(...),
        reason: str = Form(...),
        comment: str = Form(""),
        csrf_token: str = Form(...),
    ):
        require_auth(request, api=True)
        check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                ensure_preferences(conn, client_id)
                change_counter(conn, client_id=client_id, code=code, delta=-abs(amount), reason=reason, comment=comment, admin_name=request.session["admin_name"])
        except ValueError as exc:
            return RedirectResponse(f"/clients/{client_id}?error={exc}", status_code=303)
        return RedirectResponse(f"/clients/{client_id}?ok=Списание+сохранено", status_code=303)

    @app.post("/api/preferences/set-discount")
    async def preference_discount(
        request: Request,
        client_id: int = Form(...),
        code: str = Form(...),
        percent: float = Form(...),
        reason: str = Form(...),
        comment: str = Form(""),
        csrf_token: str = Form(...),
    ):
        require_auth(request, api=True)
        check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                ensure_preferences(conn, client_id)
                set_percent(conn, client_id=client_id, code=code, percent=percent, reason=reason, comment=comment, admin_name=request.session["admin_name"])
        except ValueError as exc:
            return RedirectResponse(f"/clients/{client_id}?error={exc}", status_code=303)
        return RedirectResponse(f"/clients/{client_id}?ok=Скидка+сохранена", status_code=303)

    @app.post("/api/clients/{client_id}/comment")
    async def client_comment(request: Request, client_id: int, comment: str = Form(""), csrf_token: str = Form(...)):
        require_auth(request, api=True)
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            conn.execute("UPDATE clients SET comment = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (comment.strip(), client_id))
        return RedirectResponse(f"/clients/{client_id}?ok=Комментарий+сохранён", status_code=303)

    @app.get("/clients/import", response_class=HTMLResponse)
    async def import_page(request: Request):
        require_auth(request)
        return templates.TemplateResponse(request, "import.html", context(request, preview=None, result=None, fields=FIELD_LABELS))

    @app.post("/clients/import", response_class=HTMLResponse)
    async def import_preview(request: Request, upload: UploadFile = File(...), csrf_token: str = Form(...)):
        require_auth(request)
        check_csrf(request, csrf_token)
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in {".csv", ".xlsx"}:
            raise HTTPException(status_code=400, detail="Поддерживаются только CSV и XLSX")
        content = await upload.read(settings.max_upload_mb * 1024 * 1024 + 1)
        if len(content) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Файл слишком большой")
        upload_dir = BASE_DIR / "data" / "uploads"
        saved_name = f"{uuid.uuid4().hex}{suffix}"
        path = upload_dir / saved_name
        path.write_bytes(content)
        try:
            headers, rows = read_tabular(path)
        except Exception:
            path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Не удалось прочитать файл")
        preview = {
            "token": saved_name,
            "filename": Path(upload.filename or "import").name,
            "headers": headers,
            "rows": rows[:10],
            "total": len(rows),
            "mapping": detect_mapping(headers),
        }
        return templates.TemplateResponse(request, "import.html", context(request, preview=preview, result=None, fields=FIELD_LABELS))

    @app.post("/api/import/clients", response_class=HTMLResponse)
    async def import_commit(request: Request):
        require_auth(request, api=True)
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        token = Path(str(form.get("token", ""))).name
        filename = Path(str(form.get("filename", "import"))).name
        path = BASE_DIR / "data" / "uploads" / token
        if not path.exists() or path.suffix.lower() not in {".csv", ".xlsx"}:
            raise HTTPException(status_code=400, detail="Файл предпросмотра не найден")
        mapping = {field: str(form.get(f"map_{field}", "")) for field in FIELD_LABELS}
        if not mapping.get("phone_raw") and not mapping.get("app_user_id"):
            raise HTTPException(status_code=400, detail="Укажите колонку Phone или ID")
        headers, rows = read_tabular(path)
        if any(column and column not in headers for column in mapping.values()):
            raise HTTPException(status_code=400, detail="Некорректное сопоставление колонок")
        with transaction(settings.db_path) as conn:
            stats = import_rows(conn, rows, mapping)
            conn.execute(
                """
                INSERT INTO import_batches(filename, total_rows, inserted_count, updated_count,
                    skipped_count, phone_error_count, duplicate_count, admin_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (filename, stats["total"], stats["inserted"], stats["updated"], stats["skipped"], stats["phone_errors"], stats["duplicates"], request.session["admin_name"]),
            )
        path.unlink(missing_ok=True)
        return templates.TemplateResponse(request, "import.html", context(request, preview=None, result=stats, fields=FIELD_LABELS))

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(
        request: Request,
        q: str = "",
        preference: str = "",
        operation: str = "",
        admin: str = "",
        date_from: str = "",
        date_to: str = "",
    ):
        require_auth(request)
        clauses = ["1=1"]
        params: list[Any] = []
        if q:
            clauses.append("(c.first_name LIKE ? OR c.nickname LIKE ? OR c.phone_local LIKE ?)")
            params.extend([f"%{q}%"] * 3)
        if preference:
            clauses.append("pt.code = ?")
            params.append(preference)
        if operation:
            clauses.append("pl.operation_type = ?")
            params.append(operation)
        if admin:
            clauses.append("pl.admin_name LIKE ?")
            params.append(f"%{admin}%")
        if date_from:
            clauses.append("date(pl.created_at) >= date(?)")
            params.append(date_from)
        if date_to:
            clauses.append("date(pl.created_at) <= date(?)")
            params.append(date_to)
        with connect(settings.db_path) as conn:
            logs = conn.execute(
                f"""
                SELECT pl.*, c.first_name, c.nickname, c.phone_local, pt.title, pt.code, pt.kind
                FROM preference_log pl JOIN clients c ON c.id = pl.client_id
                JOIN preference_types pt ON pt.id = pl.preference_type_id
                WHERE {' AND '.join(clauses)} ORDER BY pl.id DESC LIMIT 500
                """,
                params,
            ).fetchall()
            preference_types = conn.execute(
                "SELECT code, title FROM preference_types ORDER BY position, id"
            ).fetchall()
        filters = {"q": q, "preference": preference, "operation": operation, "admin": admin, "date_from": date_from, "date_to": date_to}
        return templates.TemplateResponse(request, "logs.html", context(request, logs=logs, filters=filters, preference_types=preference_types))

    def csv_response(filename: str, headers: list[str], rows):
        def safe_cell(value: Any) -> Any:
            if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
                return "'" + value
            return value

        output = io.StringIO()
        output.write("\ufeff")
        writer = csv.writer(output, delimiter=";")
        writer.writerow(headers)
        writer.writerows([safe_cell(value) for value in row] for row in rows)
        return StreamingResponse(iter([output.getvalue()]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/api/export/clients.csv")
    async def export_clients(request: Request):
        require_auth(request, api=True)
        with connect(settings.db_path) as conn:
            rows = conn.execute("SELECT app_user_id, telegram_id, referrer_app_user_id, nickname, username, first_name, phone_raw, phone_local, source, comment, created_at, updated_at FROM clients ORDER BY id").fetchall()
        headers = ["ID", "Telegram ID", "Referrer ID", "Nickname", "Username", "First name", "Phone", "Phone local", "Source", "Comment", "Created at", "Updated at"]
        return csv_response("clients.csv", headers, ([row[key] for key in row.keys()] for row in rows))

    @app.get("/api/export/preference-log.csv")
    async def export_logs(request: Request):
        require_auth(request, api=True)
        with connect(settings.db_path) as conn:
            rows = conn.execute(
                """
                SELECT pl.created_at, c.first_name, c.nickname, c.phone_local, pt.title,
                    pl.operation_type, pl.delta_int, pl.old_balance_int, pl.new_balance_int,
                    pl.old_percent_value, pl.new_percent_value, pl.reason, pl.comment, pl.admin_name
                FROM preference_log pl JOIN clients c ON c.id=pl.client_id
                JOIN preference_types pt ON pt.id=pl.preference_type_id ORDER BY pl.id
                """
            ).fetchall()
        headers = ["Date", "First name", "Nickname", "Phone", "Preference", "Operation", "Delta", "Old balance", "New balance", "Old percent", "New percent", "Reason", "Comment", "Admin"]
        return csv_response("preference_log.csv", headers, ([row[key] for key in row.keys()] for row in rows))

    return app


app = create_app()
