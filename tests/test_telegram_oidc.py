from urllib.parse import parse_qs, urlparse

from app.services.telegram_oidc import (
    TELEGRAM_SCOPE,
    TELEGRAM_SCOPE_WITH_BOT_ACCESS,
    authorization_url,
)


def _scope_from_url() -> str:
    url = authorization_url(
        client_id="123456",
        redirect_uri="https://club-v2.hijackpoker.ru/auth/telegram/callback",
        state="state-1",
        code_challenge="challenge-1",
    )
    query = parse_qs(urlparse(url).query)
    return query["scope"][0]


def test_telegram_login_preserves_existing_scope_by_default(monkeypatch):
    monkeypatch.delenv("HJC_TELEGRAM_BOT_ACCESS_ENABLED", raising=False)

    assert TELEGRAM_SCOPE == "openid profile"
    assert _scope_from_url() == "openid profile"


def test_telegram_login_requests_bot_access_when_enabled(monkeypatch):
    monkeypatch.setenv("HJC_TELEGRAM_BOT_ACCESS_ENABLED", "true")

    assert TELEGRAM_SCOPE_WITH_BOT_ACCESS == "openid profile telegram:bot_access"
    assert _scope_from_url() == "openid profile telegram:bot_access"
