from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_member_app_uses_server_first_base_without_legacy_cosmetic_bundles() -> None:
    template = (ROOT / "app/templates/member_account.html").read_text(encoding="utf-8")
    base = (ROOT / "app/templates/member_app_base.html").read_text(encoding="utf-8")

    assert '{% extends "member_app_base.html" %}' in template
    assert "member_first_paint_state" in base
    assert "member-app-interactions.js" in base
    assert "member-profile-refresh.js" not in base
    assert "product-shell.js" not in base
    assert "prelaunch-member.js" not in base
    assert "member-final-polish.js" not in base
    assert "profile-experience-v2.js" not in base
    assert "account-security.js" not in base


def test_first_paint_contains_final_home_store_rating_and_profile_dom() -> None:
    template = (ROOT / "app/templates/member_account.html").read_text(encoding="utf-8")

    assert '<div class="jc-wallet-balance"><strong>{{ balance }}</strong></div>' in template
    assert "Играй. Копи. Меняй." in template
    assert ">Hi, Store <span aria-hidden=\"true\">→</span>" in template
    assert "home-nearest-clean" in template
    assert "schedule-tabs" in template
    assert "data-schedule-panel=\"tournaments\"" in template
    assert "store-tabs" in template
    assert "data-store-panel=\"cards\"" in template
    assert "Hi, Titles!" in template
    assert "profile-emblem-grid" in template
    assert "referral-tree-shell" in template
    assert "account-security-panel" in template
    assert "data-hijack-rating-hub" in template
    assert ">Год</a>" in template


def test_interaction_bundle_does_not_build_first_paint_or_fetch_shell_state() -> None:
    script = (ROOT / "app/static/js/member-app-interactions.js").read_text(
        encoding="utf-8"
    )

    for forbidden in (
        "/api/account/product-shell",
        "/api/account/club-links",
        "/api/account/jackside-calendar-rating",
        ".replaceWith(",
        ".home-heading')?.remove",
        "profile-rich-blocks",
        "stableChatReplacement",
    ):
        assert forbidden not in script

    assert "data-store-tab" in script
    assert "data-schedule-tab" in script
    assert "/api/account/hijack-rating-page" in script
    assert "/api/account/rating-profile-links" in script


def test_bottom_navigation_markup_is_preserved_in_server_first_base() -> None:
    old_base = (ROOT / "app/templates/member_base.html").read_text(encoding="utf-8")
    new_base = (ROOT / "app/templates/member_app_base.html").read_text(encoding="utf-8")

    start_marker = '<nav class="member-bottom-nav"'
    end_marker = "</nav>"
    old_start = old_base.index(start_marker)
    new_start = new_base.index(start_marker)
    old_nav = old_base[old_start : old_base.index(end_marker, old_start) + len(end_marker)]
    new_nav = new_base[new_start : new_base.index(end_marker, new_start) + len(end_marker)]
    assert new_nav == old_nav


def test_server_first_state_is_installed_after_data_integrity() -> None:
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    helper = (ROOT / "app/member_first_paint.py").read_text(encoding="utf-8")

    assert main.index("install_prelaunch_data_integrity(application)") < main.index(
        "install_member_first_paint(application)"
    )
    assert "Jinja2Templates" in helper
    assert 'env.globals["member_first_paint_state"]' in helper
    assert 'env.globals["member_profile_ref"]' in helper
    assert "calendar_jackside_rating_payload" in helper
    assert "hijack_rating_page_payload" in helper
    assert "referral_tree" in helper
    assert "title_collection_payload" in helper
