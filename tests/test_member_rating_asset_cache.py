from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_member_rating_assets_are_cache_busted_and_single_controller() -> None:
    script = (ROOT / "app/static/js/member-profile-refresh.js").read_text(encoding="utf-8")

    assert "/static/js/hijack-member.js?v=3" in script
    assert "/static/js/hijack-member.js?v=2" not in script
    assert "/static/css/hijack-member.css?v=3" in script
    assert "hijack-rating-global-ui.js" not in script


def test_hi_titles_assets_and_locked_visual_priority() -> None:
    loader = (ROOT / "app/static/js/member-profile-refresh.js").read_text(encoding="utf-8")
    profile = (ROOT / "app/static/js/profile-experience-v2.js").read_text(encoding="utf-8")
    styles = (ROOT / "app/static/css/member-achievement-carousel.css").read_text(encoding="utf-8")

    assert "/static/css/member-achievement-carousel.css?v=4" in loader
    assert "/static/js/profile-experience-v2.js?v=3" in loader
    assert "function prioritizeCollection(items)" in profile
    assert "leftLocked - rightLocked" in profile
    assert 'grid-template-rows: repeat(2, 158px)' in styles
    assert ".profile-emblem-card.is-locked" in styles
    assert "filter: grayscale(1) saturate(0) !important" in styles
    assert ".profile-emblem-card.is-active-title" in styles
