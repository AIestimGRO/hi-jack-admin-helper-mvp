from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute

from app.store_card_tab_hotfix import (
    cards_redirect_location,
    install_store_card_tab_hotfix,
)


def test_cards_redirect_location_preserves_message_and_selects_cards():
    location = "/account?tab=vault&ok=%D0%9D%D0%B0%D0%B3%D1%80%D0%B0%D0%B4%D0%B0"

    corrected = cards_redirect_location(location)

    assert corrected is not None
    assert corrected.startswith("/account?")
    assert "tab=vault" in corrected
    assert "store=cards" in corrected
    assert "ok=" in corrected


def test_cards_redirect_location_leaves_unrelated_redirect_unchanged():
    assert cards_redirect_location("/account?tab=profile") == "/account?tab=profile"
    assert cards_redirect_location("https://example.com/account?tab=vault") == (
        "https://example.com/account?tab=vault"
    )


def test_activation_route_redirects_back_to_my_cards():
    app = FastAPI()

    @app.post("/account/rewards/{member_reward_id:int}/activate")
    async def activate(member_reward_id: int):
        assert member_reward_id == 42
        return RedirectResponse(
            "/account?tab=vault&ok=activated",
            status_code=303,
        )

    install_store_card_tab_hotfix(app)

    route = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path == "/account/rewards/{member_reward_id:int}/activate"
    )

    import asyncio

    response = asyncio.run(route.dependant.call(member_reward_id=42))

    assert response.status_code == 303
    assert response.headers["location"] == "/account?tab=vault&ok=activated&store=cards"
