from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.parse
import urllib.request
from typing import Any

import jwt


TELEGRAM_AUTH_URL = "https://oauth.telegram.org/auth"
TELEGRAM_TOKEN_URL = "https://oauth.telegram.org/token"
TELEGRAM_JWKS_URL = "https://oauth.telegram.org/.well-known/jwks.json"
TELEGRAM_ISSUER = "https://oauth.telegram.org"
TELEGRAM_SCOPE = "openid profile telegram:bot_access"


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorization_url(
    *, client_id: str, redirect_uri: str, state: str, code_challenge: str
) -> str:
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": TELEGRAM_SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    return f"{TELEGRAM_AUTH_URL}?{query}"


def exchange_telegram_code(
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict[str, Any]:
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }).encode("ascii")
    credentials = base64.b64encode(
        f"{client_id}:{client_secret}".encode("utf-8")
    ).decode("ascii")
    request = urllib.request.Request(
        TELEGRAM_TOKEN_URL,
        data=body,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    id_token = str(payload.get("id_token", ""))
    if not id_token:
        raise ValueError("telegram_token_missing")
    jwks = jwt.PyJWKClient(TELEGRAM_JWKS_URL, timeout=15, lifespan=300)
    signing_key = jwks.get_signing_key_from_jwt(id_token)
    return jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=str(client_id),
        issuer=TELEGRAM_ISSUER,
        options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        leeway=30,
    )
