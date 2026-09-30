from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "app" / "static" / "js" / "jackside-error-review.js"
STYLE = ROOT / "app" / "static" / "css" / "jackside-final-recovery.css"


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


def test_review_visual_states_are_enforced_by_external_jackside_css() -> None:
    css = STYLE.read_text(encoding="utf-8")

    # Mobile browsers may reject style tags created at runtime under CSP, so
    # visible answer states must live in the already-loaded JACKSIDE CSS file.
    assert '.jackside-review-option.is-wrong' in css
    assert '.jackside-review-option.is-correct' in css
    assert 'border-color: #ff6577 !important;' in css
    assert 'background: linear-gradient(135deg, rgba(173,31,50,.78)' in css
    assert 'border-color: #55dfa8 !important;' in css
    assert 'background: linear-gradient(135deg, rgba(20,139,94,.72)' in css

    # The red/green cards themselves communicate meaning; the old inline text
    # labels must not be visible next to the answer text.
    assert '.jackside-review-option small {' in css
    assert 'display: none !important;' in css


def test_review_has_separate_explanation_panel_and_centered_offer_button() -> None:
    source = ASSET.read_text(encoding="utf-8")
    css = STYLE.read_text(encoding="utf-8")

    assert "Комментарий к правильному ответу" in source
    assert "data-review-explanation" in source
    assert '.jackside-review-explanation {' in css
    assert 'border-radius: 16px !important;' in css
    assert '.jackside-review-offer [data-error-review-open] {' in css
    assert 'margin: 18px auto 0 !important;' in css
    assert 'width: min(100%, 360px) !important;' in css


def test_review_does_not_render_correct_questions_as_review_entries() -> None:
    source = ASSET.read_text(encoding="utf-8")

    # The member UI only renders questions returned by the error-review API.
    # The API contract is covered by tests/test_jackside_error_review.py and
    # returns only incorrectly answered questions. Keep the UI free of any
    # fallback that would add ordinary/correct questions to the review list.
    assert "reviewState?.questions || []" in source
    assert "state.questions" not in source
