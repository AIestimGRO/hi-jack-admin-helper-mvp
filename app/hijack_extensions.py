from __future__ import annotations

import secrets
import threading
import uuid
from datetime import date
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.db import connect, transaction
from app.product_shell import _check_csrf, _csrf_token, _current_member, _require_master
from app.services.hijack_rating import (
    HIJACK_TITLE_CONDITIONS,
    MAX_IMPORT_BYTES,
    ensure_hijack_rating_schema,
    hijack_rating_payload,
    import_hijack_rating,
    list_hijack_imports,
    parse_hijack_rating_workbook,
    referral_tree,
    refresh_hijack_engagement,
)

_SCHEMA_LOCK = threading.Lock()


def _master_redirect(message: str, *, error: bool = False) -> RedirectResponse:
    key = "error" if error else "ok"
    return RedirectResponse(
        f"/master/hijack-rating?{urlencode({key: message})}",
        status_code=303,
    )


def _title_redirect(message: str, *, error: bool = False) -> RedirectResponse:
    key = "error" if error else "ok"
    return RedirectResponse(
        f"/master?{urlencode({'tab': 'engagement', key: message})}",
        status_code=303,
    )


def _bool_form(value: str | bool | None) -> int:
    return 1 if str(value or "").lower() in {"1", "true", "on", "yes"} else 0


def _clean_title_form(
    *,
    name: str,
    description: str,
    icon: str,
    condition_code: str,
    threshold: int,
    priority: int,
    material_reward_enabled: str | bool | None,
    material_preference_code: str,
    material_reward_amount: int,
) -> dict[str, Any]:
    clean_name = " ".join(str(name or "").split())[:100]
    clean_condition = str(condition_code or "").strip()
    if not clean_name:
        raise ValueError("Укажите название звания")
    if clean_condition not in HIJACK_TITLE_CONDITIONS:
        raise ValueError("Выберите условие HI, JACK!")
    if int(threshold) < 1:
        raise ValueError("Порог должен быть не меньше 1")
    return {
        "name": clean_name,
        "description": str(description or "").strip()[:500],
        "icon": str(icon or "").strip()[:100],
        "condition_code": clean_condition,
        "threshold": min(1_000_000, int(threshold)),
        "priority": max(0, min(10_000, int(priority))),
        "material_reward_enabled": _bool_form(material_reward_enabled),
        "material_preference_code": str(material_preference_code or "").strip() or None,
        "material_reward_amount": max(0, min(1000, int(material_reward_amount))),
    }


def install_hijack_extensions(app: FastAPI) -> FastAPI:
    if getattr(app.state, "hijack_extensions_installed", False):
        return app
    app.state.hijack_extensions_installed = True
    app.state.hijack_extensions_schema_ready = False
    settings = app.state.settings
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

    @app.middleware("http")
    async def hijack_extensions_middleware(request: Request, call_next):
        if not request.app.state.hijack_extensions_schema_ready:
            with _SCHEMA_LOCK:
                if not request.app.state.hijack_extensions_schema_ready:
                    with transaction(settings.db_path) as conn:
                        ensure_hijack_rating_schema(conn)
                    request.app.state.hijack_extensions_schema_ready = True

        # Award HI, JACK! titles before the profile/rating template is rendered.
        if request.method == "GET" and request.url.path == "/account":
            try:
                member = _current_member(request, required=False)
                if member:
                    with transaction(settings.db_path) as conn:
                        refresh_hijack_engagement(
                            conn,
                            client_id=int(member["client_id"]),
                        )
            except Exception:
                # Rating imports must never make the member portal unavailable.
                pass
        return await call_next(request)

    @app.get("/api/account/hijack-rating")
    async def account_hijack_rating(request: Request):
        member = _current_member(request, required=True)
        with connect(settings.db_path) as conn:
            payload = hijack_rating_payload(
                conn,
                client_id=int(member["client_id"]),
            )
        return JSONResponse(payload)

    @app.get("/api/account/referral-tree")
    async def account_referral_tree(request: Request):
        member = _current_member(request, required=True)
        with connect(settings.db_path) as conn:
            payload = referral_tree(conn, root_client_id=int(member["client_id"]))
        return JSONResponse(payload)

    @app.get("/master/hijack-rating", response_class=HTMLResponse)
    async def master_hijack_rating(
        request: Request,
        ok: str = "",
        error: str = "",
    ):
        _require_master(request)
        with connect(settings.db_path) as conn:
            imports = list_hijack_imports(conn)
        return templates.TemplateResponse(
            request,
            "hijack_rating_admin.html",
            {
                "request": request,
                "imports": imports,
                "conditions": HIJACK_TITLE_CONDITIONS,
                "ok": ok,
                "error": error,
                "csrf_token": _csrf_token(request),
                "admin_name": request.session.get("admin_name", "Администратор"),
                "admin_role": request.session.get("admin_role", ""),
                "asset_version": "hijack-rating-1",
            },
        )

    @app.post("/api/master/hijack-rating/import")
    async def master_hijack_rating_import(
        request: Request,
        tournament_name: str = Form(...),
        tournament_date: str = Form(...),
        csrf_token: str = Form(...),
        rating_file: UploadFile = File(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        try:
            day = date.fromisoformat(str(tournament_date).strip())
        except ValueError:
            return _master_redirect("Укажите корректную дату турнира", error=True)
        data = await rating_file.read(MAX_IMPORT_BYTES + 1)
        try:
            rows = parse_hijack_rating_workbook(data)
            with transaction(settings.db_path) as conn:
                result = import_hijack_rating(
                    conn,
                    tournament_name=tournament_name,
                    tournament_date=day,
                    source_filename=rating_file.filename or "rating.xlsx",
                    rows=rows,
                    admin_id=int(request.session.get("admin_id") or 0) or None,
                )
        except ValueError as exc:
            return _master_redirect(str(exc), error=True)
        return _master_redirect(
            "Загружен турнир «{}»: {} строк, {} пользователей найдено, {} телефонов не найдено".format(
                result["tournament_name"],
                result["total_rows"],
                result["matched_rows"],
                result["unmatched_rows"] + result["invalid_rows"],
            )
        )

    @app.post("/api/master/hijack-rating/{import_id:int}/delete")
    async def master_hijack_rating_delete(
        request: Request,
        import_id: int,
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            row = conn.execute(
                "SELECT tournament_name FROM hi_jack_rating_imports WHERE id=?",
                (import_id,),
            ).fetchone()
            if not row:
                return _master_redirect("Загрузка не найдена", error=True)
            conn.execute("DELETE FROM hi_jack_rating_imports WHERE id=?", (import_id,))
        return _master_redirect(f"Загрузка «{row['tournament_name']}» удалена")

    @app.post("/api/master/hijack-titles/create")
    async def master_hijack_title_create(
        request: Request,
        csrf_token: str = Form(...),
        name: str = Form(...),
        description: str = Form(""),
        icon: str = Form(""),
        title_type: str = Form("permanent"),
        condition_code: str = Form(...),
        threshold: int = Form(1),
        period_code: str = Form("all_time"),
        priority: int = Form(100),
        material_reward_enabled: str = Form(""),
        material_preference_code: str = Form(""),
        material_reward_amount: int = Form(0),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        try:
            values = _clean_title_form(
                name=name,
                description=description,
                icon=icon,
                condition_code=condition_code,
                threshold=threshold,
                priority=priority,
                material_reward_enabled=material_reward_enabled,
                material_preference_code=material_preference_code,
                material_reward_amount=material_reward_amount,
            )
        except ValueError as exc:
            return _title_redirect(str(exc), error=True)
        clean_type = str(title_type or "permanent").strip()
        if clean_type not in {"permanent", "temporary"}:
            return _title_redirect("Некорректный тип звания", error=True)
        code = f"custom_{uuid.uuid4().hex[:12]}"
        with transaction(settings.db_path) as conn:
            conn.execute(
                """
                INSERT INTO title_definitions(
                    code,name,description,icon,title_type,condition_code,threshold,
                    period_code,priority,material_reward_enabled,
                    material_preference_code,material_reward_amount
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    code,
                    values["name"],
                    values["description"],
                    values["icon"],
                    clean_type,
                    values["condition_code"],
                    values["threshold"],
                    "all_time",
                    values["priority"],
                    values["material_reward_enabled"],
                    values["material_preference_code"],
                    values["material_reward_amount"],
                ),
            )
        return _title_redirect("Звание HI, JACK! создано")

    @app.post("/api/master/hijack-titles/{title_id:int}/update")
    async def master_hijack_title_update(
        request: Request,
        title_id: int,
        csrf_token: str = Form(...),
        name: str = Form(...),
        description: str = Form(""),
        icon: str = Form(""),
        condition_code: str = Form(...),
        threshold: int = Form(1),
        period_code: str = Form("all_time"),
        priority: int = Form(100),
        material_reward_enabled: str = Form(""),
        material_preference_code: str = Form(""),
        material_reward_amount: int = Form(0),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        try:
            values = _clean_title_form(
                name=name,
                description=description,
                icon=icon,
                condition_code=condition_code,
                threshold=threshold,
                priority=priority,
                material_reward_enabled=material_reward_enabled,
                material_preference_code=material_preference_code,
                material_reward_amount=material_reward_amount,
            )
        except ValueError as exc:
            return _title_redirect(str(exc), error=True)
        with transaction(settings.db_path) as conn:
            row = conn.execute("SELECT id FROM title_definitions WHERE id=?", (title_id,)).fetchone()
            if not row:
                return _title_redirect("Звание не найдено", error=True)
            conn.execute(
                """
                UPDATE title_definitions
                SET name=?,description=?,icon=?,condition_code=?,threshold=?,period_code='all_time',
                    priority=?,material_reward_enabled=?,material_preference_code=?,
                    material_reward_amount=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    values["name"],
                    values["description"],
                    values["icon"],
                    values["condition_code"],
                    values["threshold"],
                    values["priority"],
                    values["material_reward_enabled"],
                    values["material_preference_code"],
                    values["material_reward_amount"],
                    title_id,
                ),
            )
        return _title_redirect("Звание HI, JACK! обновлено")

    return app


__all__ = ["install_hijack_extensions"]
