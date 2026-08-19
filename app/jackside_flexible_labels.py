from __future__ import annotations

import sqlite3

from fastapi import FastAPI

from app.db import transaction


BUILTIN_DESCRIPTION_UPDATES = (
    (
        "achievement_definitions",
        "perfect_game",
        "Заверши игру со счётом 10/10.",
        "Заверши основную часть без единой ошибки.",
    ),
    (
        "title_definitions",
        "nuts_mind",
        "5 результатов 10/10.",
        "5 идеальных игр без единой ошибки.",
    ),
    (
        "title_definitions",
        "monthly_nuts",
        "Результаты 10/10 текущего месяца.",
        "Идеальные игры текущего месяца.",
    ),
)


def normalize_builtin_perfect_labels(conn: sqlite3.Connection) -> int:
    """Update only untouched built-in copy; never overwrite an admin edit."""
    changed = 0
    for table, code, old_description, new_description in BUILTIN_DESCRIPTION_UPDATES:
        cursor = conn.execute(
            f"""
            UPDATE {table}
            SET description=?
            WHERE code=? AND description=?
            """,
            (new_description, code, old_description),
        )
        changed += max(0, int(cursor.rowcount or 0))
    return changed


def install_jackside_flexible_labels(app: FastAPI) -> FastAPI:
    if getattr(app.state, "jackside_flexible_labels_installed", False):
        return app
    app.state.jackside_flexible_labels_installed = True
    with transaction(app.state.settings.db_path) as conn:
        normalize_builtin_perfect_labels(conn)
    return app


__all__ = [
    "BUILTIN_DESCRIPTION_UPDATES",
    "install_jackside_flexible_labels",
    "normalize_builtin_perfect_labels",
]
