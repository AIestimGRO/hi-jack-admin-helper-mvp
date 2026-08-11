from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def current_member_registration_contract(request, monkeypatch):
    """Adapt legacy member-account tests to the current legal registration steps."""
    module = getattr(request, "module", None)
    if module is None or module.__name__.split(".")[-1] != "test_member_accounts":
        yield
        return

    def accept_registration_documents(client) -> None:
        agreement = client.get("/account/register")
        assert agreement.status_code == 200
        assert (
            "Пользовательское соглашение и Правила Hi, Jack! Club"
            in agreement.text
        )
        response = client.post(
            "/account/register/consent",
            data={
                "document_code": "privacy",
                "accepted": "true",
                "csrf_token": module.csrf_from(agreement),
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        consent = client.get("/account/register")
        assert "Согласие на обработку персональных данных" in consent.text
        response = client.post(
            "/account/register/consent",
            data={
                "document_code": "rewards",
                "accepted": "true",
                "csrf_token": module.csrf_from(consent),
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        # Browser JS persists this before request-code. Legacy tests call the
        # request-code endpoint directly, so mirror that browser-side step here.
        profile = client.get("/account/register")
        legal_extra = client.post(
            "/api/account/register/legal-extra",
            data={
                "birth_date": "1992-10-01",
                "csrf_token": module.csrf_from(profile),
            },
            follow_redirects=False,
        )
        assert legal_extra.status_code == 200
        assert legal_extra.json()["ok"] is True

    monkeypatch.setattr(
        module,
        "accept_registration_documents",
        accept_registration_documents,
    )
    yield
