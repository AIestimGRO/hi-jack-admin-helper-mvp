from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.db import connect, transaction
from app.legal_registration import _move_latest_route_before_existing
from app.prelaunch_experience import ensure_prelaunch_schema
from app.product_shell import _require_master


def _load_links(db_path: Any) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM club_social_links ORDER BY position, id"
            ).fetchall()
        ]


def install_club_links_hotfix(app: FastAPI) -> FastAPI:
    if getattr(app.state, "club_links_hotfix_installed", False):
        return app
    app.state.club_links_hotfix_installed = True
    settings = app.state.settings
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

    # The schema/seed belongs to startup, not to a GET request. This makes the
    # page read-only during normal navigation and avoids write locks on opening it.
    with transaction(settings.db_path) as conn:
        ensure_prelaunch_schema(conn)

    @app.get("/master/club-links", response_class=HTMLResponse)
    async def master_club_links_stable(
        request: Request, ok: str = "", error: str = ""
    ):
        _require_master(request)
        try:
            links = _load_links(settings.db_path)
        except sqlite3.OperationalError:
            # Defensive one-time repair for an older database that reached this
            # route before the additive pre-launch schema had been committed.
            with transaction(settings.db_path) as conn:
                ensure_prelaunch_schema(conn)
            links = _load_links(settings.db_path)

        return templates.TemplateResponse(
            request,
            "master_club_links.html",
            {
                "request": request,
                "admin_name": request.session.get("admin_name", ""),
                "admin_role": request.session.get("admin_role", ""),
                "csrf_token": request.session.get("csrf", ""),
                "asset_version": "prelaunch-v2",
                "links": links,
                "ok": ok,
                "error": error,
            },
        )

    _move_latest_route_before_existing(app, "/master/club-links", "GET")
    return app


__all__ = ["install_club_links_hotfix"]
