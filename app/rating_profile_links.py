from __future__ import annotations

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from app.db import connect
from app.hijack_rating_paging import hijack_rating_page_payload
from app.product_shell import _current_member
from app.public_profile_refs import public_profile_ref
from app.services.jackside_analytics import build_jackside_analytics


def install_rating_profile_links(app: FastAPI) -> FastAPI:
    if getattr(app.state, "rating_profile_links_installed", False):
        return app
    app.state.rating_profile_links_installed = True
    settings = app.state.settings

    @app.get("/api/account/rating-profile-links")
    async def account_rating_profile_links(
        request: Request,
        section: str = Query("month"),
        period: str = Query("global"),
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=100),
    ):
        member = _current_member(request, required=True)
        with connect(settings.db_path) as conn:
            if section == "club":
                data = hijack_rating_page_payload(
                    conn,
                    client_id=int(member["client_id"]),
                    period=period,
                    offset=offset,
                    limit=limit,
                )
                rows = list(data.get("rows") or [])
            else:
                data = build_jackside_analytics(conn)
                key = section if section in {"today", "month", "all"} else "month"
                rows = list(data.get(key) or [])[offset : offset + limit]
        refs = [
            public_profile_ref(settings.secret_key, int(row["client_id"]))
            for row in rows
        ]
        return JSONResponse(
            {
                "section": section,
                "period": period if section == "club" else "",
                "offset": offset,
                "profile_refs": refs,
            }
        )

    return app


__all__ = ["install_rating_profile_links"]
