from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_final_lobby_uses_stable_finalists_copy_in_canonical_quiz_js() -> None:
    source = (ROOT / "app" / "static" / "js" / "quiz.js").read_text(encoding="utf-8")
    assert "`Финалистов: ${candidates}`" in source
    assert "`В отборе на финал: ${candidates}`" not in source


def test_pregame_superprize_reuses_winner_card_classes() -> None:
    template = (ROOT / "app" / "templates" / "quiz.html").read_text(encoding="utf-8")
    assert 'class="jackside-final-superprize" data-role="daily-extra-prize"' in template
    assert 'class="jackside-final-superprize-kicker">СУПЕРПРИЗ ВЫПУСКА<' in template
    assert 'class="jackside-final-superprize-title"' in template
    assert "daily-extra-prize-kicker" not in template
    assert "daily-extra-prize-title" not in template
