import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import transaction
from app.main import create_app
from app.services.clients import ensure_preferences, upsert_client


def make_client(tmp_path: Path) -> tuple[TestClient, Settings]:
    settings = Settings(
        admin_pin="2468",
        admin_name="Test Admin",
        secret_key="test-secret-key-that-is-longer-than-32-characters",
        db_path=tmp_path / "app.sqlite3",
        secure_cookie=False,
    )
    return TestClient(create_app(settings)), settings


def login(client: TestClient, username: str = "master", pin: str = "2468") -> None:
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/login",
        data={"username": username, "pin": pin, "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303


def csrf_from(client: TestClient, url: str) -> str:
    page = client.get(url)
    assert page.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def test_private_pages_require_login(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        assert client.get("/", follow_redirects=False).status_code == 303
        assert client.get("/api/clients/search").status_code == 401


def test_brand_theme_and_versioned_assets_are_used_everywhere(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        login_page = client.get("/login")
        asset_version = re.search(r"/static/css/theme.css\?v=([a-f0-9]{12})", login_page.text).group(1)
        assert f"/static/js/app.js?v={asset_version}" in login_page.text
        assert f"/static/img/brand/hi-jack-mark.webp?v={asset_version}" in login_page.text

        login(client)
        for url in ("/", "/clients", "/clients/import", "/logs", "/master", "/admin/quiz-results"):
            page = client.get(url)
            assert page.status_code == 200
            assert f"/static/css/theme.css?v={asset_version}" in page.text
            assert f"/static/js/app.js?v={asset_version}" in page.text

        quiz = client.get("/quiz?campaign=default")
        assert quiz.status_code == 200
        assert f"/static/css/quiz-theme.css?v={asset_version}" in quiz.text
        assert f"/static/js/quiz.js?v={asset_version}" in quiz.text

        with transaction(settings.db_path) as conn:
            campaign_id = conn.execute("SELECT id FROM quiz_campaigns WHERE code='default'").fetchone()[0]
        builder = client.get(f"/master/quiz-builder/{campaign_id}")
        assert builder.status_code == 200
        assert f"/static/css/builder.css?v={asset_version}" in builder.text


def test_login_qr_and_preference_operation(tmp_path, monkeypatch):
    client, settings = make_client(tmp_path)
    captured: dict[str, str] = {}

    class FakeQr:
        def save(self, output, format):
            output.write(b"fake-png")

    def fake_make(value, **kwargs):
        captured["value"] = value
        return FakeQr()

    monkeypatch.setattr("app.main.qrcode.make", fake_make)
    with client:
        login(client)
        with transaction(settings.db_path) as conn:
            client_id, _ = upsert_client(conn, {"app_user_id": "10", "first_name": "Иван", "phone_raw": "+7 999 123-45-67"})
            ensure_preferences(conn, client_id)

        qr = client.get(f"/api/clients/{client_id}/qr")
        assert qr.status_code == 200
        assert qr.content == b"fake-png"
        assert captured["value"] == "9991234567"

        token = csrf_from(client, f"/clients/{client_id}")
        result = client.post(
            "/api/preferences/add",
            data={
                "client_id": client_id,
                "code": "free_entry",
                "amount": 2,
                "reason": "бонус от клуба",
                "comment": "test",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert result.status_code == 303
        assert f"/clients/{client_id}" in result.headers["location"]


def test_master_creates_preference_and_standard_admin_has_no_master_access(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        login(client)
        token = csrf_from(client, "/master")
        created = client.post(
            "/api/master/preferences/create",
            data={
                "title": "Бесплатный коктейль",
                "kind": "counter",
                "code": "free_cocktail",
                "position": 50,
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert created.status_code == 303

        admin_created = client.post(
            "/api/master/admins/create",
            data={
                "username": "masha",
                "display_name": "Маша",
                "pin": "1357",
                "role": "admin",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert admin_created.status_code == 303

        with transaction(settings.db_path) as conn:
            client_id, _ = upsert_client(conn, {"app_user_id": "20", "first_name": "Олег", "phone_raw": "9995556677"})
        detail = client.get(f"/clients/{client_id}")
        assert "Бесплатный коктейль" in detail.text

        client.post("/logout", data={"csrf_token": token})
        login(client, username="masha", pin="1357")
        assert client.get("/clients").status_code == 200
        assert client.get("/master").status_code == 403
        assert client.post(
            "/api/master/preferences/create",
            data={"title": "Нельзя", "kind": "counter", "csrf_token": csrf_from(client, "/clients")},
        ).status_code == 403

    with transaction(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_audit_log").fetchone()[0] == 2
