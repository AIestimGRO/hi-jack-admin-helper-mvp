import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import connect
from app.main import create_app


ROOT = Path(__file__).resolve().parents[1]


def _settings(tmp_path) -> Settings:
    return Settings(
        public_base_url="http://testserver",
        quiz_public_base_url="http://testserver",
        master_login="master",
        admin_pin="1234",
        admin_name="Test Master",
        secret_key="test-secret-key-" * 4,
        db_path=tmp_path / "club-links.sqlite3",
        secure_cookie=False,
        member_portal_enabled=True,
    )


def _master_client(tmp_path) -> tuple[TestClient, Settings]:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))
    client.__enter__()
    login_page = client.get("/login")
    assert login_page.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert match
    response = client.post(
        "/login",
        data={
            "username": settings.master_login,
            "pin": settings.admin_pin,
            "csrf_token": match.group(1),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client, settings


def test_master_club_links_page_opens_and_has_seeded_links(tmp_path) -> None:
    client, settings = _master_client(tmp_path)
    try:
        response = client.get("/master/club-links")
        assert response.status_code == 200
        assert "Hi, Jack в сети" in response.text
        assert "Telegram-канал" in response.text
        assert "Hi, Jack Mini App" in response.text
        assert "Яндекс Карты" in response.text

        with connect(settings.db_path) as conn:
            rows = conn.execute(
                "SELECT code,url,is_active FROM club_social_links ORDER BY position,id"
            ).fetchall()
        assert [row["code"] for row in rows[:3]] == [
            "telegram",
            "miniapp",
            "yandex_maps",
        ]
        assert all(str(row["url"] or "") == "" for row in rows[:3])
        assert all(int(row["is_active"] or 0) == 0 for row in rows[:3])
    finally:
        client.__exit__(None, None, None)


def test_admin_simple_mutations_use_reload_free_transport() -> None:
    script = (ROOT / "app/static/js/prelaunch-admin.js").read_text(encoding="utf-8")

    assert "installGenericReloadFreeActions" in script
    assert "event.defaultPrevented" in script
    assert "new FormData(form)" in script
    assert "X-Requested-With" in script
    assert "window.scrollTo(scrollX, scrollY)" in script
    assert "'/master/club-links'" in script
    assert "refreshSelector = '.prelaunch-page'" in script

    # Login/logout and file imports remain deliberate full-navigation workflows.
    assert "['/login', '/logout']" in script
    assert "input[type=\"file\"]" in script
    assert "multipart/form-data" in script

    # Only an intentional redirect to another page may navigate the browser.
    assert "finalUrl.pathname !== location.pathname" in script
    assert "window.location.assign(finalUrl.href)" in script
