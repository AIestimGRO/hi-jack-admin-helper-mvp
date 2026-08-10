from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse

from app.db import connect, transaction
from app.product_shell import _check_csrf, _current_member, _require_master
from app.services import hijack_rating as legacy_rating
from app.services.phone import normalize_phone

_legacy_metrics = legacy_rating.hijack_member_metrics
_legacy_payload = legacy_rating.hijack_rating_payload


def ensure_baseline_schema(conn: sqlite3.Connection) -> None:
    legacy_rating.ensure_hijack_rating_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hi_jack_rating_baseline (
            id INTEGER PRIMARY KEY CHECK(id=1),
            source_filename TEXT NOT NULL DEFAULT '',
            total_rows INTEGER NOT NULL DEFAULT 0,
            matched_rows INTEGER NOT NULL DEFAULT 0,
            unmatched_rows INTEGER NOT NULL DEFAULT 0,
            invalid_rows INTEGER NOT NULL DEFAULT 0,
            uploaded_by_admin_id INTEGER REFERENCES admins(id) ON DELETE SET NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hi_jack_rating_baseline_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
            phone_local TEXT,
            phone_raw TEXT NOT NULL DEFAULT '',
            rating_points REAL NOT NULL DEFAULT 0,
            kills INTEGER NOT NULL DEFAULT 0,
            source_row INTEGER NOT NULL DEFAULT 0 UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_hi_jack_rating_baseline_phone
        ON hi_jack_rating_baseline_entries(phone_local)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_hi_jack_rating_baseline_client
        ON hi_jack_rating_baseline_entries(client_id)
        """
    )


def _client_id_for_phone(conn: sqlite3.Connection, phone_local: str | None) -> int | None:
    if not phone_local:
        return None
    row = conn.execute(
        "SELECT id FROM clients WHERE phone_local=? LIMIT 1",
        (phone_local,),
    ).fetchone()
    return int(row["id"]) if row else None


def _write_rows(
    conn: sqlite3.Connection,
    *,
    table: str,
    rows: list[dict[str, Any]],
    import_id: int | None = None,
) -> dict[str, int]:
    matched = 0
    unmatched = 0
    invalid = 0
    inserted = 0
    for item in rows:
        phone_local = item.get("phone_local")
        client_id = _client_id_for_phone(conn, phone_local)
        if phone_local and client_id:
            matched += 1
        elif phone_local:
            unmatched += 1
        else:
            invalid += 1
        values = (
            client_id,
            phone_local,
            str(item.get("phone_raw") or "")[:80],
            float(item.get("rating_points") or 0),
            int(item.get("kills") or 0),
            int(item.get("source_row") or 0),
        )
        if table == "baseline":
            conn.execute(
                """
                INSERT INTO hi_jack_rating_baseline_entries(
                    client_id,phone_local,phone_raw,rating_points,kills,source_row
                ) VALUES (?,?,?,?,?,?)
                """,
                values,
            )
        else:
            conn.execute(
                """
                INSERT INTO hi_jack_rating_entries(
                    import_id,client_id,phone_local,phone_raw,rating_points,kills,source_row
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (int(import_id or 0), *values),
            )
        inserted += 1
    return {
        "total_rows": inserted,
        "matched_rows": matched,
        "unmatched_rows": unmatched,
        "invalid_rows": invalid,
    }


def _recount_baseline(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total_rows,
               SUM(CASE WHEN client_id IS NOT NULL THEN 1 ELSE 0 END) AS matched_rows,
               SUM(CASE WHEN client_id IS NULL AND phone_local IS NOT NULL THEN 1 ELSE 0 END) AS unmatched_rows,
               SUM(CASE WHEN phone_local IS NULL THEN 1 ELSE 0 END) AS invalid_rows
        FROM hi_jack_rating_baseline_entries
        """
    ).fetchone()
    counts = {key: int(row[key] or 0) for key in ("total_rows", "matched_rows", "unmatched_rows", "invalid_rows")}
    conn.execute(
        """
        INSERT INTO hi_jack_rating_baseline(id,total_rows,matched_rows,unmatched_rows,invalid_rows)
        VALUES (1,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            total_rows=excluded.total_rows,
            matched_rows=excluded.matched_rows,
            unmatched_rows=excluded.unmatched_rows,
            invalid_rows=excluded.invalid_rows,
            updated_at=CURRENT_TIMESTAMP
        """,
        (counts["total_rows"], counts["matched_rows"], counts["unmatched_rows"], counts["invalid_rows"]),
    )
    return counts


def _recount_import(conn: sqlite3.Connection, import_id: int) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total_rows,
               SUM(CASE WHEN client_id IS NOT NULL THEN 1 ELSE 0 END) AS matched_rows,
               SUM(CASE WHEN client_id IS NULL AND phone_local IS NOT NULL THEN 1 ELSE 0 END) AS unmatched_rows,
               SUM(CASE WHEN phone_local IS NULL THEN 1 ELSE 0 END) AS invalid_rows
        FROM hi_jack_rating_entries WHERE import_id=?
        """,
        (import_id,),
    ).fetchone()
    counts = {key: int(row[key] or 0) for key in ("total_rows", "matched_rows", "unmatched_rows", "invalid_rows")}
    conn.execute(
        """
        UPDATE hi_jack_rating_imports
        SET total_rows=?,matched_rows=?,unmatched_rows=?,invalid_rows=?
        WHERE id=?
        """,
        (counts["total_rows"], counts["matched_rows"], counts["unmatched_rows"], counts["invalid_rows"], import_id),
    )
    return counts


def _global_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH combined AS (
            SELECT client_id,rating_points,kills,NULL AS import_id
            FROM hi_jack_rating_baseline_entries
            WHERE client_id IS NOT NULL
            UNION ALL
            SELECT client_id,rating_points,kills,import_id
            FROM hi_jack_rating_entries
            WHERE client_id IS NOT NULL
        )
        SELECT x.client_id,c.nickname,c.first_name,c.username,
               SUM(x.rating_points) AS points,
               SUM(x.kills) AS kills,
               COUNT(DISTINCT x.import_id) AS tournaments
        FROM combined x
        JOIN clients c ON c.id=x.client_id
        GROUP BY x.client_id,c.nickname,c.first_name,c.username
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key in ("nickname", "first_name", "username"):
            value = str(item.get(key) or "").strip()
            if value:
                item["display_name"] = f"@{value.lstrip('@')}" if key == "username" else value
                break
        else:
            item["display_name"] = f"HJ #{int(item['client_id'])}"
        points = float(item.get("points") or 0)
        item["points"] = int(points) if points.is_integer() else round(points, 1)
        item["kills"] = int(item.get("kills") or 0)
        item["tournaments"] = int(item.get("tournaments") or 0)
        result.append(item)
    result.sort(
        key=lambda item: (
            -float(item["points"]),
            -int(item["kills"]),
            -int(item["tournaments"]),
            int(item["client_id"]),
        )
    )
    previous = None
    place = 0
    for index, item in enumerate(result, start=1):
        key = (float(item["points"]), int(item["kills"]))
        if key != previous:
            place = index
            previous = key
        item["place"] = place
    return result


def hijack_rating_payload_v2(conn: sqlite3.Connection, *, client_id: int) -> dict[str, Any]:
    ensure_baseline_schema(conn)
    payload = _legacy_payload(conn, client_id=client_id)
    global_rows = _global_rows(conn)
    me = next((row for row in global_rows if int(row["client_id"]) == int(client_id)), None)
    baseline = conn.execute("SELECT * FROM hi_jack_rating_baseline WHERE id=1").fetchone()
    payload["has_imports"] = bool(payload.get("has_imports") or (baseline and int(baseline["total_rows"] or 0) > 0))
    payload["year"] = {
        "label": "весь период",
        "rows": global_rows,
        "me": me,
        "is_global": True,
    }
    return payload


def hijack_member_metrics_v2(
    conn: sqlite3.Connection,
    *,
    client_id: int,
    today=None,
) -> dict[str, int | float]:
    ensure_baseline_schema(conn)
    metrics = dict(_legacy_metrics(conn, client_id=client_id, today=today))
    row = conn.execute(
        """
        SELECT COALESCE(SUM(points),0) AS points,COALESCE(SUM(kills),0) AS kills
        FROM (
            SELECT rating_points AS points,kills FROM hi_jack_rating_baseline_entries WHERE client_id=?
            UNION ALL
            SELECT rating_points AS points,kills FROM hi_jack_rating_entries WHERE client_id=?
        )
        """,
        (client_id, client_id),
    ).fetchone()
    points = float(row["points"] or 0)
    metrics["hijack_global_rating"] = int(points) if points.is_integer() else round(points, 1)
    metrics["hijack_global_kills"] = int(row["kills"] or 0)
    return metrics


def _relink_baseline(conn: sqlite3.Connection, *, client_id: int) -> int:
    client = conn.execute(
        "SELECT phone_local,phone_full,phone_raw FROM clients WHERE id=?",
        (client_id,),
    ).fetchone()
    if not client:
        return 0
    phone_local = normalize_phone(client["phone_local"] or client["phone_full"] or client["phone_raw"])
    if not phone_local:
        return 0
    cursor = conn.execute(
        """
        UPDATE hi_jack_rating_baseline_entries
        SET client_id=? WHERE client_id IS NULL AND phone_local=?
        """,
        (client_id, phone_local),
    )
    if cursor.rowcount:
        _recount_baseline(conn)
    return int(cursor.rowcount or 0)


def _redirect(message: str, *, error: bool = False) -> RedirectResponse:
    key = "error" if error else "ok"
    return RedirectResponse(f"/master/hijack-rating?{urlencode({key: message})}", status_code=303)


def _parse_manual_number(value: str, *, integer: bool = False) -> float | int:
    text = str(value or "0").strip().replace(" ", "").replace(",", ".")
    try:
        number = float(text or "0")
    except ValueError as exc:
        raise ValueError("Укажите корректное числовое значение") from exc
    if number < 0:
        raise ValueError("Значение не может быть отрицательным")
    return int(round(number)) if integer else number


def _manual_entry(
    conn: sqlite3.Connection,
    *,
    table: str,
    phone: str,
    rating_points: str,
    kills: str,
    action: str,
    import_id: int | None = None,
) -> None:
    phone_local = normalize_phone(phone)
    if not phone_local:
        raise ValueError("Укажите корректный номер телефона")
    if table == "tournament":
        found = conn.execute("SELECT id FROM hi_jack_rating_imports WHERE id=?", (import_id,)).fetchone()
        if not found:
            raise ValueError("Турнир не найден")
        rows = conn.execute(
            "SELECT id FROM hi_jack_rating_entries WHERE import_id=? AND phone_local=? ORDER BY id",
            (import_id, phone_local),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM hi_jack_rating_baseline_entries WHERE phone_local=? ORDER BY id",
            (phone_local,),
        ).fetchall()
    ids = [int(row["id"]) for row in rows]
    if action == "delete":
        if ids:
            placeholders = ",".join("?" for _ in ids)
            target_table = "hi_jack_rating_entries" if table == "tournament" else "hi_jack_rating_baseline_entries"
            conn.execute(f"DELETE FROM {target_table} WHERE id IN ({placeholders})", ids)
    else:
        points = float(_parse_manual_number(rating_points))
        kill_count = int(_parse_manual_number(kills, integer=True))
        client_id = _client_id_for_phone(conn, phone_local)
        if ids:
            target_table = "hi_jack_rating_entries" if table == "tournament" else "hi_jack_rating_baseline_entries"
            conn.execute(
                f"UPDATE {target_table} SET client_id=?,phone_raw=?,rating_points=?,kills=? WHERE id=?",
                (client_id, str(phone).strip()[:80], points, kill_count, ids[0]),
            )
            if len(ids) > 1:
                placeholders = ",".join("?" for _ in ids[1:])
                conn.execute(f"DELETE FROM {target_table} WHERE id IN ({placeholders})", ids[1:])
        else:
            if table == "tournament":
                source_row = int(conn.execute(
                    "SELECT COALESCE(MAX(source_row),0)+1 FROM hi_jack_rating_entries WHERE import_id=?",
                    (import_id,),
                ).fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO hi_jack_rating_entries(
                        import_id,client_id,phone_local,phone_raw,rating_points,kills,source_row
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (import_id, client_id, phone_local, str(phone).strip()[:80], points, kill_count, source_row),
                )
            else:
                source_row = int(conn.execute(
                    "SELECT COALESCE(MAX(source_row),0)+1 FROM hi_jack_rating_baseline_entries"
                ).fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO hi_jack_rating_baseline_entries(
                        client_id,phone_local,phone_raw,rating_points,kills,source_row
                    ) VALUES (?,?,?,?,?,?)
                    """,
                    (client_id, phone_local, str(phone).strip()[:80], points, kill_count, source_row),
                )
    if table == "tournament":
        _recount_import(conn, int(import_id or 0))
    else:
        _recount_baseline(conn)


def install_hijack_rating_baseline(app: FastAPI) -> FastAPI:
    if getattr(app.state, "hijack_rating_baseline_installed", False):
        return app
    app.state.hijack_rating_baseline_installed = True
    settings = app.state.settings

    legacy_rating.HIJACK_TITLE_CONDITIONS.setdefault(
        "hijack_global_rating", "HI, JACK! · глобальный рейтинг"
    )
    legacy_rating.HIJACK_TITLE_CONDITIONS.setdefault(
        "hijack_global_kills", "HI, JACK! · глобальные киллы"
    )
    legacy_rating.hijack_member_metrics = hijack_member_metrics_v2

    with transaction(settings.db_path) as conn:
        ensure_baseline_schema(conn)

    @app.middleware("http")
    async def baseline_rating_middleware(request: Request, call_next):
        if request.method == "GET" and request.url.path == "/api/account/hijack-rating":
            try:
                member = _current_member(request, required=True)
                with transaction(settings.db_path) as conn:
                    ensure_baseline_schema(conn)
                    _relink_baseline(conn, client_id=int(member["client_id"]))
                    payload = hijack_rating_payload_v2(conn, client_id=int(member["client_id"]))
                return JSONResponse(payload)
            except Exception:
                pass
        if request.method == "GET" and request.url.path == "/account":
            try:
                member = _current_member(request, required=False)
                if member:
                    with transaction(settings.db_path) as conn:
                        ensure_baseline_schema(conn)
                        _relink_baseline(conn, client_id=int(member["client_id"]))
            except Exception:
                pass
        return await call_next(request)

    @app.get("/api/master/hijack-rating/manage")
    async def master_rating_manage(request: Request):
        _require_master(request)
        with connect(settings.db_path) as conn:
            ensure_baseline_schema(conn)
            baseline = conn.execute("SELECT * FROM hi_jack_rating_baseline WHERE id=1").fetchone()
            imports = conn.execute(
                "SELECT * FROM hi_jack_rating_imports ORDER BY created_at DESC,id DESC LIMIT 100"
            ).fetchall()
        return JSONResponse(
            {
                "baseline": dict(baseline) if baseline else None,
                "imports": [dict(row) for row in imports],
            }
        )

    @app.post("/api/master/hijack-rating/baseline")
    async def master_rating_baseline(
        request: Request,
        csrf_token: str = Form(...),
        rating_file: UploadFile = File(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        data = await rating_file.read(legacy_rating.MAX_IMPORT_BYTES + 1)
        try:
            rows = legacy_rating.parse_hijack_rating_workbook(data)
        except ValueError as exc:
            return _redirect(str(exc), error=True)
        with transaction(settings.db_path) as conn:
            ensure_baseline_schema(conn)
            conn.execute("DELETE FROM hi_jack_rating_baseline_entries")
            counts = _write_rows(conn, table="baseline", rows=rows)
            conn.execute(
                """
                INSERT INTO hi_jack_rating_baseline(
                    id,source_filename,total_rows,matched_rows,unmatched_rows,invalid_rows,uploaded_by_admin_id
                ) VALUES (1,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    source_filename=excluded.source_filename,
                    total_rows=excluded.total_rows,
                    matched_rows=excluded.matched_rows,
                    unmatched_rows=excluded.unmatched_rows,
                    invalid_rows=excluded.invalid_rows,
                    uploaded_by_admin_id=excluded.uploaded_by_admin_id,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    str(rating_file.filename or "rating.xlsx")[:250],
                    counts["total_rows"],counts["matched_rows"],counts["unmatched_rows"],counts["invalid_rows"],
                    int(request.session.get("admin_id") or 0) or None,
                ),
            )
        return _redirect(f"Глобальный стартовый рейтинг заменён: {counts['total_rows']} строк")

    @app.post("/api/master/hijack-rating/{import_id:int}/replace")
    async def master_rating_replace(
        request: Request,
        import_id: int,
        csrf_token: str = Form(...),
        tournament_name: str = Form(...),
        tournament_date: str = Form(...),
        rating_file: UploadFile = File(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        from datetime import date
        try:
            day = date.fromisoformat(str(tournament_date).strip())
        except ValueError:
            return _redirect("Укажите корректную дату турнира", error=True)
        name = " ".join(str(tournament_name or "").split())[:160]
        if not name:
            return _redirect("Укажите название турнира", error=True)
        data = await rating_file.read(legacy_rating.MAX_IMPORT_BYTES + 1)
        try:
            rows = legacy_rating.parse_hijack_rating_workbook(data)
        except ValueError as exc:
            return _redirect(str(exc), error=True)
        with transaction(settings.db_path) as conn:
            ensure_baseline_schema(conn)
            found = conn.execute("SELECT id FROM hi_jack_rating_imports WHERE id=?", (import_id,)).fetchone()
            if not found:
                return _redirect("Турнир не найден", error=True)
            conn.execute("DELETE FROM hi_jack_rating_entries WHERE import_id=?", (import_id,))
            counts = _write_rows(conn, table="tournament", rows=rows, import_id=import_id)
            conn.execute(
                """
                UPDATE hi_jack_rating_imports
                SET tournament_name=?,tournament_date=?,source_filename=?,
                    total_rows=?,matched_rows=?,unmatched_rows=?,invalid_rows=?
                WHERE id=?
                """,
                (
                    name,day.isoformat(),str(rating_file.filename or "rating.xlsx")[:250],
                    counts["total_rows"],counts["matched_rows"],counts["unmatched_rows"],counts["invalid_rows"],import_id,
                ),
            )
            legacy_rating._expire_previous_latest_titles(conn)
        return _redirect(f"Турнир «{name}» полностью заменён")

    @app.post("/api/master/hijack-rating/baseline/entry")
    async def master_baseline_entry(
        request: Request,
        csrf_token: str = Form(...),
        phone: str = Form(...),
        rating_points: str = Form("0"),
        kills: str = Form("0"),
        action: str = Form("save"),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                ensure_baseline_schema(conn)
                _manual_entry(conn, table="baseline", phone=phone, rating_points=rating_points, kills=kills, action=action)
        except ValueError as exc:
            return _redirect(str(exc), error=True)
        return _redirect("Строка глобального рейтинга обновлена" if action != "delete" else "Строка глобального рейтинга удалена")

    @app.post("/api/master/hijack-rating/{import_id:int}/entry")
    async def master_tournament_entry(
        request: Request,
        import_id: int,
        csrf_token: str = Form(...),
        phone: str = Form(...),
        rating_points: str = Form("0"),
        kills: str = Form("0"),
        action: str = Form("save"),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                ensure_baseline_schema(conn)
                _manual_entry(
                    conn,
                    table="tournament",
                    import_id=import_id,
                    phone=phone,
                    rating_points=rating_points,
                    kills=kills,
                    action=action,
                )
                legacy_rating._expire_previous_latest_titles(conn)
        except ValueError as exc:
            return _redirect(str(exc), error=True)
        return _redirect("Строка турнира обновлена" if action != "delete" else "Строка турнира удалена")

    return app


__all__ = [
    "ensure_baseline_schema",
    "hijack_member_metrics_v2",
    "hijack_rating_payload_v2",
    "install_hijack_rating_baseline",
]
