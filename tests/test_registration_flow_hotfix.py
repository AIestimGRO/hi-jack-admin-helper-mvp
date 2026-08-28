from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        admin_pin="2468",
        admin_name="Test Admin",
        secret_key="test-secret-key-that-is-longer-than-32-characters",
        db_path=tmp_path / "registration-flow.sqlite3",
        secure_cookie=False,
        public_base_url="https://club.example.test",
        quiz_public_base_url="https://quiz.example.test",
        smtp_host="smtp.example.test",
        smtp_from="club@example.test",
        member_portal_enabled=True,
    )


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_cached_consent_submit_is_ignored_and_registration_stays_on_profile(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    with client:
        page = client.get("/account/register")
        assert "data-registration-form" in page.text
        assert "data-consent-form" not in page.text
        token = _csrf(page.text)

        stale = client.post(
            "/account/register/consent",
            data={
                "csrf_token": token,
                "document_code": "privacy",
                "accepted": "true",
            },
            follow_redirects=False,
        )
        assert stale.status_code == 303
        assert stale.headers["location"] == "/account/register"

        page = client.get("/account/register")
        assert "data-registration-form" in page.text
        assert "data-consent-form" not in page.text


def test_legal_extra_accepts_adult_registration_state(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    with client:
        page = client.get("/account/register")
        token = _csrf(page.text)
        response = client.post(
            "/api/account/register/legal-extra",
            data={
                "csrf_token": token,
                "birth_date": "1990-01-01",
            },
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}
