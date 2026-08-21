from datetime import datetime, timezone
from pathlib import Path

from app.db import init_db, transaction
from app.jackside_runtime_compat import ensure_default_rules_compat
from app.services import daily_414_final as final_service
from app.services.jackside_copy import (
    DEFAULT_RULES_CONTENT,
    DEFAULT_RULES_VERSION,
    result_copy_for_score,
)


ROOT = Path(__file__).resolve().parents[1]
LEGACY_TOP10_MARKER = (
    "в финал проходят до 10 лучших по правильным ответам, затем по зачётному времени;"
)
LEGACY_13_MARKER = (
    "количество правильных ответов и скорость прохождения основной части "
    "на допуск в финал не влияют;"
)


def _seed_submission(
    conn,
    *,
    campaign_code: str,
    number: int,
    correct_count: int,
    max_correct_count: int = 10,
    main_prize_eligible: int = 1,
    main_round_completed: int = 1,
) -> int:
    client_id = int(
        conn.execute(
            "INSERT INTO clients(first_name, source) VALUES (?, 'test')",
            (f"Player {number}",),
        ).lastrowid
    )
    return int(
        conn.execute(
            """
            INSERT INTO quiz_submissions(
                campaign_code, campaign_version, client_id, phone_raw,
                phone_local, answers_json, correct_count, max_correct_count,
                completion_time_ms, main_prize_eligible,
                main_round_completed, ip_hash
            ) VALUES (?, 1, ?, ?, ?, '{}', ?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_code,
                client_id,
                str(number),
                str(number),
                correct_count,
                max_correct_count,
                50_000 - number * 100,
                main_prize_eligible,
                main_round_completed,
                f"ip-{number}",
            ),
        ).lastrowid
    )


def test_jackside_final_includes_all_perfect_players_without_top10_or_speed_cutoff(
    tmp_path,
) -> None:
    db_path = tmp_path / "jackside-perfect-final.sqlite3"
    init_db(db_path)
    campaign = "jackside_20260821_perfect"
    start = datetime(2026, 8, 21, 15, 19, 14, tzinfo=timezone.utc)

    with transaction(db_path) as conn:
        perfect_ids = [
            _seed_submission(
                conn,
                campaign_code=campaign,
                number=number,
                correct_count=10,
            )
            for number in range(1, 14)
        ]
        eight_of_ten_id = _seed_submission(
            conn,
            campaign_code=campaign,
            number=80,
            correct_count=8,
        )
        incomplete_perfect_id = _seed_submission(
            conn,
            campaign_code=campaign,
            number=90,
            correct_count=10,
            main_round_completed=0,
        )
        late_perfect_id = _seed_submission(
            conn,
            campaign_code=campaign,
            number=91,
            correct_count=10,
            main_prize_eligible=0,
        )
        table = final_service.ensure_final_table(
            conn,
            campaign_code=campaign,
            campaign_version=1,
            starts_at=start,
            questions=[{"id": "f1"}],
        )
        live = final_service.reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start,
        )
        finalists = conn.execute(
            """
            SELECT submission_id, seed
            FROM daily_414_finalists
            WHERE final_table_id=?
            ORDER BY seed
            """,
            (table["id"],),
        ).fetchall()

    finalist_ids = [int(row["submission_id"]) for row in finalists]
    assert live["status"] == "live"
    assert len(finalists) == 13
    assert finalist_ids == perfect_ids
    assert eight_of_ten_id not in finalist_ids
    assert incomplete_perfect_id not in finalist_ids
    assert late_perfect_id not in finalist_ids


def test_result_copy_requires_perfect_score_for_final() -> None:
    imperfect = result_copy_for_score(
        8,
        final_eligible=True,
        max_correct_count=10,
    )
    perfect = result_copy_for_score(
        10,
        final_eligible=True,
        max_correct_count=10,
    )

    assert "правильно ответили на все вопросы" in imperfect["message"]
    assert "вы в финальном столе" not in imperfect["message"].lower()
    assert "вы в финальном столе" in perfect["message"].lower()


def _assert_current_rules(rules) -> None:
    content = str(rules["content"] or "")
    assert str(rules["version"]) == "1.4"
    assert "правильно ответили на все вопросы основной части" in content
    assert LEGACY_13_MARKER not in content
    assert "зачётное время считается от общего старта выпуска" not in content
    assert content == DEFAULT_RULES_CONTENT


def test_builtin_rules_migrate_from_11_to_14(tmp_path) -> None:
    db_path = tmp_path / "jackside-rules-11-to-14.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        conn.execute("UPDATE jackside_rules_versions SET is_active=0")
        conn.execute(
            """
            INSERT INTO jackside_rules_versions(version, title, content, is_active)
            VALUES ('1.1', 'Правила JACKSIDE 4:14', ?, 1)
            """,
            (f"Built-in rules. {LEGACY_TOP10_MARKER}",),
        )
        migrated = ensure_default_rules_compat(conn)

    assert DEFAULT_RULES_VERSION == "1.4"
    _assert_current_rules(migrated)


def test_builtin_rules_migrate_from_12_to_14(tmp_path) -> None:
    db_path = tmp_path / "jackside-rules-12-to-14.sqlite3"
    init_db(db_path)
    old_content = (
        "JACKSIDE built-in.\n"
        "• количество вопросов основной части задаёт мастер для конкретного выпуска;\n"
        f"• {LEGACY_TOP10_MARKER}\n"
    )
    with transaction(db_path) as conn:
        conn.execute("UPDATE jackside_rules_versions SET is_active=0")
        conn.execute(
            """
            INSERT INTO jackside_rules_versions(version, title, content, is_active)
            VALUES ('1.2', 'Правила JACKSIDE 4:14', ?, 1)
            """,
            (old_content,),
        )
        migrated = ensure_default_rules_compat(conn)

    _assert_current_rules(migrated)


def test_builtin_rules_migrate_from_13_to_14(tmp_path) -> None:
    db_path = tmp_path / "jackside-rules-13-to-14.sqlite3"
    init_db(db_path)
    old_content = (
        "JACKSIDE built-in.\n"
        "• в финальный стол проходят все участники, которые ответили на все вопросы "
        "основной части до общего дедлайна 4:14;\n"
        f"• {LEGACY_13_MARKER}\n"
    )
    with transaction(db_path) as conn:
        conn.execute("UPDATE jackside_rules_versions SET is_active=0")
        conn.execute(
            """
            INSERT INTO jackside_rules_versions(version, title, content, is_active)
            VALUES ('1.3', 'Правила JACKSIDE 4:14', ?, 1)
            """,
            (old_content,),
        )
        migrated = ensure_default_rules_compat(conn)

    _assert_current_rules(migrated)


def test_custom_rules_version_13_is_not_auto_migrated(tmp_path) -> None:
    db_path = tmp_path / "jackside-custom-rules-13.sqlite3"
    init_db(db_path)
    custom_content = "Кастомные правила JACKSIDE. " + ("детали " * 10)
    with transaction(db_path) as conn:
        conn.execute("UPDATE jackside_rules_versions SET is_active=0")
        conn.execute(
            """
            INSERT INTO jackside_rules_versions(version, title, content, is_active)
            VALUES ('1.3', 'Правила JACKSIDE 4:14', ?, 1)
            """,
            (custom_content,),
        )
        current = ensure_default_rules_compat(conn)
        active = conn.execute(
            """
            SELECT version, content
            FROM jackside_rules_versions
            WHERE is_active=1
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()

    assert current["version"] == "1.3"
    assert active["version"] == "1.3"
    assert active["content"] == custom_content


def test_jackside_policy_ownership_stays_canonical() -> None:
    compat_source = (ROOT / "app/jackside_runtime_compat.py").read_text(
        encoding="utf-8"
    )
    multi_source = (ROOT / "app/jackside_multi_runtime.py").read_text(
        encoding="utf-8"
    )
    final_source = (ROOT / "app/services/daily_414_final.py").read_text(
        encoding="utf-8"
    )

    assert "final_service.seed_finalists = seed_finalists_compat" not in compat_source
    assert "copy_service.result_copy_for_score = result_copy_for_score_compat" not in compat_source
    assert "issue_service.ensure_default_rules = ensure_default_rules_compat" not in compat_source
    assert "issue_service.validate_issue_for_publish = validate_issue_for_publish_compat" not in compat_source
    assert "return multi_runtime.ensure_default_rules_flexible(conn)" in compat_source
    assert "return multi_runtime.validate_issue_for_publish_flexible(conn, current)" in compat_source
    assert "FLEX_RULES_VERSION = copy_service.DEFAULT_RULES_VERSION" in multi_source
    assert "FLEX_RULES_CONTENT = copy_service.DEFAULT_RULES_CONTENT" in multi_source
    assert 'if campaign_code.startswith("jackside_"):' in final_source
    assert "correct_count = max_correct_count" in final_source
    assert "ORDER BY created_at ASC, id ASC" in final_source


def test_jackside_ui_uses_perfect_final_copy_and_hides_prestart_urgency() -> None:
    source = (ROOT / "app/static/js/jackside-critical-hotfix.js").read_text(
        encoding="utf-8"
    )
    assert "box.style.setProperty('display', 'none', 'important')" in source
    assert "welcome-final-rule" in source
    assert "Правильно ответили на все вопросы основной части" in source
    assert "Правильность и скорость на допуск не влияют" not in source
    assert "Финалистов: " in source
