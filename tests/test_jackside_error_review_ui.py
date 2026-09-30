from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "app" / "static" / "js" / "jackside-error-review.js"


def test_review_reuses_question_visual_language_and_marks_answers() -> None:
    source = ASSET.read_text(encoding="utf-8")

    assert "screen.className = 'quiz-screen jackside-review-shell';" in source
    assert '<div class="quiz-progress"><span data-review-progress-bar></span></div>' in source
    assert 'class="quiz-question-media" data-review-media' in source
    assert 'class="quiz-question-title" data-review-title' in source
    assert 'class="quiz-options jackside-review-options" data-review-options' in source
    assert "holder.className = 'quiz-option jackside-review-option';" in source
    assert "if (option.correct) holder.classList.add('is-correct');" in source
    assert "else if (option.selected) holder.classList.add('is-wrong');" in source
    assert "note.textContent = option.correct ? 'Правильный ответ' : 'Ваш ответ';" in source


def test_review_has_separate_explanation_panel_and_centered_offer_button() -> None:
    source = ASSET.read_text(encoding="utf-8")

    assert "Комментарий к правильному ответу" in source
    assert "data-review-explanation" in source
    assert '.jackside-review-offer [data-error-review-open]' in source
    assert 'margin: 18px auto 0;' in source
    assert 'width: min(100%, 360px);' in source


def test_review_does_not_render_correct_questions_as_review_entries() -> None:
    source = ASSET.read_text(encoding="utf-8")

    # The member UI only renders questions returned by the error-review API.
    # The API contract is covered by tests/test_jackside_error_review.py and
    # returns only incorrectly answered questions. Keep the UI free of any
    # fallback that would add ordinary/correct questions to the review list.
    assert "reviewState?.questions || []" in source
    assert "state.questions" not in source
