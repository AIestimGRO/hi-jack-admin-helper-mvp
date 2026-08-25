from pathlib import Path

from app.admin_vault_scanner import (
    _card_code_from_scan,
    _client_id_from_scan,
    _client_phone_from_scan,
)


ROOT = Path(__file__).resolve().parents[1]


def test_vault_scanner_reuses_existing_redeem_action() -> None:
    html = (ROOT / "app/templates/vault.html").read_text(encoding="utf-8")

    assert "data-vault-scanner" in html
    assert "data-vault-scan-video" in html
    assert "data-vault-scan-start" in html
    assert "data-vault-redeem-form" in html
    assert 'action="/api/vault/redeem"' in html
    assert 'name="csrf_token"' in html
    assert 'class="primary vault-burn-button" type="submit"' in html
    assert "onsubmit=\"return confirm('Сжечь эту JACK CARD?" in html
    assert "/js/admin-vault-scanner.js" in html
    assert "/css/admin-vault-scanner.css" in html


def test_vault_code_input_is_locked_until_explicit_manual_tap() -> None:
    source = (ROOT / "app/static/js/admin-vault-scanner.js").read_text(
        encoding="utf-8"
    )

    assert "codeInput.readOnly = true" in source
    assert "codeInput.addEventListener('pointerdown', unlockCodeInput)" in source
    assert "window.addEventListener('pageshow', lockCodeInput)" in source
    assert "if (document.activeElement === codeInput) codeInput.blur()" in source


def test_scan_only_fills_code_and_never_submits_redeem_form() -> None:
    source = (ROOT / "app/static/js/admin-vault-scanner.js").read_text(
        encoding="utf-8"
    )

    assert "navigator.mediaDevices.getUserMedia" in source
    assert "facingMode: { ideal: 'environment' }" in source
    assert "new window.BarcodeDetector({ formats: ['qr_code'] })" in source
    assert "window.jsQR" in source
    assert "parsed.pathname.replace(/\\/+$/, '') !== '/admin/vault'" in source
    assert "parsed.searchParams.get('code')" in source
    assert "codeInput.value = code" in source
    assert "form.submit(" not in source
    assert "form.requestSubmit(" not in source
    assert ".click()" not in source
    assert "burnButton.focus" in source
    assert "Нажмите «Сжечь JACK CARD», чтобы продолжить" in source


def test_scanner_uses_only_same_origin_pinned_jsqr_dependency() -> None:
    source = (ROOT / "app/static/js/admin-vault-scanner.js").read_text(
        encoding="utf-8"
    )
    vault_html = (ROOT / "app/templates/vault.html").read_text(encoding="utf-8")
    clients_html = (ROOT / "app/templates/admin_clients_workspace.html").read_text(
        encoding="utf-8"
    )
    gitmodules = (ROOT / ".gitmodules").read_text(encoding="utf-8")

    assert "const QR_DECODER_URL = '/static/vendor/jsqr/dist/jsQR.js';" in source
    assert "https://unpkg.com" not in source
    assert "https://cdn.jsdelivr.net" not in source
    assert "https://unpkg.com" not in vault_html
    assert "https://cdn.jsdelivr.net" not in vault_html
    assert "https://unpkg.com" not in clients_html
    assert "https://cdn.jsdelivr.net" not in clients_html
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


def test_clients_workspace_replaces_import_with_scanner() -> None:
    html = (ROOT / "app/templates/admin_clients_workspace.html").read_text(
        encoding="utf-8"
    )

    assert "data-client-scan-start" in html
    assert ">Сканер</button>" in html
    assert 'href="/clients/import"' not in html
    assert "data-client-scanner" in html
    assert "data-client-scan-card-result" in html
    assert 'data-client-scan-redeem data-no-ajax="true"' in html
    assert 'action="/api/vault/redeem"' in html
    assert "/js/admin-vault-scanner.js" in html
    assert "/css/admin-vault-scanner.css" in html


def test_client_scanner_resolves_client_or_card_without_auto_burn() -> None:
    source = (ROOT / "app/static/js/admin-vault-scanner.js").read_text(
        encoding="utf-8"
    )

    assert "fetch('/api/master/qr/resolve'" in source
    assert "payload.kind === 'client' || payload.kind === 'client_search'" in source
    assert "window.location.assign(payload.url)" in source
    assert "payload.kind !== 'card'" in source
    assert "redeemForm?.addEventListener('submit'" in source
    assert "window.confirm('Сжечь эту JACK CARD?" in source
    assert "fetch(redeemForm.action" in source
    assert "JACK CARD распознана. Карта не списана" in source
    assert "form.requestSubmit(" not in source
    assert "form.submit(" not in source


def test_qr_value_helpers_distinguish_client_and_card_formats() -> None:
    assert _client_phone_from_scan("9991234567") == "9991234567"
    assert _client_phone_from_scan("+7 (999) 123-45-67") == "9991234567"
    assert _client_phone_from_scan("89991234567") == "9991234567"
    assert _client_phone_from_scan("1234") is None
    assert _client_id_from_scan("https://quiz-v2.hijackpoker.ru/clients/42") == 42
    assert _client_id_from_scan("/clients/42") == 42
    assert _card_code_from_scan("1234") == "1234"
    assert _card_code_from_scan("9991234567") is None
    assert (
        _card_code_from_scan("https://quiz-v2.hijackpoker.ru/admin/vault?code=JC-ABCD-EFGH")
        == "JC-ABCD-EFGH"
    )


def test_qr_resolver_reuses_existing_clients_and_vault_tables() -> None:
    source = (ROOT / "app/admin_vault_scanner.py").read_text(encoding="utf-8")

    assert '@app.post("/api/master/qr/resolve"' in source
    assert "_require_master(request)" in source
    assert "_check_csrf(request, csrf_token)" in source
    assert "FROM clients" in source
    assert "WHERE phone_local=?" in source
    assert "FROM vault_member_rewards vmr" in source
    assert "JOIN vault_catalog_rewards" in source
    assert '"redeem_code": card_code' in source
    assert '"url": f"/clients/{client_id}"' in source


def test_camera_permission_is_scoped_to_scanner_pages() -> None:
    source = (ROOT / "app/admin_vault_scanner.py").read_text(encoding="utf-8")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")

    assert '_CAMERA_PATHS = frozenset({"/admin/vault", "/master/clients"})' in source
    assert "request.url.path in _CAMERA_PATHS" in source
    assert '"camera=(self), microphone=(), geolocation=()"' in source
    assert "install_admin_vault_scanner(application)" in main
