from __future__ import annotations

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse

from app.db import transaction
from app.legal_registration import (
    _move_latest_route_before_existing,
    _record_rating_consent,
)
from app.product_shell import _check_csrf, _current_member


PUBLIC_RATING_CATEGORY_KEYS = (
    "nickname",
    "avatar",
    "result",
    "place",
    "achievements",
    "titles",
)


def install_public_rating_consent_policy(app: FastAPI) -> FastAPI:
    """Keep public-rating consent explicit, fixed and free of custom conditions."""
    if getattr(app.state, "public_rating_consent_policy_installed", False):
        return app
    app.state.public_rating_consent_policy_installed = True
    settings = app.state.settings

    @app.post("/account/legal/public-rating")
    async def member_public_rating_consent_policy_update(
        request: Request,
        nickname: bool = Form(False),
        avatar: bool = Form(False),
        result: bool = Form(False),
        place: bool = Form(False),
        achievements: bool = Form(False),
        titles: bool = Form(False),
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
        }
        with transaction(settings.db_path) as conn:
            _record_rating_consent(
                conn,
                settings=settings,
                request=request,
                account_id=int(member["id"]),
                categories=categories,
                conditions_text="",
            )
        return RedirectResponse(
            "/account/legal?ok=Настройки+публичного+рейтинга+сохранены",
            status_code=303,
        )

    _move_latest_route_before_existing(app, "/account/legal/public-rating", "POST")
    return app


__all__ = ["PUBLIC_RATING_CATEGORY_KEYS", "install_public_rating_consent_policy"]
