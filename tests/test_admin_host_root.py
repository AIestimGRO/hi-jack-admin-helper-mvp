from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_configured_production_admin_host_root_goes_to_login(tmp_path: Path) -> None:
    settings = Settings(
        admin_pin="2468",
        admin_name="Test Admin",
        secret_key="test-secret-key-that-is-longer-than-32-characters",
        db_path=tmp_path / "admin-root.sqlite3",
        secure_cookie=False,
        quiz_public_base_url="https://quiz.hijackpoker.ru",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/",
            headers={"host": "quiz.hijackpoker.ru"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
