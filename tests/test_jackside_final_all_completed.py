from datetime import datetime, timezone
from pathlib import Path

from app.db import init_db, transaction
from app.jackside_runtime_compat import ensure_default_rules_compat
from app.services import daily_414_final as final_service
from app.services.jackside_copy import DEFAULT_RULES_CONTENT, DEFAULT_RULES_VERSION


ROOT = Path(__file__).resolve().parents[1]
LEGACY_BUILTIN_MARKER = (
    "в финал проходят до 10 лучших по правильным ответам, затем по зачётному времени;"
)


def _seed_submission(
    conn,
    *,
    campaign_code: str,
    number: int,
    correct_count: int,
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
            ) VALUES (?, 1, ?, ?, ?, '{}', ?, 10, ?, ?, ?, ?)
            """,
            (
                campaign_code,
                client_id,
                str(number),
                str(number),
                correct_count,
                10_000 + number,
                main_prize_eligible,
                main_round_completed,
                f"ip-{number}",
            ),
        ).lastrowid
    )


def test_jackside_final_includes_every_completed_player_without_score_or_speed_cutoff(
    tmp_path,
) -> None:
    db_path = tmp_path / "jackside-all-completed.sqlite3"
    init_db(db_path)
    campaign = "jackside_20260821_all"
    start = datetime(2026, 8, 21, 15, 19, 14, tzinfo=timezone.utc)

    with transaction(db_path) as conn:
        eligible_ids = [
            _seed_submission(
                conn,
                campaign_code=campaign,
                number=number,
                correct_count=(number - 1) % 11,
            )
            for number in range(1, 14)
        ]
        incomplete_id = _seed_submission(
            conn,
            campaign_code=campaign,
            number=90,
            correct_count=10,
            main_round_completed=0,
        )
        outside_deadline_id = _seed_submission(
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
    assert finalist_ids == eligible_ids
    assert incomplete_id not in finalist_ids
    assert outside_deadline_id not in finalist_ids


def test_builtin_rules_migrate_from_11_to_13(tmp_path) -> None:
    db_path = tmp_path / "jackside-rules-13.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        conn.execute("UPDATE jackside_rules_versions SET is_active=0")
        conn.execute(
            """
            INSERT INTO jackside_rules_versions(version, title, content, is_active)
            VALUES ('1.1', 'Правила JACKSIDE 4:14', ?, 1)
            """,
            (f"Built-in rules. {LEGACY_BUILTIN_MARKER}",),
        )
        migrated = ensure_default_rules_compat(conn)
        active = conn.execute(
            """
            SELECT version, content
            FROM jackside_rules_versions
            WHERE is_active=1
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()

    assert DEFAULT_RULES_VERSION == "1.3"
    assert migrated["version"] == "1.3"
    assert active["version"] == "1.3"
    assert "проходят все участники" in active["content"]
    assert "скорость прохождения основной части на допуск в финал не влияют" in active["content"]
    assert "ответили на все вопросы основной части" in active["content"]
    assert active["content"] == DEFAULT_RULES_CONTENT


def test_builtin_flexible_rules_12_migrate_to_13(tmp_path) -> None:
    db_path = tmp_path / "jackside-rules-12-to-13.sqlite3"
    init_db(db_path)
    old_content = (
        "JACKSIDE built-in.\n"
        "• количество вопросов основной части задаёт мастер для конкретного выпуска;\n"
        "• один общий таймер 4 минуты 14 секунд начинается для всего клуба одновременно;\n"
        f"• {LEGACY_BUILTIN_MARKER}\n"
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

    assert migrated["version"] == "1.3"
    assert migrated["content"] == DEFAULT_RULES_CONTENT


def test_custom_rules_version_11_is_not_auto_migrated(tmp_path) -> None:
    db_path = tmp_path / "jackside-custom-rules-11.sqlite3"
    init_db(db_path)
    custom_content = "Обновлённые правила JACKSIDE. " + ("подробности " * 10)
    with transaction(db_path) as conn:
        conn.execute("UPDATE jackside_rules_versions SET is_active=0")
        conn.execute(
            """
            INSERT INTO jackside_rules_versions(version, title, content, is_active)
            VALUES ('1.1', 'Правила JACKSIDE 4:14', ?, 1)
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

    assert current["version"] == "1.1"
    assert active["version"] == "1.1"
    assert active["content"] == custom_content


def test_jackside_policy_is_not_reimplemented_in_runtime_compat() -> None:
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
    assert LEGACY_BUILTIN_MARKER not in multi_source
    assert 'if campaign_code.startswith("jackside_"):' in final_source
    assert "ORDER BY created_at ASC, id ASC" in final_source


def test_jackside_ui_hides_late_entry_warning_before_real_start() -> None:
    source = (ROOT / "app/static/js/jackside-critical-hotfix.js").read_text(
        encoding="utf-8"
    )
    assert "box.style.setProperty('display', 'none', 'important')" in source
    assert "welcome-final-rule" in source
    assert "Финалистов: " in source
    assert "final-lobby-place')?.remove()" in source
    assert "final-lobby-cutoff')?.remove()" in source
