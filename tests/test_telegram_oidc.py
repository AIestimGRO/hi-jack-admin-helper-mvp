from urllib.parse import parse_qs, urlparse

import pytest

from app.services.telegram_oidc import (
    TELEGRAM_SCOPE,
    TELEGRAM_SCOPE_WITH_BOT_ACCESS,
    TELEGRAM_USER_ID_MAX,
    authorization_url,
    normalize_telegram_claims,
    telegram_user_id_from_claims,
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


def test_telegram_profile_id_is_used_instead_of_oidc_subject():
    claims = {
        "sub": "1234123412341234123",
        "id": 987654321,
        "preferred_username": "johndoe",
    }

    normalized = normalize_telegram_claims(claims)

    assert telegram_user_id_from_claims(claims) == "987654321"
    assert normalized["telegram_user_id"] == "987654321"
    assert normalized["sub"] == "987654321"
    assert normalized["oidc_sub"] == "1234123412341234123"
    assert normalized["preferred_username"] == "johndoe"


@pytest.mark.parametrize(
    "raw_id",
    [None, "", 0, -1, TELEGRAM_USER_ID_MAX + 1, True, "not-a-number"],
)
def test_telegram_profile_id_must_be_valid_bot_api_user_id(raw_id):
    with pytest.raises(ValueError):
        telegram_user_id_from_claims({"id": raw_id})


def test_normalization_requires_verified_oidc_subject():
    with pytest.raises(ValueError, match="telegram_oidc_subject_missing"):
        normalize_telegram_claims({"id": 987654321})
