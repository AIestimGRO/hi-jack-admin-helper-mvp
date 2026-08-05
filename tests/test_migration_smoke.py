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

        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
