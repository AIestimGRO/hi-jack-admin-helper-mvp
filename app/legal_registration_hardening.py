from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

import app.legal_registration as legal
from app.db import connect, transaction
from app.product_shell import _check_csrf, _current_member, _require_master
from app.services.auth import audit


def _is_quiz_path(path: str) -> bool:
    return path == "/quiz" or path.startswith("/quiz/") or path.startswith("/api/quiz/")


def _redirect_legal(message: str, *, error: bool = False) -> RedirectResponse:
    key = "error" if error else "ok"
    from urllib.parse import urlencode

    return RedirectResponse(
        f"/master/legal-documents?{urlencode({key: message})}", status_code=303
    )


def _has_column(conn, table: str, column: str) -> bool:
    return any(str(row[1]) == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _ensure_rating_conditions_schema(conn) -> None:
    if not _has_column(conn, "member_public_rating_consent_state", "conditions_text"):
        conn.execute(
            "ALTER TABLE member_public_rating_consent_state ADD COLUMN conditions_text TEXT NOT NULL DEFAULT ''"
        )
    if not _has_column(conn, "member_public_rating_consent_events", "conditions_text"):
        conn.execute(
            "ALTER TABLE member_public_rating_consent_events ADD COLUMN conditions_text TEXT NOT NULL DEFAULT ''"
        )


def install_legal_registration_hardening(app: FastAPI) -> FastAPI:
    if getattr(app.state, "legal_registration_hardening_installed", False):
        return app
    app.state.legal_registration_hardening_installed = True
    settings = app.state.settings

    # Seed the legal package during app construction. This makes the active legal
    # version deterministic before any account/test helper records consent evidence.
    with transaction(settings.db_path) as conn:
        legal.ensure_legal_registration_schema(conn)
        _ensure_rating_conditions_schema(conn)
    app.state.legal_registration_schema_ready = True

    # The legacy legal middleware is intentionally conservative and already protects
    # a set of quiz routes. Let authenticated admins keep their preview workflow while
    # all player-facing quiz endpoints require a real active member account.
    original_current_member = legal._current_member

    def current_member_with_admin_preview(request: Request, *, required: bool = False):
        if _is_quiz_path(request.url.path) and request.session.get("authenticated"):
            return {"id": 0, "client_id": 0, "admin_preview": True}
        return original_current_member(request, required=required)

    legal._current_member = current_member_with_admin_preview

    @app.middleware("http")
    async def strict_quiz_member_gate(request: Request, call_next):
        path = request.url.path
        if not _is_quiz_path(path):
            return await call_next(request)
        if request.session.get("authenticated"):
            return await call_next(request)
        member = _current_member(request, required=False)
        if member:
            return await call_next(request)

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

    @app.post("/master/legal-documents/{store}/{code}/publish")
    async def strict_legal_document_publish(
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
            return _redirect_legal("Проверьте версию, название и текст", error=True)

        with transaction(settings.db_path) as conn:
            if store == "mandatory" and code in legal.MANDATORY_DOCUMENTS:
                used = conn.execute(
                    "SELECT 1 FROM legal_documents WHERE code=? AND version=? LIMIT 1",
                    (code, version),
                ).fetchone()
                if used:
                    return _redirect_legal(
                        "Эта версия уже использовалась. Укажите новую версию", error=True
                    )
                conn.execute("UPDATE legal_documents SET is_active=0 WHERE code=?", (code,))
                conn.execute(
                    """
                    INSERT INTO legal_documents(code,version,title,content,is_active,published_at)
                    VALUES (?,?,?,?,1,CURRENT_TIMESTAMP)
                    """,
                    (code, version, title, content),
                )
            elif store == "reference" and code in legal.REFERENCE_DOCUMENTS:
                used = conn.execute(
                    """
                    SELECT 1 FROM legal_reference_documents
                    WHERE code=? AND version=? LIMIT 1
                    """,
                    (code, version),
                ).fetchone()
                if used:
                    return _redirect_legal(
                        "Эта версия уже использовалась. Укажите новую версию", error=True
                    )
                kind = legal.REFERENCE_DOCUMENTS[code]["kind"]
                conn.execute(
                    "UPDATE legal_reference_documents SET is_active=0 WHERE code=?", (code,)
                )
                conn.execute(
                    """
                    INSERT INTO legal_reference_documents(
                        code,version,title,content,kind,is_active,published_at
                    ) VALUES (?,?,?,?,?,1,CURRENT_TIMESTAMP)
                    """,
                    (code, version, title, content, kind),
                )
            else:
                return _redirect_legal("Неизвестный документ", error=True)

            audit(
                conn,
                admin_id=int(request.session.get("admin_id")),
                admin_name=str(request.session.get("admin_name") or "master"),
                action="legal_document_published",
                entity_type="legal_document",
                entity_id=None,
                details={"code": code, "store": store, "version": version},
            )
        return _redirect_legal("Новая редакция опубликована")

    legal._move_latest_route_before_existing(
        app, "/master/legal-documents/{store}/{code}/publish", "POST"
    )

    @app.post("/account/legal/public-rating")
    async def public_rating_consent_with_conditions(
        request: Request,
        nickname: bool = Form(False),
        avatar: bool = Form(False),
        result: bool = Form(False),
        place: bool = Form(False),
        achievements: bool = Form(False),
        titles: bool = Form(False),
        participation_stats: bool = Form(False),
        conditions_text: str = Form(""),
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
        normalized = {key: bool(categories.get(key)) for key in legal.RATING_CATEGORY_KEYS}
        conditions = " ".join(str(conditions_text or "").split())[:1000]
        payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        granted = any(normalized.values())

        with transaction(settings.db_path) as conn:
            document = legal._active_reference(conn, "public-rating-consent")
            if not document:
                return RedirectResponse(
                    "/account/legal?error=Документ+временно+недоступен", status_code=303
                )
            version = str(document["version"])
            conn.execute(
                """
                INSERT INTO member_public_rating_consent_state(
                    account_id,document_version,categories_json,granted,conditions_text
                ) VALUES (?,?,?,?,?)
                ON CONFLICT(account_id) DO UPDATE SET
                    document_version=excluded.document_version,
                    categories_json=excluded.categories_json,
                    granted=excluded.granted,
                    conditions_text=excluded.conditions_text,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (int(member["id"]), version, payload, 1 if granted else 0, conditions),
            )
            conn.execute(
                """
                INSERT INTO member_public_rating_consent_events(
                    account_id,document_version,categories_json,granted,conditions_text,
                    ip_hash,user_agent
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    int(member["id"]),
                    version,
                    payload,
                    1 if granted else 0,
                    conditions,
                    legal._ip_hash(settings, request),
                    request.headers.get("user-agent", "")[:500],
                ),
            )
        return RedirectResponse(
            "/account/legal?ok=Настройки+рейтинга+сохранены", status_code=303
        )

    legal._move_latest_route_before_existing(app, "/account/legal/public-rating", "POST")

    return app


__all__ = ["install_legal_registration_hardening"]
