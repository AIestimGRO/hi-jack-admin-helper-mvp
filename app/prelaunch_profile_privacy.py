from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.db import connect
from app.prelaunch_experience import _public_profile_payload
from app.product_shell import _current_member


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


def install_prelaunch_profile_privacy(app: FastAPI) -> FastAPI:
    if getattr(app.state, "prelaunch_profile_privacy_installed", False):
        return app
    app.state.prelaunch_profile_privacy_installed = True
    settings = app.state.settings
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

    @app.get("/players/{client_id:int}", response_class=HTMLResponse)
    async def privacy_safe_player_profile(request: Request, client_id: int):
        viewer = _current_member(request, required=True)
        if int(viewer["client_id"]) == int(client_id):
            return RedirectResponse("/account?tab=profile", status_code=303)
        with connect(settings.db_path) as conn:
            payload = _public_profile_payload(conn, client_id)
            if payload and not payload.get("restricted"):
                identity = conn.execute(
                    "SELECT nickname FROM clients WHERE id=?",
                    (client_id,),
                ).fetchone()
                nickname = str(identity["nickname"] or "").strip() if identity else ""
                payload["display_name"] = (
                    nickname
                    if payload.get("categories", {}).get("nickname") and nickname
                    else "Игрок Hi, Jack"
                )
        if not payload:
            return HTMLResponse("Профиль не найден", status_code=404)
        return templates.TemplateResponse(
            request,
            "public_player_profile.html",
            {
                "request": request,
                "member": viewer,
                "current_tab": "rating",
                "profile": payload,
                "asset_version": "prelaunch-v1",
            },
        )

    _move_latest_route_before_existing(app, "/players/{client_id:int}", "GET")
    return app


__all__ = ["install_prelaunch_profile_privacy"]
