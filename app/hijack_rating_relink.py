from __future__ import annotations

import sqlite3

from fastapi import FastAPI, Request

from app.db import transaction
from app.product_shell import _current_member
from app.services.client_phone_aliases import phones_for_client
from app.services.hijack_rating import ensure_hijack_rating_schema


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def _recount_baseline(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "hi_jack_rating_baseline"):
        return
    counts = conn.execute(
        """
        SELECT COUNT(*) AS total_rows,
               SUM(CASE WHEN client_id IS NOT NULL THEN 1 ELSE 0 END) AS matched_rows,
               SUM(CASE WHEN client_id IS NULL AND phone_local IS NOT NULL THEN 1 ELSE 0 END) AS unmatched_rows,
               SUM(CASE WHEN phone_local IS NULL THEN 1 ELSE 0 END) AS invalid_rows
        FROM hi_jack_rating_baseline_entries
        """
    ).fetchone()
    conn.execute(
        """
        UPDATE hi_jack_rating_baseline
        SET total_rows=?,matched_rows=?,unmatched_rows=?,invalid_rows=?,updated_at=CURRENT_TIMESTAMP
        WHERE id=1
        """,
        (
            int(counts["total_rows"] or 0),
            int(counts["matched_rows"] or 0),
            int(counts["unmatched_rows"] or 0),
            int(counts["invalid_rows"] or 0),
        ),
    )


def relink_hijack_history(conn: sqlite3.Connection, *, client_id: int) -> int:
    """Attach imported rating rows using the current phone and historical phone aliases."""
    ensure_hijack_rating_schema(conn)
    phone_values = phones_for_client(conn, client_id)
    if not phone_values:
        return 0

    placeholders = ",".join("?" for _ in phone_values)
    import_ids = [
        int(row[0])
        for row in conn.execute(
            f"""
            SELECT DISTINCT import_id
            FROM hi_jack_rating_entries
            WHERE client_id IS NULL AND phone_local IN ({placeholders})
            """,
            phone_values,
        ).fetchall()
    ]
    cursor = conn.execute(
        f"""
        UPDATE hi_jack_rating_entries
        SET client_id=?
        WHERE client_id IS NULL AND phone_local IN ({placeholders})
        """,
        (client_id, *phone_values),
    )
    changed = int(cursor.rowcount or 0)

    for import_id in import_ids:
        counts = conn.execute(
            """
            SELECT
                COUNT(*) AS total_rows,
                SUM(CASE WHEN client_id IS NOT NULL THEN 1 ELSE 0 END) AS matched_rows,
                SUM(CASE WHEN client_id IS NULL AND phone_local IS NOT NULL THEN 1 ELSE 0 END) AS unmatched_rows,
                SUM(CASE WHEN phone_local IS NULL THEN 1 ELSE 0 END) AS invalid_rows
            FROM hi_jack_rating_entries
            WHERE import_id=?
            """,
            (import_id,),
        ).fetchone()
        conn.execute(
            """
            UPDATE hi_jack_rating_imports
            SET total_rows=?,matched_rows=?,unmatched_rows=?,invalid_rows=?
            WHERE id=?
            """,
            (
                int(counts["total_rows"] or 0),
                int(counts["matched_rows"] or 0),
                int(counts["unmatched_rows"] or 0),
                int(counts["invalid_rows"] or 0),
                import_id,
            ),
        )

    if _table_exists(conn, "hi_jack_rating_baseline_entries"):
        baseline_cursor = conn.execute(
            f"""
            UPDATE hi_jack_rating_baseline_entries
            SET client_id=?
            WHERE client_id IS NULL AND phone_local IN ({placeholders})
            """,
            (client_id, *phone_values),
        )
        baseline_changed = int(baseline_cursor.rowcount or 0)
        changed += baseline_changed
        if baseline_changed:
            _recount_baseline(conn)
    return changed


def install_hijack_rating_relink(app: FastAPI) -> FastAPI:
    if getattr(app.state, "hijack_rating_relink_installed", False):
        return app
    app.state.hijack_rating_relink_installed = True

    @app.middleware("http")
    async def hijack_rating_relink_middleware(request: Request, call_next):
        should_relink = request.method == "GET" and request.url.path in {
            "/account",
            "/api/account/hijack-rating",
            "/api/account/hijack-rating-page",
        }
        if should_relink:
            try:
                member = _current_member(request, required=False)
                if member:
                    with transaction(request.app.state.settings.db_path) as conn:
                        relink_hijack_history(
                            conn,
                            client_id=int(member["client_id"]),
                        )
            except Exception:
                # Historical rating relinking must never block account access.
                pass
        return await call_next(request)

    return app


__all__ = ["install_hijack_rating_relink", "relink_hijack_history"]
