from __future__ import annotations

import inspect
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute


_ACTIVATE_ROUTE = "/account/rewards/{member_reward_id:int}/activate"


def cards_redirect_location(location: str | None) -> str | None:
    """Keep JACK CARD activation results on the My Cards sub-tab."""
    raw = str(location or "").strip()
    if not raw:
        return location
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or parsed.path != "/account":
        return raw
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if params.get("tab") != "vault":
        return raw
    params["store"] = "cards"
    return urlunsplit(("", "", parsed.path, urlencode(params), parsed.fragment))


def install_store_card_tab_hotfix(app: FastAPI) -> FastAPI:
    if getattr(app.state, "store_card_tab_hotfix_installed", False):
        return app
    app.state.store_card_tab_hotfix_installed = True

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path != _ACTIVATE_ROUTE or "POST" not in (route.methods or set()):
            continue
        if getattr(route, "_store_card_tab_hotfix_wrapped", False):
            return app

        original = route.dependant.call

        async def activation_redirect_wrapper(**kwargs):
            result = original(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, RedirectResponse):
                current = result.headers.get("location")
                corrected = cards_redirect_location(current)
                if corrected and corrected != current:
                    result.headers["location"] = corrected
            return result

        route.endpoint = activation_redirect_wrapper
        route.dependant.call = activation_redirect_wrapper
        setattr(route, "_store_card_tab_hotfix_wrapped", True)
        break

    return app


__all__ = ["cards_redirect_location", "install_store_card_tab_hotfix"]
