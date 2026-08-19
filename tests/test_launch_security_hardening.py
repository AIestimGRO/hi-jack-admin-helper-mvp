from __future__ import annotations

import re

from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import transaction
from app.main import create_app


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
