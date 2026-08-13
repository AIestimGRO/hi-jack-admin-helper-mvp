from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import prelaunch_experience as experience
from app.config import BASE_DIR
from app.db import connect
from app.hijack_rating_paging import hijack_rating_page_payload
from app.product_shell import _current_member
from app.public_profile_refs import public_profile_ref, resolve_public_profile_ref
from app.services.jackside_analytics import build_jackside_analytics


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


def _rating_places(conn, client_id: int) -> tuple[int | None, int | None]:
    jackside_place = None
    hijack_place = None
    analytics = build_jackside_analytics(conn)
    for row in list(analytics.get("all") or []):
        if int(row.get("client_id") or 0) == int(client_id):
            if row.get("place") is not None:
                jackside_place = int(row["place"])
            break
    hijack = hijack_rating_page_payload(
        conn,
        client_id=client_id,
        period="global",
        offset=0,
        limit=1,
    )
    me = hijack.get("me") or {}
    if me.get("place") is not None:
        hijack_place = int(me["place"])
    return jackside_place, hijack_place


def install_prelaunch_profile_privacy(app: FastAPI) -> FastAPI:
    if getattr(app.state, "prelaunch_profile_privacy_installed", False):
        return app
    app.state.prelaunch_profile_privacy_installed = True
    settings = app.state.settings
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

    @app.get("/players", response_class=HTMLResponse)
    async def privacy_safe_players_directory(request: Request):
        member = _current_member(request, required=True)
        players = []
        with connect(settings.db_path) as conn:
            rows = conn.execute(
                """
                SELECT ma.id AS account_id,c.id AS client_id,c.nickname
                FROM member_accounts ma
                JOIN clients c ON c.id=ma.client_id
                WHERE ma.is_active=1 AND IFNULL(c.client_status,'')<>'deleted'
                ORDER BY LOWER(IFNULL(c.nickname,'')),c.id
                """
            ).fetchall()
            for row in rows:
                categories = experience._rating_categories(
                    conn, int(row["account_id"])
                )
                nickname = str(row["nickname"] or "").strip()
                if not categories.get("nickname") or not nickname:
                    continue
                profile_path = "p/" + public_profile_ref(
                    settings.secret_key, int(row["client_id"])
                )
                players.append(
                    {"display_name": nickname, "client_id": profile_path}
                )
        return templates.TemplateResponse(
            request,
            "players_directory.html",
            {
                "request": request,
                "member": member,
                "current_tab": "rating",
                "players": players,
                "asset_version": "prelaunch-v2",
            },
        )

    _move_latest_route_before_existing(app, "/players", "GET")

    @app.get("/players/p/{profile_ref}", response_class=HTMLResponse)
    async def privacy_safe_player_profile_ref(request: Request, profile_ref: str):
        viewer = _current_member(request, required=True)
        with connect(settings.db_path) as conn:
            client_id = resolve_public_profile_ref(
                conn,
                secret_key=settings.secret_key,
                profile_ref=profile_ref,
            )
            if client_id is None:
                return HTMLResponse("Профиль не найден", status_code=404)
            if int(viewer["client_id"]) == int(client_id):
                return RedirectResponse("/account?tab=profile", status_code=303)
            payload = experience._public_profile_payload(conn, client_id)
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
                if payload.get("categories", {}).get("place"):
                    jackside_place, hijack_place = _rating_places(conn, client_id)
                    payload["jackside"]["place"] = jackside_place
                    payload["hijack"]["place"] = hijack_place
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
                "asset_version": "prelaunch-v2",
            },
        )

    @app.get("/players/{client_id:int}")
    async def legacy_numeric_player_profile(request: Request, client_id: int):
        viewer = _current_member(request, required=True)
        if int(viewer["client_id"]) == int(client_id):
            return RedirectResponse("/account?tab=profile", status_code=303)
        return RedirectResponse(
            f"/players/p/{public_profile_ref(settings.secret_key, client_id)}",
            status_code=303,
        )

    _move_latest_route_before_existing(app, "/players/{client_id:int}", "GET")
    return app


__all__ = ["install_prelaunch_profile_privacy"]
