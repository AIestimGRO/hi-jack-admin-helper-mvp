from pathlib import Path

from app.config import Settings
from app.main import create_app
from app.public_rating_consent_policy import PUBLIC_RATING_CATEGORY_KEYS


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        admin_pin="2468",
        admin_name="Test Admin",
        secret_key="test-secret-key-that-is-longer-than-32-characters",
        db_path=tmp_path / "rating-consent.sqlite3",
        secure_cookie=False,
        public_base_url="https://club.example.test",
        quiz_public_base_url="https://quiz.example.test",
        member_portal_enabled=True,
    )


def test_public_rating_consent_has_fixed_minimal_categories() -> None:
    assert PUBLIC_RATING_CATEGORY_KEYS == (
        "nickname",
        "avatar",
        "result",
        "place",
        "achievements",
        "titles",
    )

    root = Path(__file__).resolve().parents[1]
    template = (root / "app/templates/member_legal_preferences.html").read_text(
        encoding="utf-8"
    )

    for name in PUBLIC_RATING_CATEGORY_KEYS:
        assert f'name="{name}"' in template

    assert 'name="participation_stats"' not in template
    assert 'name="conditions_text"' not in template
    assert "Ограничения или условия распространения" not in template
    assert "отозвать разрешение" in template


def test_fixed_rating_policy_route_precedes_legacy_handler(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    handlers = []
    for route in app.router.routes:
        if getattr(route, "path", None) != "/account/legal/public-rating":
            continue
        if "POST" not in (getattr(route, "methods", set()) or set()):
            continue
        handlers.append(getattr(route.endpoint, "__name__", ""))

    assert handlers
    assert handlers[0] == "member_public_rating_consent_policy_update"
