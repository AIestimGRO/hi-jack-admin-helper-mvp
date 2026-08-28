from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def legacy_password_fixture(request, monkeypatch):
    """Keep unrelated legacy-flow tests focused on their original behavior.

    Launch hardening raises the real product minimum password length to eight
    characters. A number of older tests seed synthetic accounts with the
    historical six-character fixture value. Those tests are not password-policy
    tests, so keep their fixture contract without weakening production behavior.
    The dedicated launch-security tests exercise the new eight-character policy.
    """
    module = getattr(request, "module", None)
    module_name = module.__name__.split(".")[-1] if module is not None else ""
    if module_name != "test_launch_security_hardening":
        monkeypatch.setattr("app.services.member_accounts.MIN_PASSWORD_LENGTH", 6)
    yield


@pytest.fixture(autouse=True)
def current_member_registration_contract(request, monkeypatch):
    """Adapt legacy member-account tests to registration without legal consent."""
    module = getattr(request, "module", None)
    if module is None or module.__name__.split(".")[-1] != "test_member_accounts":
        yield
        return

    def accept_registration_documents(client) -> None:
        # The helper name is kept for legacy tests, but registration no longer
        # presents or requires legal-document consent checkboxes. Browser JS
        # still persists the adult birth date before the request-code submit.
        profile = client.get("/account/register")
        assert profile.status_code == 200
        assert "data-registration-form" in profile.text
        assert "data-consent-form" not in profile.text
        assert 'type="checkbox"' not in profile.text
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
