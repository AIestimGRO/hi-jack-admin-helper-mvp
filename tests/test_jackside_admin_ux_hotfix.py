from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.db import init_db, transaction
from app.jackside_multi_issue import ensure_multi_issue_schema
from app.jackside_multi_runtime import (
    award_daily_jackcoin_flexible,
    create_issue_multi_guarded,
    validate_daily_questions_flexible,
    validate_issue_for_publish_flexible,
)
from app.jackside_runtime_compat import (
    reschedule_future_issue_compat,
    validate_issue_for_publish_compat,
)
from app.services import jackside_copy as copy_service
from app.services import jackside_issues as issue_service


ROOT = Path(__file__).resolve().parents[1]
MOSCOW = ZoneInfo("Europe/Moscow")


def _add_choice_question(
    conn,
    *,
    campaign_code: str,
    code: str,
    title: str,
    game_round: str,
    position: int,
) -> int:
    question_id = int(
        conn.execute(
            """
            INSERT INTO quiz_questions(
                campaign_code, code, type, title, game_round,
                required, points, time_limit_seconds, position, is_active
            ) VALUES (?, ?, 'single_choice', ?, ?, 1, 1, 30, ?, 1)
            """,
            (campaign_code, code, title, game_round, position),
        ).lastrowid
    )
    conn.execute(
        """
        INSERT INTO quiz_options(
            question_id, code, text, is_correct, position
        ) VALUES (?, 'a', 'A', 1, 10), (?, 'b', 'B', 0, 20)
        """,
        (question_id, question_id),
    )
    return question_id


def _seed_publishable_questions(conn, campaign_code: str, main_count: int = 3) -> None:
    for index in range(1, main_count + 1):
        _add_choice_question(
            conn,
            campaign_code=campaign_code,
            code=f"m{index}",
            title=f"Main {index}",
            game_round="main",
            position=index * 10,
        )
    _add_choice_question(
        conn,
        campaign_code=campaign_code,
        code="f1",
        title="Final",
        game_round="final",
        position=1000,
    )


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
    starts = datetime(2026, 8, 20, 18, 14, tzinfo=MOSCOW)
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


def test_same_day_different_start_times_are_allowed(tmp_path) -> None:
    db_path = tmp_path / "same-day-different-time.sqlite3"
    init_db(db_path)
    ensure_multi_issue_schema(db_path)
    with transaction(db_path) as conn:
        first = create_issue_multi_guarded(
            conn,
            issue_date_value=date(2026, 8, 20),
            starts_at=datetime(2026, 8, 20, 18, 14, tzinfo=MOSCOW),
            title="JACKSIDE A",
        )
        second = create_issue_multi_guarded(
            conn,
            issue_date_value=date(2026, 8, 20),
            starts_at=datetime(2026, 8, 20, 21, 0, tzinfo=MOSCOW),
            title="JACKSIDE B",
        )
    assert int(first["id"]) != int(second["id"])


def test_reschedule_cannot_collide_with_another_exact_start(tmp_path) -> None:
    db_path = tmp_path / "reschedule-collision.sqlite3"
    init_db(db_path)
    ensure_multi_issue_schema(db_path)
    occupied = datetime(2026, 8, 21, 21, 0, tzinfo=MOSCOW)
    with transaction(db_path) as conn:
        first = create_issue_multi_guarded(
            conn,
            issue_date_value=date(2026, 8, 21),
            starts_at=datetime(2026, 8, 21, 18, 14, tzinfo=MOSCOW),
            title="First",
        )
        create_issue_multi_guarded(
            conn,
            issue_date_value=date(2026, 8, 21),
            starts_at=occupied,
            title="Second",
        )
        with pytest.raises(ValueError, match="На это время уже существует выпуск JACKSIDE"):
            reschedule_future_issue_compat(
                conn,
                issue_id=int(first["id"]),
                issue_date_value=date(2026, 8, 21),
                starts_at=occupied,
                title="First moved",
                timezone_name="Europe/Moscow",
            )


def test_publish_validation_accepts_three_main_questions(tmp_path) -> None:
    db_path = tmp_path / "flex-publish.sqlite3"
    init_db(db_path)
    ensure_multi_issue_schema(db_path)
    with transaction(db_path) as conn:
        issue = create_issue_multi_guarded(
            conn,
            issue_date_value=date(2026, 8, 20),
            starts_at=datetime(2026, 8, 20, 18, 14, tzinfo=MOSCOW),
            title="Flexible release",
        )
        issue_service.ensure_issue_campaign(conn, issue=issue)
        _seed_publishable_questions(conn, str(issue["campaign_code"]), main_count=3)
        refreshed = issue_service.refresh_issue_question_counts(conn, int(issue["id"]))
        errors = validate_issue_for_publish_flexible(conn, refreshed)
    assert "main_questions_must_be_ten" not in errors
    assert "main_questions_required" not in errors
    assert errors == []


def test_existing_builtin_draft_upgrades_to_flexible_rules_on_validation(tmp_path) -> None:
    db_path = tmp_path / "rules-upgrade.sqlite3"
    init_db(db_path)
    ensure_multi_issue_schema(db_path)
    with transaction(db_path) as conn:
        issue = create_issue_multi_guarded(
            conn,
            issue_date_value=date(2026, 8, 20),
            starts_at=datetime(2026, 8, 20, 18, 14, tzinfo=MOSCOW),
            title="Existing draft",
        )
        issue_service.ensure_issue_campaign(conn, issue=issue)
        _seed_publishable_questions(conn, str(issue["campaign_code"]), main_count=3)
        conn.execute("UPDATE jackside_rules_versions SET is_active=0")
        old_cursor = conn.execute(
            """
            INSERT OR IGNORE INTO jackside_rules_versions(
                version, title, content, is_active
            ) VALUES (?, 'Old built-in rules', ?, 0)
            """,
            (copy_service.DEFAULT_RULES_VERSION, copy_service.DEFAULT_RULES_CONTENT),
        )
        old_rules = conn.execute(
            "SELECT * FROM jackside_rules_versions WHERE version=?",
            (copy_service.DEFAULT_RULES_VERSION,),
        ).fetchone()
        assert old_rules is not None
        conn.execute(
            """
            UPDATE jackside_issues
            SET rules_version_id=?, rules_version=?
            WHERE id=?
            """,
            (int(old_rules["id"]), str(old_rules["version"]), int(issue["id"])),
        )
        conn.execute(
            "UPDATE jackside_rules_versions SET is_active=1 WHERE id=?",
            (int(old_rules["id"]),),
        )
        legacy_draft = issue_service.refresh_issue_question_counts(conn, int(issue["id"]))
        errors = validate_issue_for_publish_compat(conn, legacy_draft)
        upgraded = issue_service.get_issue(conn, int(issue["id"]))
        assert old_cursor is not None
    assert errors == []
    assert upgraded is not None
    assert str(upgraded["rules_version"]) == copy_service.DEFAULT_RULES_VERSION


def test_admin_release_form_is_not_captured_by_generic_ajax() -> None:
    script = (ROOT / "app/static/js/prelaunch-admin.js").read_text(encoding="utf-8")
    assert "form.campaign-create, [data-release-form], [data-edit-release-form]" in script
    assert "form.dataset.sameDayGuard = 'true'" in script
    assert "form.dataset.releaseSubmitting" in script
    assert "sameDayConfirmed ? '1' : '0'" in script


def test_admin_quiz_information_architecture_is_explicit() -> None:
    script = (ROOT / "app/static/js/admin-quiz-ux.js").read_text(encoding="utf-8")
    assert "Обычные квизы" in script
    assert "Опубликовать и запланировать" in script
    assert "Отменить выпуск" in script
    assert "JACKSIDE 4:14 управляется отдельно" in script


def test_player_welcome_uses_release_question_count() -> None:
    template = (ROOT / "app/templates/quiz.html").read_text(encoding="utf-8")
    assert 'data-role="main-question-count"' in template
    assert "10 вопросов · 4 минуты 14 секунд" not in template
