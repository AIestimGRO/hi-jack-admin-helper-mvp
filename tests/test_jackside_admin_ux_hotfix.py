from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from app.db import init_db, transaction
from app.jackside_multi_issue import ensure_multi_issue_schema
from app.jackside_multi_runtime import (
    award_daily_jackcoin_flexible,
    create_issue_multi_guarded,
    validate_daily_questions_flexible,
    validate_issue_for_publish_flexible,
)
from app.services import jackside_issues as issue_service


ROOT = Path(__file__).resolve().parents[1]


def test_daily_414_accepts_any_positive_main_question_count() -> None:
    questions = [
        {"campaign": "jackside_any", "id": "q1"},
        {"campaign": "jackside_any", "id": "q2"},
        {"campaign": "jackside_any", "id": "q3"},
    ]
    validate_daily_questions_flexible(questions, "jackside_any")
    with pytest.raises(ValueError, match="daily_414_requires_questions"):
        validate_daily_questions_flexible([], "jackside_any")


def test_perfect_bonus_means_all_questions_not_exactly_ten(tmp_path) -> None:
    db_path = tmp_path / "flex-perfect.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        client_id = int(
            conn.execute(
                "INSERT INTO clients(first_name, source) VALUES ('Flex', 'test')"
            ).lastrowid
        )
        result = award_daily_jackcoin_flexible(
            conn,
            client_id=client_id,
            submission_id=9001,
            issue_day=date(2026, 8, 20),
            correct_count=4,
            max_correct_count=4,
            jackcoin_per_correct=5,
            jackcoin_completion_bonus=10,
            jackcoin_perfect_bonus=20,
        )
    assert result["answers"] == 20
    assert result["completion"] == 10
    assert result["perfect"] == 20
    assert result["total"] >= 50


def test_exact_duplicate_release_start_is_rejected(tmp_path) -> None:
    db_path = tmp_path / "duplicate-release.sqlite3"
    init_db(db_path)
    ensure_multi_issue_schema(db_path)
    starts = datetime(2026, 8, 20, 18, 14)
    with transaction(db_path) as conn:
        first = create_issue_multi_guarded(
            conn,
            issue_date_value=date(2026, 8, 20),
            starts_at=starts,
            title="JACKSIDE A",
        )
        assert first["id"]
        with pytest.raises(ValueError, match="На это время уже существует выпуск JACKSIDE"):
            create_issue_multi_guarded(
                conn,
                issue_date_value=date(2026, 8, 20),
                starts_at=starts,
                title="JACKSIDE B",
            )


def test_publish_validation_accepts_three_main_questions(tmp_path) -> None:
    db_path = tmp_path / "flex-publish.sqlite3"
    init_db(db_path)
    ensure_multi_issue_schema(db_path)
    with transaction(db_path) as conn:
        issue = create_issue_multi_guarded(
            conn,
            issue_date_value=date(2026, 8, 20),
            starts_at=datetime(2026, 8, 20, 18, 14),
            title="Flexible release",
        )
        issue_service.ensure_issue_campaign(conn, issue=issue)
        for index in range(1, 4):
            question_id = int(
                conn.execute(
                    """
                    INSERT INTO quiz_questions(
                        campaign_code, code, type, title, game_round,
                        required, points, position, is_active
                    ) VALUES (?, ?, 'single_choice', ?, 'main', 1, 1, ?, 1)
                    """,
                    (
                        issue["campaign_code"],
                        f"m{index}",
                        f"Main {index}",
                        index * 10,
                    ),
                ).lastrowid
            )
            conn.execute(
                "INSERT INTO quiz_options(question_id, code, text, is_correct, position) VALUES (?, 'a', 'A', 1, 10)",
                (question_id,),
            )
        final_id = int(
            conn.execute(
                """
                INSERT INTO quiz_questions(
                    campaign_code, code, type, title, game_round,
                    required, points, time_limit_seconds, position, is_active
                ) VALUES (?, 'f1', 'single_choice', 'Final', 'final', 1, 1, 30, 100, 1)
                """,
                (issue["campaign_code"],),
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO quiz_options(question_id, code, text, is_correct, position) VALUES (?, 'a', 'A', 1, 10)",
            (final_id,),
        )
        refreshed = issue_service.refresh_issue_question_counts(conn, int(issue["id"]))
        errors = validate_issue_for_publish_flexible(conn, refreshed)
    assert "main_questions_must_be_ten" not in errors
    assert "main_questions_required" not in errors
    assert errors == []


def test_admin_release_form_is_not_captured_by_generic_ajax() -> None:
    script = (ROOT / "app/static/js/prelaunch-admin.js").read_text(encoding="utf-8")
    assert "[data-release-form], [data-edit-release-form]" in script
    assert "form.dataset.sameDayGuard = 'true'" in script
    assert "form.dataset.releaseSubmitting" in script


def test_admin_quiz_information_architecture_is_explicit() -> None:
    script = (ROOT / "app/static/js/admin-quiz-ux.js").read_text(encoding="utf-8")
    assert "Обычные квизы" in script
    assert "Опубликовать и запланировать" in script
    assert "JACKSIDE 4:14 управляется отдельно" in script


def test_player_welcome_uses_release_question_count() -> None:
    template = (ROOT / "app/templates/quiz.html").read_text(encoding="utf-8")
    assert 'data-role="main-question-count"' in template
    assert "10 вопросов · 4 минуты 14 секунд" not in template
