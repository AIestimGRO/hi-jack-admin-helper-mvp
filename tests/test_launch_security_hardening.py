from __future__ import annotations

import io
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.db import transaction
from app.launch_security_hardening import (
    _privacy_safe_rating_categories,
    _safe_open_image,
)
from app.main import create_app
from app.services.member_accounts import _session_touch_due, validate_password


def _settings(tmp_path) -> Settings:
    return Settings(
        admin_pin="2468",
        admin_name="Security Test",
        secret_key="security-test-secret-key-longer-than-32-characters",
        db_path=tmp_path / "launch-security.sqlite3",
        secure_cookie=False,
        public_base_url="https://club.example.test",
        quiz_public_base_url="https://quiz.example.test",
        member_portal_enabled=True,
    )


def _csrf(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def _login(client: TestClient) -> None:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "username": "master",
            "pin": "2468",
            "csrf_token": _csrf(page),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def _profile_consent_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE member_public_rating_consent_state(
            account_id INTEGER PRIMARY KEY,
            document_version TEXT NOT NULL,
            categories_json TEXT NOT NULL,
            granted INTEGER NOT NULL
        );
        CREATE TABLE legal_reference_documents(
            code TEXT NOT NULL,
            version TEXT NOT NULL,
            is_active INTEGER NOT NULL
        );
        CREATE TABLE member_profile_sharing(
            account_id INTEGER PRIMARY KEY,
            share_game_profile INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE member_profile_visibility(
            account_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            is_visible INTEGER NOT NULL,
            PRIMARY KEY(account_id, category)
        );
        """
    )
    conn.execute(
        "INSERT INTO legal_reference_documents(code,version,is_active) VALUES ('public-rating-consent','v1',1)"
    )
    return conn


def test_public_deep_health_probe_is_disabled(tmp_path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.post("/health/deep")
        assert response.status_code == 404
        assert response.json() == {"error": "not_found"}


def test_stale_admin_session_is_rejected_globally(tmp_path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        _login(client)
        assert client.get("/staff-access").status_code == 200

        with transaction(settings.db_path) as conn:
            conn.execute(
                "UPDATE admins SET session_version=session_version+1 WHERE username='master'"
            )

        response = client.get("/staff-access", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_scheme_relative_redirect_is_sanitized_and_headers_are_present(tmp_path) -> None:
    app = create_app(_settings(tmp_path))

    @app.get("/security-test-redirect")
    async def unsafe_redirect_for_test():
        return RedirectResponse("//evil.example/path", status_code=303)

    with TestClient(app) as client:
        response = client.get("/security-test-redirect", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/account"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "same-origin"


def test_private_routes_are_not_browser_cached(tmp_path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.get("/account/login")
        assert response.headers["cache-control"] == "no-store"


def test_public_profile_requires_current_explicit_legal_consent() -> None:
    conn = _profile_consent_conn()
    try:
        assert _privacy_safe_rating_categories(conn, 1) == {}
        conn.execute(
            """
            INSERT INTO member_public_rating_consent_state(
                account_id,document_version,categories_json,granted
            ) VALUES (1,'v1',?,1)
            """,
            ('{"nickname":true,"avatar":true,"participation_stats":true}',),
        )
        conn.execute(
            "INSERT INTO member_profile_visibility(account_id,category,is_visible) VALUES (1,'avatar',0)"
        )
        allowed = _privacy_safe_rating_categories(conn, 1)
        assert allowed["nickname"] is True
        assert allowed["avatar"] is False
        assert allowed["game_stats"] is True
        assert allowed["game_history"] is True

        conn.execute(
            "UPDATE member_public_rating_consent_state SET document_version='old' WHERE account_id=1"
        )
        assert _privacy_safe_rating_categories(conn, 1) == {}
    finally:
        conn.close()


def test_image_dimensions_are_rejected_before_pixel_load(monkeypatch) -> None:
    original_open = Image.open
    load_called = False

    class OversizedImage:
        width = 9000
        height = 64

        def load(self):
            nonlocal load_called
            load_called = True

    monkeypatch.setattr(Image, "open", lambda *_args, **_kwargs: OversizedImage())
    with pytest.raises(ValueError, match="слишком большое"):
        _safe_open_image(b"fake")
    assert load_called is False
    monkeypatch.setattr(Image, "open", original_open)


def test_safe_image_decoder_accepts_normal_image() -> None:
    output = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(output, format="PNG")
    image = _safe_open_image(output.getvalue())
    assert image.size == (64, 64)


def test_session_touch_is_throttled_to_five_minutes() -> None:
    now = datetime(2026, 8, 19, 6, 30, tzinfo=timezone.utc)
    assert _session_touch_due(None, now) is True
    assert _session_touch_due((now - timedelta(minutes=6)).isoformat(), now) is True
    assert _session_touch_due((now - timedelta(minutes=1)).isoformat(), now) is False


def test_new_passwords_require_at_least_eight_characters() -> None:
    with pytest.raises(ValueError, match="8 до 128"):
        validate_password("abc1234")
    assert validate_password("abc12345") == "abc12345"
