from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.db import transaction
from app.legal_registration import _move_latest_route_before_existing
from app.prelaunch_experience import ensure_prelaunch_schema
from app.product_shell import _current_member
from app.services.jackside_engagement import refresh_member_engagement


def install_account_links_hotfix(app: FastAPI) -> FastAPI:
    if getattr(app.state, "account_links_hotfix_installed", False):
        return app
    app.state.account_links_hotfix_installed = True
    settings = app.state.settings
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

    @app.get("/account/links", response_class=HTMLResponse)
    async def member_club_links_stable(request: Request):
        member = _current_member(request, required=True)
        with transaction(settings.db_path) as conn:
            ensure_prelaunch_schema(conn)
            engagement = refresh_member_engagement(
                conn,
                client_id=int(member["client_id"]),
                timezone_name=settings.timezone_name,
            )
            links = conn.execute(
                """
                SELECT * FROM club_social_links
                WHERE is_active=1 AND url<>''
                ORDER BY position,id
                """
            ).fetchall()

        return templates.TemplateResponse(
            request,
            "member_club_links.html",
            {
                "request": request,
                "member": member,
                # This is a standalone member page. Marking it as profile causes
                # member_base.html to render profile-only blocks that require the
                # full account dashboard context (quiz_stats, ratings, vault, etc.).
                "current_tab": "links",
                "links": links,
                "engagement": engagement,
                "asset_version": "account-links-v3",
            },
        )

    _move_latest_route_before_existing(app, "/account/links", "GET")
    return app


__all__ = ["install_account_links_hotfix"]
