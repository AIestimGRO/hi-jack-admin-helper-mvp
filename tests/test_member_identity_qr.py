from pathlib import Path

from app.admin_vault_scanner import _client_phone_from_scan
from app.member_identity_qr import member_identity_payload


ROOT = Path(__file__).resolve().parents[1]


def test_member_identity_qr_uses_existing_admin_client_scan_payload() -> None:
    payload = member_identity_payload("+7 999 123-45-67")

    assert payload == "9991234567"
    assert _client_phone_from_scan(payload) == payload


def test_member_identity_qr_requires_valid_local_phone() -> None:
    try:
        member_identity_payload("123")
    except ValueError as exc:
        assert str(exc) == "member_phone_unavailable"
    else:
        raise AssertionError("invalid member phone must not produce an identity QR")


def test_member_identity_qr_button_is_only_for_home_and_profile_header() -> None:
    html = (ROOT / "app/templates/member_base.html").read_text(encoding="utf-8")

    assert "current_tab|default('') in ['home', 'profile']" in html
    assert "data-member-identity-qr-open" in html
    assert "member-topbar-actions" in html
    assert ">QR<" not in html
    assert 'src="/account/identity/qr.png"' in html


def test_member_identity_qr_is_rendered_as_modal_club_card() -> None:
    html = (ROOT / "app/templates/member_base.html").read_text(encoding="utf-8")
    css = (ROOT / "app/static/css/member-identity-qr.css").read_text(encoding="utf-8")
    js = (ROOT / "app/static/js/member-identity-qr.js").read_text(encoding="utf-8")

    assert "data-member-identity-qr-dialog" in html
    assert "Моя клубная карта" in html
    assert "width: 42px" in css
    assert "height: 42px" in css
    assert "dialog.showModal" in js
    assert "member-identity-qr-open" in js


def test_member_identity_qr_route_is_authenticated_and_not_cached() -> None:
    source = (ROOT / "app/member_identity_qr.py").read_text(encoding="utf-8")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")

    assert '@app.get("/account/identity/qr.png")' in source
    assert "_current_member(request, required=True)" in source
    assert '"Cache-Control": "private, no-store"' in source
    assert "qrcode.make(payload)" in source
    assert "install_member_identity_qr(application)" in main
