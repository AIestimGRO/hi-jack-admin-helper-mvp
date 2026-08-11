from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_final_polish_bundle_is_loaded() -> None:
    loader = (ROOT / "app/static/js/member-profile-refresh.js").read_text(encoding="utf-8")

    assert "member-final-polish.css?v=1" in loader
    assert "member-final-polish.js?v=1" in loader


def test_wallet_and_hijack_rating_final_polish() -> None:
    script = (ROOT / "app/static/js/member-final-polish.js").read_text(encoding="utf-8")
    css = (ROOT / "app/static/css/member-final-polish.css").read_text(encoding="utf-8")

    assert "Играй. Копи. Меняй." in script
    assert ".jc-wallet-balance small" in script
    assert "Твоё место в рейтинге" in script
    assert "hijack-rating-head-compact" in script
    assert ".hijack-rating-caption" in css
    assert "grid-template-columns: repeat(3" in css
