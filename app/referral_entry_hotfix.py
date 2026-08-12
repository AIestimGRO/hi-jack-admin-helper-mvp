from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.db import transaction
from app.legal_registration import _move_latest_route_before_existing
from app.product_shell import _current_member
from app.referral_registration_integrity import (
    PENDING_REFERRAL_KEY,
    REFERRAL_SOURCE_CAMPAIGN,
    bind_referral,
)
from app.services.jackside_engagement import record_referral_click
from app.services.jackside_issues import current_featured_issue
from app.services.quiz import ip_fingerprint


def install_referral_entry_hotfix(app: FastAPI) -> FastAPI:
    if getattr(app.state, "referral_entry_hotfix_installed", False):
        return app
    app.state.referral_entry_hotfix_installed = True
    settings = app.state.settings

    @app.get("/jackside/ref/{code}")
    async def registration_first_referral_entry(request: Request, code: str):
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

        # Referral links are onboarding links. Preserve the code before any redirect
        # so Telegram in-app browser, legal consent and email verification cannot
        # lose attribution.
        request.session[PENDING_REFERRAL_KEY] = clean_code

        member = _current_member(request, required=False)
        if not member:
            return RedirectResponse("/account/register", status_code=303)

        with transaction(settings.db_path) as conn:
            result = bind_referral(
                conn,
                invited_client_id=int(member["client_id"]),
                referral_code=clean_code,
                source_campaign_code=campaign_code or REFERRAL_SOURCE_CAMPAIGN,
            )
        if result.get("status") != "unknown_code":
            request.session.pop(PENDING_REFERRAL_KEY, None)
        return RedirectResponse("/account", status_code=303)

    _move_latest_route_before_existing(app, "/jackside/ref/{code}", "GET")
    return app


__all__ = ["install_referral_entry_hotfix"]
