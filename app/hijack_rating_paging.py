from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from app.db import connect
from app.hijack_rating_baseline import _global_rows, ensure_baseline_schema
from app.product_shell import _current_member
from app.services import hijack_rating as legacy_rating


DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


def _table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def _legacy_snapshot_rows(conn) -> list[dict[str, Any]]:
    if not _table_exists(conn, "club_rating_snapshots") or not _table_exists(
        conn, "club_rating_entries"
    ):
        return []
    snapshot = conn.execute(
        """
        SELECT id FROM club_rating_snapshots
        ORDER BY snapshot_date DESC,id DESC
        LIMIT 1
        """
    ).fetchone()
    if not snapshot:
        return []
    rows = conn.execute(
        """
        SELECT e.client_id,e.display_name,e.points,e.place
        FROM club_rating_entries e
        WHERE e.snapshot_id=?
        ORDER BY CASE WHEN e.place IS NULL THEN 1 ELSE 0 END,e.place,e.id
        """,
        (int(snapshot["id"]),),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        points = float(item.get("points") or 0)
        item["points"] = int(points) if points.is_integer() else round(points, 1)
        item["kills"] = 0
        item["tournaments"] = 0
        item["display_name"] = str(item.get("display_name") or "").strip() or "Игрок"
        actual_place = item.get("place")
        if actual_place is not None:
            actual_place = int(actual_place)
        item["actual_place"] = actual_place
        if actual_place is not None and actual_place > 30:
            item["place"] = "•••"
        result.append(item)
    return result


def _member_row(rows: list[dict[str, Any]], client_id: int) -> dict[str, Any] | None:
    match = next(
        (
            row
            for row in rows
            if row.get("client_id") is not None
            and int(row["client_id"]) == int(client_id)
        ),
        None,
    )
    if match is None:
        return None
    result = dict(match)
    if result.get("actual_place") is not None:
        result["place"] = int(result["actual_place"])
    return result


def hijack_rating_page_payload(
    conn,
    *,
    client_id: int,
    period: str,
    offset: int = 0,
    limit: int = DEFAULT_PAGE_SIZE,
    today: date | None = None,
) -> dict[str, Any]:
    ensure_baseline_schema(conn)
    today = today or date.today()
    period = period if period in {"global", "month", "latest"} else "global"
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), MAX_PAGE_SIZE))

    latest = conn.execute(
        "SELECT * FROM hi_jack_rating_imports ORDER BY created_at DESC,id DESC LIMIT 1"
    ).fetchone()

    if period == "global":
        rows = _global_rows(conn)
        if not rows:
            rows = _legacy_snapshot_rows(conn)
        label = "весь период"
        tournament_name = ""
        tournament_date = ""
    elif period == "month":
        month_start = today.replace(day=1)
        if today.month == 12:
            month_end = date(today.year + 1, 1, 1)
        else:
            month_end = date(today.year, today.month + 1, 1)
        rows = legacy_rating._period_aggregate(
            conn,
            start_date=month_start,
            end_date=month_end,
        )
        label = month_start.strftime("%m.%Y")
        tournament_name = ""
        tournament_date = ""
    else:
        rows = (
            legacy_rating._period_aggregate(conn, import_id=int(latest["id"]))
            if latest
            else []
        )
        label = ""
        tournament_name = str(latest["tournament_name"]) if latest else ""
        tournament_date = str(latest["tournament_date"]) if latest else ""

    total = len(rows)
    page_rows = rows[offset : offset + limit]
    baseline = conn.execute(
        "SELECT total_rows FROM hi_jack_rating_baseline WHERE id=1"
    ).fetchone()
    has_data = bool(
        total
        or latest
        or (baseline and int(baseline["total_rows"] or 0) > 0)
    )

    return {
        "has_data": has_data,
        "period": period,
        "label": label,
        "tournament_name": tournament_name,
        "tournament_date": tournament_date,
        "rows": page_rows,
        "me": _member_row(rows, client_id),
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(page_rows) < total,
    }


def install_hijack_rating_paging(app: FastAPI) -> FastAPI:
    if getattr(app.state, "hijack_rating_paging_installed", False):
        return app
    app.state.hijack_rating_paging_installed = True
    settings = app.state.settings

    @app.get("/api/account/hijack-rating-page")
    async def account_hijack_rating_page(
        request: Request,
        period: str = Query("global"),
        offset: int = Query(0, ge=0),
        limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    ):
        member = _current_member(request, required=True)
        with connect(settings.db_path) as conn:
            payload = hijack_rating_page_payload(
                conn,
                client_id=int(member["client_id"]),
                period=period,
                offset=offset,
                limit=limit,
            )
        return JSONResponse(payload)

    return app


__all__ = ["hijack_rating_page_payload", "install_hijack_rating_paging"]
