from __future__ import annotations

import sqlite3
from pathlib import Path

from app import jackside_multi_issue as multi_issue


_TARGET_TABLE = "jackside_issues"


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _dependent_schema_objects(
    conn: sqlite3.Connection,
) -> list[tuple[str, str, str]]:
    """Return triggers/views whose SQL directly references jackside_issues.

    A trigger may be attached to a completely different table while querying
    jackside_issues in its WHEN/body. Such a trigger keeps SQLite's schema
    parser from accepting the parent-table rebuild after DROP TABLE unless it
    is temporarily removed. DDL is transactional, so failures restore it.
    """
    rows = conn.execute(
        """
        SELECT type, name, sql
        FROM sqlite_master
        WHERE type IN ('trigger', 'view')
          AND sql IS NOT NULL
        ORDER BY CASE type WHEN 'trigger' THEN 0 ELSE 1 END, rowid
        """
    ).fetchall()
    result: list[tuple[str, str, str]] = []
    for object_type, name, sql in rows:
        statement = str(sql or "")
        if _TARGET_TABLE.casefold() not in statement.casefold():
            continue
        result.append((str(object_type), str(name), statement))
    return result


def _drop_dependent_schema(
    conn: sqlite3.Connection,
    objects: list[tuple[str, str, str]],
) -> None:
    # Triggers first: an INSTEAD OF trigger can itself belong to a view.
    for object_type, name, _sql in objects:
        if object_type == "trigger":
            conn.execute(f"DROP TRIGGER IF EXISTS {_quote_identifier(name)}")
    for object_type, name, _sql in objects:
        if object_type == "view":
            conn.execute(f"DROP VIEW IF EXISTS {_quote_identifier(name)}")


def _restore_dependent_schema(
    conn: sqlite3.Connection,
    objects: list[tuple[str, str, str]],
) -> None:
    # Views must exist before INSTEAD OF triggers that may belong to them.
    for object_type, _name, sql in objects:
        if object_type == "view":
            conn.execute(sql)
    for object_type, _name, sql in objects:
        if object_type == "trigger":
            conn.execute(sql)


def ensure_multi_issue_schema(db_path: str | Path) -> bool:
    """Remove UNIQUE(issue_date) while preserving data and dependent schema."""
    path = Path(db_path)
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        table = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (_TARGET_TABLE,),
        ).fetchone()
        if not table:
            return False
        if not multi_issue._has_unique_issue_date(conn):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_jackside_issues_date_start "
                "ON jackside_issues(issue_date, starts_at, id)"
            )
            conn.commit()
            return False

        saved_indexes = [
            str(row[0])
            for row in conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                (_TARGET_TABLE,),
            ).fetchall()
            if row[0]
        ]
        dependent_schema = _dependent_schema_objects(conn)
        expected_schema_names = {
            (object_type, name) for object_type, name, _sql in dependent_schema
        }
        columns_sql = ", ".join(multi_issue._ISSUE_COLUMNS)

        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            _drop_dependent_schema(conn, dependent_schema)
            conn.execute("DROP TABLE IF EXISTS jackside_issues_multi")
            conn.execute(multi_issue._MULTI_ISSUE_TABLE)
            conn.execute(
                f"INSERT INTO jackside_issues_multi({columns_sql}) "
                f"SELECT {columns_sql} FROM jackside_issues"
            )
            source_count = int(
                conn.execute("SELECT COUNT(*) FROM jackside_issues").fetchone()[0]
            )
            copy_count = int(
                conn.execute("SELECT COUNT(*) FROM jackside_issues_multi").fetchone()[0]
            )
            if source_count != copy_count:
                raise RuntimeError("jackside_issue_migration_count_mismatch")

            conn.execute("DROP TABLE jackside_issues")
            conn.execute("ALTER TABLE jackside_issues_multi RENAME TO jackside_issues")
            for statement in saved_indexes:
                conn.execute(statement)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_jackside_issues_date_start "
                "ON jackside_issues(issue_date, starts_at, id)"
            )
            _restore_dependent_schema(conn, dependent_schema)

            restored_schema_names = {
                (str(row[0]), str(row[1]))
                for row in conn.execute(
                    "SELECT type, name FROM sqlite_master "
                    "WHERE type IN ('trigger', 'view')"
                ).fetchall()
            }
            missing_schema = expected_schema_names - restored_schema_names
            if missing_schema:
                raise RuntimeError(
                    "jackside_issue_migration_schema_missing:"
                    f"{sorted(missing_schema)!r}"
                )

            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    f"jackside_issue_migration_fk_violation:{violations!r}"
                )
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(
                    f"jackside_issue_migration_integrity:{integrity}"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
        return True
    finally:
        conn.close()


def install_schema_override() -> None:
    """Keep callers of app.jackside_multi_issue on the corrected migration."""
    multi_issue.ensure_multi_issue_schema = ensure_multi_issue_schema


install_schema_override()


__all__ = ["ensure_multi_issue_schema", "install_schema_override"]
