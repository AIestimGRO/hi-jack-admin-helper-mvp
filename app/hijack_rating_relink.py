from __future__ import annotations

import sqlite3

from fastapi import FastAPI, Request

from app.db import transaction
from app.product_shell import _current_member
from app.services.hijack_rating import ensure_hijack_rating_schema
from app.services.phone import normalize_phone


def relink_hijack_history(conn: sqlite3.Connection, *, client_id: int) -> int:
    """Attach previously imported unlinked rating rows to a newly known client."""
    ensure_hijack_rating_schema(conn)
    client = conn.execute(
        "SELECT phone_local,phone_full,phone_raw FROM clients WHERE id=?",
        (client_id,),
    ).fetchone()
    if not client:
        return 0

    phone_local = normalize_phone(
        client["phone_local"] or client["phone_full"] or client["phone_raw"]
    )
    if not phone_local:
        return 0

    import_ids = [
        int(row[0])
        for row in conn.execute(
            """
            SELECT DISTINCT import_id
            FROM hi_jack_rating_entries
            WHERE client_id IS NULL AND phone_local=?
            """,
            (phone_local,),
        ).fetchall()
    ]
    if not import_ids:
        return 0

    cursor = conn.execute(
        """
        UPDATE hi_jack_rating_entries
        SET client_id=?
        WHERE client_id IS NULL AND phone_local=?
        """,
        (client_id, phone_local),
    )

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
    return int(cursor.rowcount or 0)


def install_hijack_rating_relink(app: FastAPI) -> FastAPI:
    if getattr(app.state, "hijack_rating_relink_installed", False):
        return app
    app.state.hijack_rating_relink_installed = True

    @app.middleware("http")
    async def hijack_rating_relink_middleware(request: Request, call_next):
        should_relink = request.method == "GET" and (
            request.url.path == "/account"
            or request.url.path == "/api/account/hijack-rating"
        )
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
                # A historical rating relink must never block account access.
                pass
        return await call_next(request)

    return app


__all__ = ["install_hijack_rating_relink", "relink_hijack_history"]
