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


def test_scanner_uses_same_origin_pinned_jsqr_dependency() -> None:
    source = (ROOT / "app/static/js/admin-vault-scanner.js").read_text(
        encoding="utf-8"
    )
    gitmodules = (ROOT / ".gitmodules").read_text(encoding="utf-8")

    assert "const QR_DECODER_URL = '/static/vendor/jsqr/dist/jsQR.js';" in source
    assert "https://unpkg.com" not in source
    assert "https://cdn.jsdelivr.net" not in source
    assert "async function ensureJsQR()" in source
    assert "await ensureJsQR()" in source
    assert "app/static/vendor/jsqr" in gitmodules
    assert "https://github.com/cozmo/jsQR.git" in gitmodules


def test_scanner_opens_as_fullscreen_camera_overlay() -> None:
    source = (ROOT / "app/static/js/admin-vault-scanner.js").read_text(
        encoding="utf-8"
    )
    css = (ROOT / "app/static/css/admin-vault-scanner.css").read_text(
        encoding="utf-8"
    )

    assert "function setFullscreenOpen(open)" in source
    assert "root.classList.toggle('is-open', open)" in source
    assert "document.documentElement.classList.toggle('vault-scanner-open', open)" in source
    assert "document.body.classList.toggle('vault-scanner-open', open)" in source
    assert "setFullscreenOpen(true)" in source
    assert "setFullscreenOpen(false)" in source
    assert ".vault-scanner.is-open .vault-scanner-panel" in css
    assert "position: fixed" in css
    assert "height: 100dvh" in css
    assert ".vault-scanner.is-open [data-vault-scan-stop]" in css
    assert "env(safe-area-inset-top" in css


def test_camera_permission_is_scoped_to_admin_vault() -> None:
    source = (ROOT / "app/admin_vault_scanner.py").read_text(encoding="utf-8")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")

    assert 'request.url.path == "/admin/vault"' in source
    assert '"camera=(self), microphone=(), geolocation=()"' in source
    assert "install_admin_vault_scanner(application)" in main
