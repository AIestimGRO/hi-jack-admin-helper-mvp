from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_member_rating_assets_are_cache_busted_and_single_controller() -> None:
    script = (ROOT / "app/static/js/member-profile-refresh.js").read_text(encoding="utf-8")

    assert "/static/js/hijack-member.js?v=3" in script
    assert "/static/js/hijack-member.js?v=2" not in script
    assert "/static/css/hijack-member.css?v=3" in script
    assert "hijack-rating-global-ui.js" not in script
