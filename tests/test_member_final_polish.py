from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_member_theme_is_render_blocking_in_stable_order() -> None:
    template = (ROOT / "app/templates/member_base.html").read_text(encoding="utf-8")

    compatibility = template.index("prelaunch-ui-hotfix.css")
    final_polish = template.index("member-final-polish.css")
    theme = template.index("member-theme.css")
    cleanup = template.index("member-ui-cleanup.css")
    hijack = template.index("hijack-member.css")
    refinement = template.index("member-brand-refinement.css")
    assert compatibility < final_polish < theme < cleanup < hijack < refinement
    assert "data-member-theme" in template
    assert "data-prelaunch-ui-hotfix" in template
    assert "data-member-brand-refinement" in template


def test_dynamic_loader_remains_only_as_safe_fallback() -> None:
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


def test_canonical_theme_owns_palette_quiz_geometry_and_privacy_entry() -> None:
    css = (ROOT / "app/static/css/member-theme.css").read_text(encoding="utf-8")

    assert "--hj-blue: #005b7d" in css
    assert "--hj-sea: #095c57" in css
    assert "--hj-red: #d52b42" in css
    assert "--hj-graphite: #111214" in css
    assert "--hj-quiz-gradient:" in css
    assert ".jc-wallet-card" in css
    assert '[data-account-tab="home"] .quiz-feature-card' in css
    assert "height: auto !important" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr)) !important" in css
    assert ".campaign-card.upcoming" in css
    assert ".profile-visibility-entry" in css
    assert ".member-bottom-nav" not in css


def test_brand_refinement_is_detail_only_and_does_not_own_core_surfaces() -> None:
    css = (ROOT / "app/static/css/member-brand-refinement.css").read_text(encoding="utf-8")

    assert ".referral-tree-root" in css
    assert ".profile-achievement-stage" in css
    assert ".jc-wallet-card" not in css
    assert ".quiz-feature-card" not in css
    assert ".campaign-card" not in css
    assert ".member-bottom-nav" not in css


def test_legacy_prelaunch_hotfix_no_longer_restores_turquoise_wallet() -> None:
    css = (ROOT / "app/static/css/prelaunch-ui-hotfix.css").read_text(encoding="utf-8")

    assert "--app-accent:#0b88b2!important" in css
    assert "--app-ocean:#095c57!important" in css
    assert "linear-gradient(135deg,#095c57 0%,#075867 48%,#005b7d 100%)!important" in css
    assert "#3ac3b0" not in css
    assert "#2aaa97" not in css
    assert "linear-gradient(145deg,#121719,#0d0f10)!important" not in css
    assert ".quiz-feature-card{background:" not in css
