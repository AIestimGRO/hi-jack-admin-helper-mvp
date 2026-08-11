from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.db import connect, transaction
from app.member_account_management import _identity_row
from app.product_shell import _check_csrf, _current_member, _require_master
from app.services.auth import audit
from app.services.phone import display_phone


LEGAL_VERSION = "2026-08-11"
LEGAL_DIR = BASE_DIR / "data" / "legal"
_SCHEMA_LOCK = threading.Lock()

MANDATORY_DOCUMENTS = {
    # Keep the historic internal codes because the existing registration engine and
    # consent migration use them. The user-facing titles are the legal meaning.
    "privacy": {
        "title": "Пользовательское соглашение и Правила Hi, Jack! Club",
        "path": "01_user_agreement.txt",
        "public_code": "user-agreement",
    },
    "rewards": {
        "title": "Согласие на обработку персональных данных",
        "path": "03_personal_data_consent.txt",
        "public_code": "personal-data-consent",
    },
}

REFERENCE_DOCUMENTS = {
    "privacy-policy": {
        "title": "Политика в отношении обработки и защиты персональных данных",
        "path": "02_privacy_policy.txt",
        "kind": "policy",
    },
    "marketing-consent": {
        "title": "Согласие на получение рекламных и информационных сообщений",
        "path": "04_marketing_consent.txt",
        "kind": "optional_consent",
    },
    "image-consent": {
        "title": "Согласие на использование изображения",
        "path": "05_image_consent.txt",
        "kind": "optional_consent",
    },
    "public-rating-consent": {
        "title": "Согласие на распространение персональных данных для публичного рейтинга",
        "path": "06_public_rating_consent.txt",
        "kind": "optional_consent",
    },
}

QUIZ_MEMBER_API_PATHS = frozenset(
    {
        "/api/quiz/questions",
        "/api/quiz/start",
        "/api/quiz/answer",
        "/api/quiz/finish",
        "/api/quiz/final-table/status",
        "/api/quiz/final-table/answer",
        "/api/quiz/identity",
        "/api/quiz/identity/confirm",
        "/api/quiz/identity/forget",
        "/api/quiz/email/request",
        "/api/quiz/email/verify",
        "/quiz/telegram/start",
        "/quiz/telegram/callback",
    }
)

RATING_CATEGORY_KEYS = (
    "nickname",
    "avatar",
    "result",
    "place",
    "achievements",
    "titles",
    "participation_stats",
)


def _read_legal_file(filename: str) -> str:
    path = LEGAL_DIR / filename
    return path.read_text(encoding="utf-8").strip()


def _ip_hash(settings, request: Request) -> str:
    value = request.client.host if request.client else "unknown"
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        f"legal:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _is_adult(value: str) -> bool:
    try:
        born = date.fromisoformat(str(value or ""))
    except ValueError:
        return False
    today = date.today()
    years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return years >= 18


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def ensure_legal_registration_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS legal_reference_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            version TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'reference',
            is_active INTEGER NOT NULL DEFAULT 1,
            published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(code, version)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_legal_reference_documents_active
            ON legal_reference_documents(code) WHERE is_active=1;

        CREATE TABLE IF NOT EXISTS member_optional_consent_state (
            account_id INTEGER NOT NULL REFERENCES member_accounts(id) ON DELETE CASCADE,
            code TEXT NOT NULL,
            document_version TEXT NOT NULL,
            granted INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(account_id, code)
        );

        CREATE TABLE IF NOT EXISTS member_optional_consent_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES member_accounts(id) ON DELETE CASCADE,
            code TEXT NOT NULL,
            document_version TEXT NOT NULL,
            granted INTEGER NOT NULL,
            ip_hash TEXT NOT NULL,
            user_agent TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_member_optional_consent_events_account
            ON member_optional_consent_events(account_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS member_public_rating_consent_state (
            account_id INTEGER PRIMARY KEY REFERENCES member_accounts(id) ON DELETE CASCADE,
            document_version TEXT NOT NULL,
            categories_json TEXT NOT NULL DEFAULT '{}',
            granted INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS member_public_rating_consent_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES member_accounts(id) ON DELETE CASCADE,
            document_version TEXT NOT NULL,
            categories_json TEXT NOT NULL,
            granted INTEGER NOT NULL,
            ip_hash TEXT NOT NULL,
            user_agent TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_member_public_rating_consent_events_account
            ON member_public_rating_consent_events(account_id, created_at DESC);
        """
    )
    _seed_documents(conn)


def _seed_documents(conn: sqlite3.Connection) -> None:
    for code, meta in MANDATORY_DOCUMENTS.items():
        content = _read_legal_file(meta["path"])
        current = conn.execute(
            "SELECT id,version,title,content FROM legal_documents WHERE code=? AND is_active=1",
            (code,),
        ).fetchone()
        if (
            current
            and str(current["version"]) == LEGAL_VERSION
            and str(current["title"]) == meta["title"]
            and str(current["content"]).strip() == content
        ):
            continue
        conn.execute("UPDATE legal_documents SET is_active=0 WHERE code=?", (code,))
        conn.execute(
            """
            INSERT INTO legal_documents(code,version,title,content,is_active,published_at)
            VALUES (?,?,?,?,1,CURRENT_TIMESTAMP)
            ON CONFLICT(code,version) DO UPDATE SET
                title=excluded.title,
                content=excluded.content,
                is_active=1,
                published_at=CURRENT_TIMESTAMP
            """,
            (code, LEGAL_VERSION, meta["title"], content),
        )

    for code, meta in REFERENCE_DOCUMENTS.items():
        content = _read_legal_file(meta["path"])
        current = conn.execute(
            """
            SELECT id,version,title,content,kind
            FROM legal_reference_documents
            WHERE code=? AND is_active=1
            """,
            (code,),
        ).fetchone()
        if (
            current
            and str(current["version"]) == LEGAL_VERSION
            and str(current["title"]) == meta["title"]
            and str(current["content"]).strip() == content
            and str(current["kind"]) == meta["kind"]
        ):
            continue
        conn.execute(
            "UPDATE legal_reference_documents SET is_active=0 WHERE code=?", (code,)
        )
        conn.execute(
            """
            INSERT INTO legal_reference_documents(
                code,version,title,content,kind,is_active,published_at
            ) VALUES (?,?,?,?,?,1,CURRENT_TIMESTAMP)
            ON CONFLICT(code,version) DO UPDATE SET
                title=excluded.title,
                content=excluded.content,
                kind=excluded.kind,
                is_active=1,
                published_at=CURRENT_TIMESTAMP
            """,
            (code, LEGAL_VERSION, meta["title"], content, meta["kind"]),
        )


def _active_reference(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM legal_reference_documents WHERE code=? AND is_active=1",
        (code,),
    ).fetchone()


def _record_optional_consent(
    conn: sqlite3.Connection,
    *,
    settings,
    request: Request,
    account_id: int,
    code: str,
    granted: bool,
) -> None:
    document = _active_reference(conn, code)
    if not document:
        raise ValueError("Документ временно недоступен")
    version = str(document["version"])
    conn.execute(
        """
        INSERT INTO member_optional_consent_state(account_id,code,document_version,granted)
        VALUES (?,?,?,?)
        ON CONFLICT(account_id,code) DO UPDATE SET
            document_version=excluded.document_version,
            granted=excluded.granted,
            updated_at=CURRENT_TIMESTAMP
        """,
        (account_id, code, version, 1 if granted else 0),
    )
    conn.execute(
        """
        INSERT INTO member_optional_consent_events(
            account_id,code,document_version,granted,ip_hash,user_agent
        ) VALUES (?,?,?,?,?,?)
        """,
        (
            account_id,
            code,
            version,
            1 if granted else 0,
            _ip_hash(settings, request),
            request.headers.get("user-agent", "")[:500],
        ),
    )


def _record_rating_consent(
    conn: sqlite3.Connection,
    *,
    settings,
    request: Request,
    account_id: int,
    categories: dict[str, bool],
) -> None:
    document = _active_reference(conn, "public-rating-consent")
    if not document:
        raise ValueError("Документ временно недоступен")
    normalized = {key: bool(categories.get(key)) for key in RATING_CATEGORY_KEYS}
    granted = any(normalized.values())
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    version = str(document["version"])
    conn.execute(
        """
        INSERT INTO member_public_rating_consent_state(
            account_id,document_version,categories_json,granted
        ) VALUES (?,?,?,?)
        ON CONFLICT(account_id) DO UPDATE SET
            document_version=excluded.document_version,
            categories_json=excluded.categories_json,
            granted=excluded.granted,
            updated_at=CURRENT_TIMESTAMP
        """,
        (account_id, version, payload, 1 if granted else 0),
    )
    conn.execute(
        """
        INSERT INTO member_public_rating_consent_events(
            account_id,document_version,categories_json,granted,ip_hash,user_agent
        ) VALUES (?,?,?,?,?,?)
        """,
        (
            account_id,
            version,
            payload,
            1 if granted else 0,
            _ip_hash(settings, request),
            request.headers.get("user-agent", "")[:500],
        ),
    )


def _move_latest_route_before_existing(app: FastAPI, path: str, method: str) -> None:
    latest_index = len(app.router.routes) - 1
    latest = app.router.routes[latest_index]
    if getattr(latest, "path", None) != path:
        return
    app.router.routes.pop(latest_index)
    target = len(app.router.routes)
    for index, route in enumerate(app.router.routes):
        if getattr(route, "path", None) != path:
            continue
        methods = getattr(route, "methods", set()) or set()
        if method.upper() in methods:
            target = index
            break
    app.router.routes.insert(target, latest)


def _legal_document_row(conn: sqlite3.Connection, public_code: str) -> sqlite3.Row | None:
    for internal_code, meta in MANDATORY_DOCUMENTS.items():
        if meta["public_code"] == public_code:
            return conn.execute(
                "SELECT *, ? AS public_code FROM legal_documents WHERE code=? AND is_active=1",
                (public_code, internal_code),
            ).fetchone()
    return conn.execute(
        "SELECT *, code AS public_code FROM legal_reference_documents WHERE code=? AND is_active=1",
        (public_code,),
    ).fetchone()


def _selected_matches_view(selected: sqlite3.Row | None, view: str) -> bool:
    if not selected:
        return False
    archived = not selected["account_id"] or str(selected["client_status"] or "") == "deleted"
    return archived if view == "archive" else not archived


def install_legal_registration(app: FastAPI) -> FastAPI:
    if getattr(app.state, "legal_registration_installed", False):
        return app
    app.state.legal_registration_installed = True
    app.state.legal_registration_schema_ready = False
    settings = app.state.settings
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

    @app.middleware("http")
    async def legal_registration_middleware(request: Request, call_next):
        if not request.app.state.legal_registration_schema_ready:
            with _SCHEMA_LOCK:
                if not request.app.state.legal_registration_schema_ready:
                    with transaction(settings.db_path) as conn:
                        ensure_legal_registration_schema(conn)
                    request.app.state.legal_registration_schema_ready = True

        path = request.url.path
        method = request.method.upper()

        if method == "POST" and path == "/account/register/request-code":
            birth_date = str(request.session.get("member_registration_birth_date") or "")
            if not birth_date or not _is_adult(birth_date):
                values = urlencode(
                    {"error": "Регистрация доступна только пользователям, достигшим 18 лет"}
                )
                return RedirectResponse(f"/account/register?{values}", status_code=303)

        member = None
        if path == "/quiz" or path.startswith("/quiz/") or path in QUIZ_MEMBER_API_PATHS:
            member = _current_member(request, required=False)
            if not member:
                next_path = path
                if request.url.query:
                    next_path += f"?{request.url.query}"
                login_url = f"/account/login?next={quote(next_path, safe='')}"
                if path == "/quiz" or path.startswith("/quiz/"):
                    return RedirectResponse(login_url, status_code=303)
                return JSONResponse(
                    {
                        "error": "account_required",
                        "detail": "Для участия в квизе нужен зарегистрированный личный кабинет",
                        "login_url": login_url,
                    },
                    status_code=401,
                )

        pending_email = ""
        marketing_choice: bool | None = None
        if method == "POST" and path == "/account/register/verify":
            code_id = request.session.get("member_registration_code_id")
            marketing_choice = bool(request.session.get("member_registration_marketing", False))
            if code_id:
                with connect(settings.db_path) as conn:
                    row = conn.execute(
                        "SELECT email_normalized FROM member_email_codes WHERE id=? AND purpose='register'",
                        (int(code_id),),
                    ).fetchone()
                if row:
                    pending_email = str(row["email_normalized"] or "")

        response = await call_next(request)

        if (
            method == "POST"
            and path == "/account/register/verify"
            and response.status_code in {302, 303}
            and pending_email
            and marketing_choice is not None
        ):
            with transaction(settings.db_path) as conn:
                account = conn.execute(
                    "SELECT id FROM member_accounts WHERE email_normalized=? ORDER BY id DESC LIMIT 1",
                    (pending_email,),
                ).fetchone()
                if account:
                    _record_optional_consent(
                        conn,
                        settings=settings,
                        request=request,
                        account_id=int(account["id"]),
                        code="marketing-consent",
                        granted=marketing_choice,
                    )
            request.session.pop("member_registration_marketing", None)
        return response

    @app.get("/legal/{public_code}", response_class=HTMLResponse)
    async def legal_document_page(request: Request, public_code: str):
        with connect(settings.db_path) as conn:
            document = _legal_document_row(conn, public_code)
        if not document:
            return HTMLResponse("Документ не найден", status_code=404)
        member = _current_member(request, required=False)
        return templates.TemplateResponse(
            request,
            "legal_document.html",
            {
                "request": request,
                "member": member,
                "current_tab": "profile",
                "document": document,
                "asset_version": "legal-registration-v1",
            },
        )

    @app.post("/api/account/register/legal-extra")
    async def registration_legal_extra(
        request: Request,
        birth_date: str = Form(...),
        marketing: bool = Form(False),
        csrf_token: str = Form(...),
    ):
        _check_csrf(request, csrf_token)
        if not _is_adult(birth_date):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Регистрация доступна только пользователям, достигшим 18 лет",
                },
                status_code=409,
            )
        request.session["member_registration_marketing"] = bool(marketing)
        return JSONResponse({"ok": True})

    @app.get("/account/legal", response_class=HTMLResponse)
    async def member_legal_preferences(request: Request, ok: str = "", error: str = ""):
        member = _current_member(request, required=True)
        with connect(settings.db_path) as conn:
            references = {
                row["code"]: row
                for row in conn.execute(
                    "SELECT * FROM legal_reference_documents WHERE is_active=1"
                ).fetchall()
            }
            states = {
                row["code"]: bool(row["granted"])
                for row in conn.execute(
                    "SELECT code,granted FROM member_optional_consent_state WHERE account_id=?",
                    (int(member["id"]),),
                ).fetchall()
            }
            rating_row = conn.execute(
                "SELECT * FROM member_public_rating_consent_state WHERE account_id=?",
                (int(member["id"]),),
            ).fetchone()
        rating = {key: False for key in RATING_CATEGORY_KEYS}
        if rating_row:
            try:
                stored = json.loads(str(rating_row["categories_json"] or "{}"))
                for key in RATING_CATEGORY_KEYS:
                    rating[key] = bool(stored.get(key))
            except json.JSONDecodeError:
                pass
        return templates.TemplateResponse(
            request,
            "member_legal_preferences.html",
            {
                "request": request,
                "member": member,
                "current_tab": "profile",
                "references": references,
                "states": states,
                "rating": rating,
                "csrf_token": request.session.get("csrf", ""),
                "asset_version": "legal-registration-v1",
                "ok": ok,
                "error": error,
            },
        )

    @app.post("/account/legal/optional/{code}")
    async def member_optional_consent_update(
        request: Request,
        code: str,
        granted: bool = Form(False),
        csrf_token: str = Form(...),
    ):
        member = _current_member(request, required=True)
        _check_csrf(request, csrf_token)
        if code not in {"marketing-consent", "image-consent"}:
            return RedirectResponse(
                "/account/legal?error=Неизвестное+согласие", status_code=303
            )
        with transaction(settings.db_path) as conn:
            _record_optional_consent(
                conn,
                settings=settings,
                request=request,
                account_id=int(member["id"]),
                code=code,
                granted=granted,
            )
        return RedirectResponse("/account/legal?ok=Настройки+сохранены", status_code=303)

    @app.post("/account/legal/public-rating")
    async def member_public_rating_consent_update(
        request: Request,
        nickname: bool = Form(False),
        avatar: bool = Form(False),
        result: bool = Form(False),
        place: bool = Form(False),
        achievements: bool = Form(False),
        titles: bool = Form(False),
        participation_stats: bool = Form(False),
        csrf_token: str = Form(...),
    ):
        member = _current_member(request, required=True)
        _check_csrf(request, csrf_token)
        categories = {
            "nickname": nickname,
            "avatar": avatar,
            "result": result,
            "place": place,
            "achievements": achievements,
            "titles": titles,
            "participation_stats": participation_stats,
        }
        with transaction(settings.db_path) as conn:
            _record_rating_consent(
                conn,
                settings=settings,
                request=request,
                account_id=int(member["id"]),
                categories=categories,
            )
        return RedirectResponse("/account/legal?ok=Настройки+рейтинга+сохранены", status_code=303)

    @app.get("/master/member-accounts", response_class=HTMLResponse)
    async def master_member_accounts_filtered(
        request: Request,
        q: str = "",
        client_id: int | None = None,
        view: str = "active",
        ok: str = "",
        error: str = "",
    ):
        _require_master(request)
        selected_view = "archive" if view == "archive" else "active"
        search = " ".join(str(q or "").split())[:120]
        category_sql = (
            "(ma.id IS NULL OR COALESCE(c.client_status,'existing')='deleted')"
            if selected_view == "archive"
            else "(ma.id IS NOT NULL AND COALESCE(c.client_status,'existing')<>'deleted')"
        )
        where_parts = [category_sql]
        params: list[Any] = []
        if search:
            like = f"%{search}%"
            where_parts.append(
                """(
                    CAST(c.id AS TEXT) LIKE ? OR COALESCE(c.first_name,'') LIKE ?
                    OR COALESCE(c.nickname,'') LIKE ? OR COALESCE(c.username,'') LIKE ?
                    OR COALESCE(c.phone_full,'') LIKE ? OR COALESCE(c.phone_local,'') LIKE ?
                    OR COALESCE(ma.email,'') LIKE ? OR COALESCE(c.app_user_id,'') LIKE ?
                    OR COALESCE(c.referrer_app_user_id,'') LIKE ?
                )"""
            )
            params.extend([like] * 9)
        where = " WHERE " + " AND ".join(where_parts)
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
                ORDER BY c.updated_at DESC,c.id DESC
                LIMIT 200
                """,
                params,
            ).fetchall()
            selected = _identity_row(conn, client_id) if client_id else None
            if not _selected_matches_view(selected, selected_view):
                selected = None
            counts = conn.execute(
                """
                SELECT
                  SUM(CASE WHEN ma.id IS NOT NULL AND COALESCE(c.client_status,'existing')<>'deleted' THEN 1 ELSE 0 END) AS active_count,
                  SUM(CASE WHEN ma.id IS NULL OR COALESCE(c.client_status,'existing')='deleted' THEN 1 ELSE 0 END) AS archive_count
                FROM clients c LEFT JOIN member_accounts ma ON ma.client_id=c.id
                """
            ).fetchone()
        return templates.TemplateResponse(
            request,
            "master_member_accounts.html",
            {
                "request": request,
                "rows": rows,
                "selected": selected,
                "q": search,
                "view": selected_view,
                "active_count": int(counts["active_count"] or 0),
                "archive_count": int(counts["archive_count"] or 0),
                "ok": ok,
                "error": error,
                "csrf_token": request.session.get("csrf", ""),
                "admin_name": request.session.get("admin_name", "Мастер"),
                "admin_role": request.session.get("admin_role", "master_admin"),
                "asset_version": "legal-registration-v1",
                "display_phone": display_phone,
            },
        )

    _move_latest_route_before_existing(app, "/master/member-accounts", "GET")

    @app.get("/master/legal-documents", response_class=HTMLResponse)
    async def master_legal_documents(request: Request, ok: str = "", error: str = ""):
        _require_master(request)
        with connect(settings.db_path) as conn:
            mandatory = conn.execute(
                "SELECT * FROM legal_documents WHERE is_active=1 ORDER BY code"
            ).fetchall()
            references = conn.execute(
                "SELECT * FROM legal_reference_documents WHERE is_active=1 ORDER BY code"
            ).fetchall()
        return templates.TemplateResponse(
            request,
            "master_legal_documents.html",
            {
                "request": request,
                "mandatory": mandatory,
                "references": references,
                "csrf_token": request.session.get("csrf", ""),
                "asset_version": "legal-registration-v1",
                "admin_name": request.session.get("admin_name", "Мастер"),
                "admin_role": request.session.get("admin_role", "master_admin"),
                "ok": ok,
                "error": error,
            },
        )

    @app.post("/master/legal-documents/{store}/{code}/publish")
    async def master_legal_document_publish(
        request: Request,
        store: str,
        code: str,
        version: str = Form(...),
        title: str = Form(...),
        content: str = Form(...),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        version = " ".join(version.split())[:40]
        title = " ".join(title.split())[:240]
        content = content.strip()
        if not version or not title or len(content) < 100:
            return RedirectResponse(
                "/master/legal-documents?error=Проверьте+версию,+название+и+текст",
                status_code=303,
            )
        with transaction(settings.db_path) as conn:
            if store == "mandatory" and code in MANDATORY_DOCUMENTS:
                conn.execute("UPDATE legal_documents SET is_active=0 WHERE code=?", (code,))
                conn.execute(
                    """
                    INSERT INTO legal_documents(code,version,title,content,is_active,published_at)
                    VALUES (?,?,?,?,1,CURRENT_TIMESTAMP)
                    ON CONFLICT(code,version) DO UPDATE SET
                        title=excluded.title,content=excluded.content,is_active=1,published_at=CURRENT_TIMESTAMP
                    """,
                    (code, version, title, content),
                )
            elif store == "reference" and code in REFERENCE_DOCUMENTS:
                kind = REFERENCE_DOCUMENTS[code]["kind"]
                conn.execute(
                    "UPDATE legal_reference_documents SET is_active=0 WHERE code=?", (code,)
                )
                conn.execute(
                    """
                    INSERT INTO legal_reference_documents(
                        code,version,title,content,kind,is_active,published_at
                    ) VALUES (?,?,?,?,?,1,CURRENT_TIMESTAMP)
                    ON CONFLICT(code,version) DO UPDATE SET
                        title=excluded.title,content=excluded.content,kind=excluded.kind,
                        is_active=1,published_at=CURRENT_TIMESTAMP
                    """,
                    (code, version, title, content, kind),
                )
            else:
                return RedirectResponse(
                    "/master/legal-documents?error=Неизвестный+документ", status_code=303
                )
            audit(
                conn,
                admin_id=int(request.session.get("admin_id")),
                admin_name=str(request.session.get("admin_name") or "master"),
                action="legal_document_published",
                entity_type="legal_document",
                entity_id=code,
                details={"store": store, "version": version},
            )
        return RedirectResponse(
            "/master/legal-documents?ok=Новая+редакция+опубликована", status_code=303
        )

    return app


__all__ = ["ensure_legal_registration_schema", "install_legal_registration"]
