from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_member_ui_cleanup_bundle_is_loaded() -> None:
    loader = (ROOT / "app/static/js/member-profile-refresh.js").read_text(encoding="utf-8")

    assert "member-ui-cleanup.css?v=1" in loader
    assert "member-ui-cleanup.js?v=1" in loader


def test_member_ui_cleanup_covers_requested_shell_changes() -> None:
    script = (ROOT / "app/static/js/member-ui-cleanup.js").read_text(encoding="utf-8")
    css = (ROOT / "app/static/css/member-ui-cleanup.css").read_text(encoding="utf-8")

    assert "Ближайшая игра" in script
    assert "home-jack-cards-link" in script
    assert "href = '/account?tab=vault'" in script
    assert "Главное в цифрах" in script
    assert "Рефералы" in script
    assert "is-empty-referral-stats" in script
    assert "store-balance-chip" in script
    assert "jackside-rating-note" in script
    assert "chat-collapsed" in script
    assert "stableChatReplacement" in script

    assert ".member-app-body.member-tab-rating .member-topbar" in css
    assert ".member-app-body.member-tab-vault .member-topbar" in css
    assert ".member-app-body.member-tab-quizzes .member-topbar" in css
    assert ".store-balance-chip" in css
    assert ".is-stable-chat.chat-collapsed" in css
    assert "min-height: 164px" in css


def test_profile_rich_blocks_require_engagement_context() -> None:
    base = (ROOT / "app/templates/member_base.html").read_text(encoding="utf-8")

    assert (
        "{% if current_tab|default('') == 'profile' and engagement is defined %}"
        in base
    )
