from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_vault_scanner_reuses_existing_redeem_action() -> None:
    html = (ROOT / "app/templates/vault.html").read_text(encoding="utf-8")

    assert "data-vault-scanner" in html
    assert "data-vault-scan-video" in html
    assert "data-vault-scan-start" in html
    assert "data-vault-redeem-form" in html
    assert 'action="/api/vault/redeem"' in html
    assert 'name="csrf_token"' in html
    assert "jsqr@1.4.0/dist/jsQR.js" in html
    assert "/js/admin-vault-scanner.js" in html
    assert "/css/admin-vault-scanner.css" in html


def test_scanner_extracts_card_code_and_requests_confirmed_redeem() -> None:
    source = (ROOT / "app/static/js/admin-vault-scanner.js").read_text(
        encoding="utf-8"
    )

    assert "navigator.mediaDevices.getUserMedia" in source
    assert "facingMode: { ideal: 'environment' }" in source
    assert "new window.BarcodeDetector({ formats: ['qr_code'] })" in source
    assert "window.jsQR" in source
    assert "parsed.pathname.replace(/\\/+$/, '') !== '/admin/vault'" in source
    assert "parsed.searchParams.get('code')" in source
    assert "form.submit(" not in source
    assert "form.requestSubmit()" in source
    assert "Подтвердите сжигание JACK CARD" in source


def test_camera_permission_is_scoped_to_admin_vault() -> None:
    source = (ROOT / "app/admin_vault_scanner.py").read_text(encoding="utf-8")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")

    assert 'request.url.path == "/admin/vault"' in source
    assert '"camera=(self), microphone=(), geolocation=()"' in source
    assert "install_admin_vault_scanner(application)" in main
