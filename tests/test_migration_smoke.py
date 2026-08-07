"""Smoke coverage for idempotent SQLite migrations and WAL mode."""

from __future__ import annotations

from pathlib import Path

from app.db import connect, init_db


def test_init_db_is_idempotent_and_enables_wal(tmp_path: Path) -> None:
    db_path = tmp_path / "migrate.sqlite3"
    init_db(db_path)
    init_db(db_path)

    with connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "jackside_issues" in tables
        assert "jackside_rules_versions" in tables
        assert "jackside_issue_participants" in tables
        assert "jackcoin_ledger" in tables
        assert "daily_414_final_tables" in tables
        assert "daily_414_finalists" in tables
        assert "quiz_campaigns" in tables
        assert "jackside_analytics_cache" in tables
        assert "jackside_analytics_state" in tables

        submission_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(quiz_submissions)")
        }
        assert "timed_out" in submission_columns

        analytics_triggers = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='trigger' AND name LIKE '%_analytics_%'"
            ).fetchall()
        }
        assert "trg_quiz_submissions_analytics_insert" in analytics_triggers
        assert "trg_jackcoin_ledger_analytics_update" in analytics_triggers

        before = conn.execute(
            "SELECT source_version FROM jackside_analytics_state WHERE id=1"
        ).fetchone()[0]
        conn.execute("INSERT INTO clients(first_name, source) VALUES ('Smoke', 'test')")
        after = conn.execute(
            "SELECT source_version FROM jackside_analytics_state WHERE id=1"
        ).fetchone()[0]
        assert after == before + 1

        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
