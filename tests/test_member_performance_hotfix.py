from __future__ import annotations

from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR, Settings
from app.main import create_app
from app.member_performance import VAULT_PAGE_SIZE, _decorate_collection, _display_name


def test_display_name_prefers_nickname_over_first_name() -> None:
    member = {
        "nickname": "BUNNY BLESS",
        "first_name": "Alex",
        "username": "MIMalex",
    }
    assert _display_name(member) == "BUNNY BLESS"


def test_collection_is_server_prioritized_like_final_client_view() -> None:
    payload = {
        "active_count": 2,
        "total_count": 3,
        "items": [
            {"name": "Unlocked", "state": "active", "kind": "title", "selected": False},
            {"name": "Locked", "state": "locked", "kind": "achievement", "selected": False},
            {"name": "Selected", "state": "active", "kind": "title", "selected": True},
        ],
    }
    result = _decorate_collection(payload)
    assert [item["name"] for item in result["items"]] == [
        "Selected",
        "Unlocked",
        "Locked",
    ]
    assert result["unlocked_count"] == 2
    assert result["items"][2]["subtitle"] == "Не открыто · достижение"


def test_fast_member_templates_compile_and_store_page_size_is_six() -> None:
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
    assert templates.get_template("member_profile_fast.html") is not None
    assert templates.get_template("member_vault_fast.html") is not None
    assert templates.get_template("_vault_catalog_cards_fast.html") is not None
    assert VAULT_PAGE_SIZE == 6


def test_member_performance_is_wired_into_runtime(tmp_path) -> None:
    settings = Settings(
        admin_pin="2468",
        admin_name="Performance Test",
        secret_key="performance-test-secret-key-longer-than-32-chars",
        db_path=tmp_path / "member-performance.sqlite3",
        secure_cookie=False,
        public_base_url="https://club.example.test",
        quiz_public_base_url="https://quiz.example.test",
        member_portal_enabled=True,
    )
    app = create_app(settings)

    assert app.state.member_performance_installed is True
    assert any(
        isinstance(route, APIRoute) and route.path == "/api/account/vault-catalog-page"
        for route in app.routes
    )
    account_route = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path == "/account"
        and "GET" in (route.methods or set())
    )
    assert getattr(account_route, "_member_performance_wrapped", False) is True
