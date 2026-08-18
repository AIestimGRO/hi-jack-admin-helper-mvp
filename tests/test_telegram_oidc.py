from urllib.parse import parse_qs, urlparse

from app.services.telegram_oidc import TELEGRAM_SCOPE, authorization_url


def test_telegram_login_requests_bot_access():
    url = authorization_url(
        client_id="123456",
        redirect_uri="https://club-v2.hijackpoker.ru/auth/telegram/callback",
        state="state-1",
        code_challenge="challenge-1",
    )
    query = parse_qs(urlparse(url).query)

    assert TELEGRAM_SCOPE == "openid profile telegram:bot_access"
    assert query["scope"] == ["openid profile telegram:bot_access"]
