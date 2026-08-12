from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.db import connect, transaction
from app.legal_registration import _move_latest_route_before_existing
from app.product_shell import _check_csrf, _current_member, _require_master
from app.services.auth import audit
from app.services.jackside_engagement import (
    ensure_jackside_referral_code,
    fix_jackside_referral,
    record_referral_click,
)
from app.services.jackside_issues import current_featured_issue
from app.services.quiz import ip_fingerprint


PENDING_REFERRAL_KEY = "pending_jackside_referral_code"
REFERRAL_SOURCE_CAMPAIGN = "jackside"


def bind_referral(
    conn,
    *,
    invited_client_id: int,
    referral_code: str,
    source_campaign_code: str = REFERRAL_SOURCE_CAMPAIGN,
) -> dict[str, Any]:
    return fix_jackside_referral(
        conn,
        invited_client_id=int(invited_client_id),
        referral_code=str(referral_code or "").strip()[:80],
        campaign_code=str(source_campaign_code or REFERRAL_SOURCE_CAMPAIGN)[:80],
    )


def manual_link_referral(
    conn,
    *,
    referrer_client_id: int,
    invited_client_id: int,
) -> dict[str, Any]:
    referrer_client_id = int(referrer_client_id)
    invited_client_id = int(invited_client_id)
    if referrer_client_id == invited_client_id:
        raise ValueError("Нельзя привязать пользователя к самому себе")

    referrer = conn.execute(
        "SELECT id,client_status FROM clients WHERE id=?",
        (referrer_client_id,),
    ).fetchone()
    invited = conn.execute(
        "SELECT id,client_status FROM clients WHERE id=?",
        (invited_client_id,),
    ).fetchone()
    if not referrer or str(referrer["client_status"] or "") == "deleted":
        raise ValueError("Аккаунт пригласившего не найден или удалён")
    if not invited or str(invited["client_status"] or "") == "deleted":
        raise ValueError("Аккаунт приглашённого не найден или удалён")

    existing = conn.execute(
        "SELECT referrer_client_id FROM referral_qualification_progress WHERE invited_client_id=?",
        (invited_client_id,),
    ).fetchone()
    if existing:
        current_referrer = int(existing["referrer_client_id"])
        if current_referrer == referrer_client_id:
            return {
                "status": "already_fixed",
                "fixed": True,
                "referrer_client_id": current_referrer,
            }
        raise ValueError(
            f"У приглашённого уже зафиксирован другой реферер: Client ID {current_referrer}"
        )

    code = ensure_jackside_referral_code(conn, referrer_client_id)
    result = bind_referral(
        conn,
        invited_client_id=invited_client_id,
        referral_code=str(code["code"]),
        source_campaign_code="manual_master",
    )
    if not result.get("fixed"):
        raise ValueError("Не удалось зафиксировать реферальную связь")
    return result


def install_referral_registration_integrity(app: FastAPI) -> FastAPI:
    if getattr(app.state, "referral_registration_integrity_installed", False):
        return app
    app.state.referral_registration_integrity_installed = True
    settings = app.state.settings
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

    @app.middleware("http")
    async def pending_referral_binding_middleware(request: Request, call_next):
        pending = str(request.session.get(PENDING_REFERRAL_KEY) or "").strip()[:80]
        if pending:
            try:
                member = _current_member(request, required=False)
            except Exception:
                member = None
            if member:
                try:
                    with transaction(settings.db_path) as conn:
                        result = bind_referral(
                            conn,
                            invited_client_id=int(member["client_id"]),
                            referral_code=pending,
                        )
                    if result.get("status") != "unknown_code":
                        request.session.pop(PENDING_REFERRAL_KEY, None)
                except Exception:
                    # Keep the pending code for the next request; a transient DB
                    # problem must not silently erase attribution.
                    pass
        return await call_next(request)

    @app.get("/jackside/ref/{code}")
    async def durable_jackside_referral_entry(request: Request, code: str):
        clean_code = str(code or "").strip()[:80]
        if not clean_code:
            raise HTTPException(status_code=404, detail="Реферальная ссылка не найдена")
        client_ip = request.client.host if request.client else "unknown"
        ip_hash = ip_fingerprint(settings.secret_key, client_ip)
        with transaction(settings.db_path) as conn:
            featured = current_featured_issue(
                conn,
                now=datetime.now(timezone.utc),
                timezone_name=settings.timezone_name,
            )
            campaign_code = str(featured.get("campaign_code") or "") if featured else ""
            owner = record_referral_click(
                conn,
                code=clean_code,
                campaign_code=campaign_code or None,
                ip_hash=ip_hash,
            )
        if not owner:
            raise HTTPException(status_code=404, detail="Реферальная ссылка не найдена")

        # Preserve attribution through login, legal consent and registration.
        request.session[PENDING_REFERRAL_KEY] = clean_code

        member = _current_member(request, required=False)
        if member:
            with transaction(settings.db_path) as conn:
                result = bind_referral(
                    conn,
                    invited_client_id=int(member["client_id"]),
                    referral_code=clean_code,
                    source_campaign_code=campaign_code or REFERRAL_SOURCE_CAMPAIGN,
                )
            if result.get("status") != "unknown_code":
                request.session.pop(PENDING_REFERRAL_KEY, None)

        if campaign_code:
            return RedirectResponse(
                f"/quiz?{urlencode({'campaign': campaign_code, 'ref': clean_code, 'source': 'jackside_referral'})}",
                status_code=303,
            )
        return RedirectResponse("/account", status_code=303)

    _move_latest_route_before_existing(app, "/jackside/ref/{code}", "GET")

    @app.get("/master/referrals", response_class=HTMLResponse)
    async def master_referrals_page(
        request: Request,
        ok: str = "",
        error: str = "",
    ):
        _require_master(request)
        with connect(settings.db_path) as conn:
            rows = conn.execute(
                """
                SELECT rqp.id,rqp.referrer_client_id,rqp.invited_client_id,
                       rqp.distinct_completed_days,rqp.qualified_at,rqp.created_at,
                       COALESCE(r.first_name,r.nickname,r.username,'') AS referrer_name,
                       COALESCE(i.first_name,i.nickname,i.username,'') AS invited_name
                FROM referral_qualification_progress rqp
                JOIN clients r ON r.id=rqp.referrer_client_id
                JOIN clients i ON i.id=rqp.invited_client_id
                ORDER BY rqp.created_at DESC,rqp.id DESC
                LIMIT 100
                """
            ).fetchall()
        return templates.TemplateResponse(
            request,
            "master_referrals.html",
            {
                "request": request,
                "rows": rows,
                "ok": ok,
                "error": error,
                "csrf_token": request.session.get("csrf", ""),
                "admin_name": request.session.get("admin_name", "Мастер"),
                "admin_role": request.session.get("admin_role", "master_admin"),
                "asset_version": "referral-integrity-v1",
            },
        )

    @app.post("/api/master/referrals/link")
    async def master_referrals_link(
        request: Request,
        referrer_client_id: int = Form(...),
        invited_client_id: int = Form(...),
        confirmation: str = Form(...),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        if str(confirmation or "").strip().upper() != "ПРИВЯЗАТЬ":
            return RedirectResponse(
                "/master/referrals?error=Введите+ПРИВЯЗАТЬ+для+подтверждения",
                status_code=303,
            )
        try:
            with transaction(settings.db_path) as conn:
                result = manual_link_referral(
                    conn,
                    referrer_client_id=referrer_client_id,
                    invited_client_id=invited_client_id,
                )
                audit(
                    conn,
                    admin_id=int(request.session.get("admin_id")),
                    admin_name=str(request.session.get("admin_name") or "master"),
                    action="manual_referral_link",
                    entity_type="referral_qualification_progress",
                    entity_id=int(invited_client_id),
                    details={
                        "referrer_client_id": int(referrer_client_id),
                        "invited_client_id": int(invited_client_id),
                        "status": str(result.get("status") or ""),
                    },
                )
        except ValueError as exc:
            return RedirectResponse(
                f"/master/referrals?{urlencode({'error': str(exc)})}",
                status_code=303,
            )
        return RedirectResponse(
            f"/master/referrals?{urlencode({'ok': 'Реферальная связь зафиксирована'})}",
            status_code=303,
        )

    return app


__all__ = [
    "PENDING_REFERRAL_KEY",
    "bind_referral",
    "install_referral_registration_integrity",
    "manual_link_referral",
]
