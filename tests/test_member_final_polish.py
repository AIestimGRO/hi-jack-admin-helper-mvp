from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_final_polish_bundle_is_loaded_in_stable_order() -> None:
    loader = (ROOT / "app/static/js/member-profile-refresh.js").read_text(encoding="utf-8")

    compatibility = loader.index("prelaunch-ui-hotfix.css?v=2")
    final_polish = loader.index("member-final-polish.css?v=1")
    refinement = loader.index("member-brand-refinement.css?v=2")
    assert compatibility < final_polish < refinement
    assert "member-final-polish.js?v=1" in loader
    assert "data-prelaunch-ui-hotfix" in loader


def test_wallet_and_hijack_rating_final_polish() -> None:
    script = (ROOT / "app/static/js/member-final-polish.js").read_text(encoding="utf-8")
    css = (ROOT / "app/static/css/member-final-polish.css").read_text(encoding="utf-8")

    assert "Играй. Копи. Меняй." in script
    assert ".jc-wallet-balance small" in script
    assert "Твоё место в рейтинге" in script
    assert "hijack-rating-head-compact" in script
    assert ".hijack-rating-caption" in css
    assert "grid-template-columns: repeat(3" in css


def test_brand_refinement_uses_blue_and_sea_wave_without_nav_override() -> None:
    css = (ROOT / "app/static/css/member-brand-refinement.css").read_text(encoding="utf-8")

    assert "--hj-blue: #005b7d" in css
    assert "--hj-sea: #095c57" in css
    assert '[data-account-tab="home"] .jc-wallet-card' in css
    assert '[data-account-tab="quizzes"] .campaign-card' in css
    assert ".referral-tree-root" in css
    assert ".member-bottom-nav" not in css


def test_legacy_prelaunch_hotfix_no_longer_restores_turquoise_wallet() -> None:
    css = (ROOT / "app/static/css/prelaunch-ui-hotfix.css").read_text(encoding="utf-8")

    assert "--app-accent:#0b88b2!important" in css
    assert "--app-ocean:#095c57!important" in css
    assert "linear-gradient(135deg,#095c57 0%,#075867 48%,#005b7d 100%)!important" in css
    assert "#3ac3b0" not in css
    assert "#2aaa97" not in css
