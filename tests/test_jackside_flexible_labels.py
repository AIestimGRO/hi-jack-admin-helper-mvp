from __future__ import annotations

from pathlib import Path

from app.db import connect, init_db, transaction
from app.jackside_flexible_labels import normalize_builtin_perfect_labels


ROOT = Path(__file__).resolve().parents[1]


def test_builtin_ten_of_ten_copy_is_normalized(tmp_path) -> None:
    db_path = tmp_path / "labels.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        changed = normalize_builtin_perfect_labels(conn)
        achievement = conn.execute(
            "SELECT description FROM achievement_definitions WHERE code='perfect_game'"
        ).fetchone()
        title = conn.execute(
            "SELECT description FROM title_definitions WHERE code='nuts_mind'"
        ).fetchone()
        monthly = conn.execute(
            "SELECT description FROM title_definitions WHERE code='monthly_nuts'"
        ).fetchone()
    assert changed == 3
    assert achievement["description"] == "Заверши основную часть без единой ошибки."
    assert title["description"] == "5 идеальных игр без единой ошибки."
    assert monthly["description"] == "Идеальные игры текущего месяца."


def test_admin_edited_copy_is_not_overwritten(tmp_path) -> None:
    db_path = tmp_path / "custom-label.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE title_definitions SET description='Мой текст' WHERE code='nuts_mind'"
        )
        normalize_builtin_perfect_labels(conn)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT description FROM title_definitions WHERE code='nuts_mind'"
        ).fetchone()
    assert row["description"] == "Мой текст"


def test_flexible_labels_are_normalized_inside_jackside_validation() -> None:
    source = (ROOT / "app/jackside_runtime_compat.py").read_text(encoding="utf-8")
    assert "from app.jackside_flexible_labels import normalize_builtin_perfect_labels" in source
    assert "normalize_builtin_perfect_labels(conn)" in source
    assert "install_jackside_flexible_labels" not in (
        ROOT / "app/main.py"
    ).read_text(encoding="utf-8")


def test_member_stats_do_not_present_perfect_games_as_ten_of_ten() -> None:
    source = (ROOT / "app/static/js/member.js").read_text(encoding="utf-8")
    assert '.member-stat-card, .quiz-stat-board article' in source
    assert 'replace(/^10\\/10\\s*:/, "идеальных игр:")' in source
