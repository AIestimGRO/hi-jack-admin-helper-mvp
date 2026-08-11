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


def test_duplicate_consent_submit_does_not_reset_registration_flow(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    with client:
        first = client.get("/account/register")
        token = _csrf(first.text)
        accepted = client.post(
            "/account/register/consent",
            data={
                "csrf_token": token,
                "document_code": "privacy",
                "accepted": "true",
            },
            follow_redirects=False,
        )
        assert accepted.status_code == 303

        duplicate = client.post(
            "/account/register/consent",
            data={
                "csrf_token": token,
                "document_code": "privacy",
                "accepted": "true",
            },
            follow_redirects=False,
        )
        assert duplicate.status_code == 303

        second = client.get("/account/register")
        assert "data-consent-form" in second.text
        assert 'value="rewards"' in second.text
        assert 'value="privacy"' not in second.text

        token = _csrf(second.text)
        accepted = client.post(
            "/account/register/consent",
            data={
                "csrf_token": token,
                "document_code": "rewards",
                "accepted": "true",
            },
            follow_redirects=False,
        )
        assert accepted.status_code == 303

        duplicate = client.post(
            "/account/register/consent",
            data={
                "csrf_token": token,
                "document_code": "rewards",
                "accepted": "true",
            },
            follow_redirects=False,
        )
        assert duplicate.status_code == 303

        profile = client.get("/account/register")
        assert "data-registration-form" in profile.text
        assert "data-consent-form" not in profile.text


def test_legal_extra_persists_birth_date_for_request_code_gate(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    with client:
        page = client.get("/account/register")
        token = _csrf(page.text)
        response = client.post(
            "/api/account/register/legal-extra",
            data={
                "csrf_token": token,
                "birth_date": "1990-01-01",
                "marketing": "true",
            },
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}

        with client.session_transaction() as session:
            assert session["member_registration_birth_date"] == "1990-01-01"
            assert session["member_registration_marketing"] is True
