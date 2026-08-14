from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.db import connect, transaction
from app.product_shell import _check_csrf, _csrf_token, _current_member, _require_master


ASSET_VERSION = "tournaments-core-1"
ACTIVE_REGISTRATION_STATUSES = {"registered", "checked_in", "played"}
CANCELLABLE_REGISTRATION_STATUSES = {"registered", "waitlist"}
ALL_REGISTRATION_STATUSES = {
    "registered",
    "waitlist",
    "cancelled",
    "checked_in",
    "played",
    "no_show",
}


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        str(row[1]) == column
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    )


def ensure_tournament_schema(conn: sqlite3.Connection) -> None:
    """Create/extend the tournament schema without destructive migrations."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS club_tournaments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            starts_at TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            format_text TEXT NOT NULL DEFAULT '',
            buy_in_text TEXT NOT NULL DEFAULT '',
            max_slots INTEGER NOT NULL DEFAULT 0 CHECK(max_slots >= 0),
            registration_open INTEGER NOT NULL DEFAULT 1,
            is_published INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'scheduled'
                CHECK(status IN ('draft', 'scheduled', 'registration_closed', 'completed', 'cancelled')),
            created_by_admin_id INTEGER REFERENCES admins(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    additive_columns = {
        "stack_text": "TEXT NOT NULL DEFAULT ''",
        "levels_text": "TEXT NOT NULL DEFAULT ''",
        "reentry_text": "TEXT NOT NULL DEFAULT ''",
        "addon_text": "TEXT NOT NULL DEFAULT ''",
        "guarantee_text": "TEXT NOT NULL DEFAULT ''",
        "late_registration_text": "TEXT NOT NULL DEFAULT ''",
        "venue_text": "TEXT NOT NULL DEFAULT ''",
        "registration_opens_at": "TEXT",
        "registration_closes_at": "TEXT",
        "published_at": "TEXT",
    }
    for column, definition in additive_columns.items():
        if not _has_column(conn, "club_tournaments", column):
            conn.execute(
                f"ALTER TABLE club_tournaments ADD COLUMN {column} {definition}"
            )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_club_tournaments_public_schedule
        ON club_tournaments(is_published, status, starts_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS club_tournament_registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id INTEGER NOT NULL
                REFERENCES club_tournaments(id) ON DELETE CASCADE,
            client_id INTEGER NOT NULL
                REFERENCES clients(id) ON DELETE CASCADE,
            account_id INTEGER
                REFERENCES member_accounts(id) ON DELETE SET NULL,
            status TEXT NOT NULL DEFAULT 'registered'
                CHECK(status IN ('registered','waitlist','cancelled','checked_in','played','no_show')),
            source TEXT NOT NULL DEFAULT 'member',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            cancelled_at TEXT,
            UNIQUE(tournament_id, client_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_tournament_registrations_tournament_status
        ON club_tournament_registrations(tournament_id, status, created_at, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_tournament_registrations_client
        ON club_tournament_registrations(client_id, tournament_id)
        """
    )


def _ensure_tournament_schema(db_path: str) -> None:
    with transaction(db_path) as conn:
        ensure_tournament_schema(conn)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_utc(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _local_form_to_utc(value: str, timezone_name: str, *, required: bool = False) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        if required:
            raise ValueError("Укажите дату и время")
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("Некорректная дата или время") from exc
    club_tz = ZoneInfo(timezone_name)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=club_tz)
    else:
        parsed = parsed.astimezone(club_tz)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _utc_to_local_input(value: Any, timezone_name: str) -> str:
    parsed = _parse_iso_utc(value)
    if parsed is None:
        return ""
    return parsed.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%dT%H:%M")


def _display_datetime(value: Any, timezone_name: str) -> str:
    parsed = _parse_iso_utc(value)
    if parsed is None:
        return str(value or "")
    return parsed.astimezone(ZoneInfo(timezone_name)).strftime("%d.%m.%Y · %H:%M")


def _registration_counts(conn: sqlite3.Connection, tournament_id: int) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS amount
        FROM club_tournament_registrations
        WHERE tournament_id=?
        GROUP BY status
        """,
        (int(tournament_id),),
    ).fetchall()
    counts = {str(row["status"]): int(row["amount"]) for row in rows}
    registered = sum(counts.get(status, 0) for status in ACTIVE_REGISTRATION_STATUSES)
    return {
        "registered": registered,
        "waitlist": counts.get("waitlist", 0),
        "cancelled": counts.get("cancelled", 0),
        "checked_in": counts.get("checked_in", 0),
        "played": counts.get("played", 0),
        "no_show": counts.get("no_show", 0),
    }


def _registration_window_state(row: sqlite3.Row | dict[str, Any], now: datetime) -> str:
    item = dict(row)
    if not bool(item.get("is_published")) or str(item.get("status")) != "scheduled":
        return "closed"
    if not bool(item.get("registration_open")):
        return "closed"
    opens_at = _parse_iso_utc(item.get("registration_opens_at"))
    closes_at = _parse_iso_utc(item.get("registration_closes_at"))
    starts_at = _parse_iso_utc(item.get("starts_at"))
    if opens_at and now < opens_at:
        return "not_open"
    effective_close = closes_at or starts_at
    if effective_close and now >= effective_close:
        return "closed"
    return "open"


def tournament_payload(
    conn: sqlite3.Connection,
    row: sqlite3.Row | dict[str, Any],
    *,
    client_id: int | None,
    timezone_name: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    item = dict(row)
    tournament_id = int(item["id"])
    counts = _registration_counts(conn, tournament_id)
    max_slots = max(0, int(item.get("max_slots") or 0))
    seats_left = None if max_slots == 0 else max(0, max_slots - counts["registered"])
    state = _registration_window_state(item, now or _now_utc())
    my_status = None
    if client_id is not None:
        registration = conn.execute(
            """
            SELECT status FROM club_tournament_registrations
            WHERE tournament_id=? AND client_id=?
            """,
            (tournament_id, int(client_id)),
        ).fetchone()
        if registration:
            my_status = str(registration["status"])

    capacity_full = max_slots > 0 and counts["registered"] >= max_slots
    item.update(
        {
            "starts_at_display": _display_datetime(item.get("starts_at"), timezone_name),
            "starts_at_input": _utc_to_local_input(item.get("starts_at"), timezone_name),
            "registration_opens_at_display": _display_datetime(
                item.get("registration_opens_at"), timezone_name
            ),
            "registration_opens_at_input": _utc_to_local_input(
                item.get("registration_opens_at"), timezone_name
            ),
            "registration_closes_at_display": _display_datetime(
                item.get("registration_closes_at"), timezone_name
            ),
            "registration_closes_at_input": _utc_to_local_input(
                item.get("registration_closes_at"), timezone_name
            ),
            "registered_count": counts["registered"],
            "waitlist_count": counts["waitlist"],
            "seats_left": seats_left,
            "registration_state": "waitlist" if state == "open" and capacity_full else state,
            "my_registration_status": my_status,
            "can_register": state == "open"
            and my_status not in ACTIVE_REGISTRATION_STATUSES
            and my_status != "waitlist",
            "can_cancel": my_status in CANCELLABLE_REGISTRATION_STATUSES,
            "detail_url": f"/account/tournaments/{tournament_id}",
        }
    )
    return item


def _get_tournament(conn: sqlite3.Connection, tournament_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM club_tournaments WHERE id=?", (int(tournament_id),)
    ).fetchone()
    if row is None:
        raise ValueError("tournament_not_found")
    return row


def register_client_for_tournament(
    conn: sqlite3.Connection,
    *,
    tournament_id: int,
    client_id: int,
    account_id: int | None,
    source: str = "member",
    now: datetime | None = None,
) -> sqlite3.Row:
    """Idempotently register a client, using waitlist when capacity is full."""
    now = now or _now_utc()
    tournament = _get_tournament(conn, tournament_id)
    if _registration_window_state(tournament, now) != "open":
        raise ValueError("registration_closed")

    existing = conn.execute(
        """
        SELECT * FROM club_tournament_registrations
        WHERE tournament_id=? AND client_id=?
        """,
        (int(tournament_id), int(client_id)),
    ).fetchone()
    if existing and str(existing["status"]) in ACTIVE_REGISTRATION_STATUSES | {"waitlist"}:
        return existing

    counts = _registration_counts(conn, tournament_id)
    max_slots = max(0, int(tournament["max_slots"] or 0))
    next_status = (
        "waitlist"
        if max_slots > 0 and counts["registered"] >= max_slots
        else "registered"
    )
    source = str(source or "member").strip()[:32] or "member"
    if existing:
        conn.execute(
            """
            UPDATE club_tournament_registrations
            SET account_id=?,status=?,source=?,cancelled_at=NULL,updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (account_id, next_status, source, int(existing["id"])),
        )
        registration_id = int(existing["id"])
    else:
        cursor = conn.execute(
            """
            INSERT INTO club_tournament_registrations(
                tournament_id,client_id,account_id,status,source
            ) VALUES (?,?,?,?,?)
            """,
            (int(tournament_id), int(client_id), account_id, next_status, source),
        )
        registration_id = int(cursor.lastrowid)
    return conn.execute(
        "SELECT * FROM club_tournament_registrations WHERE id=?",
        (registration_id,),
    ).fetchone()


def _promote_waitlist(conn: sqlite3.Connection, tournament_id: int) -> sqlite3.Row | None:
    tournament = _get_tournament(conn, tournament_id)
    max_slots = max(0, int(tournament["max_slots"] or 0))
    if max_slots == 0:
        return None
    counts = _registration_counts(conn, tournament_id)
    if counts["registered"] >= max_slots:
        return None
    candidate = conn.execute(
        """
        SELECT * FROM club_tournament_registrations
        WHERE tournament_id=? AND status='waitlist'
        ORDER BY created_at,id
        LIMIT 1
        """,
        (int(tournament_id),),
    ).fetchone()
    if candidate is None:
        return None
    conn.execute(
        """
        UPDATE club_tournament_registrations
        SET status='registered',updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (int(candidate["id"]),),
    )
    return conn.execute(
        "SELECT * FROM club_tournament_registrations WHERE id=?",
        (int(candidate["id"]),),
    ).fetchone()


def cancel_client_registration(
    conn: sqlite3.Connection,
    *,
    tournament_id: int,
    client_id: int,
    now: datetime | None = None,
) -> sqlite3.Row:
    tournament = _get_tournament(conn, tournament_id)
    starts_at = _parse_iso_utc(tournament["starts_at"])
    if starts_at and (now or _now_utc()) >= starts_at:
        raise ValueError("tournament_started")
    registration = conn.execute(
        """
        SELECT * FROM club_tournament_registrations
        WHERE tournament_id=? AND client_id=?
        """,
        (int(tournament_id), int(client_id)),
    ).fetchone()
    if registration is None or str(registration["status"]) == "cancelled":
        raise ValueError("registration_not_found")
    if str(registration["status"]) not in CANCELLABLE_REGISTRATION_STATUSES:
        raise ValueError("registration_locked")
    released_seat = str(registration["status"]) == "registered"
    conn.execute(
        """
        UPDATE club_tournament_registrations
        SET status='cancelled',cancelled_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (int(registration["id"]),),
    )
    if released_seat:
        _promote_waitlist(conn, tournament_id)
    return conn.execute(
        "SELECT * FROM club_tournament_registrations WHERE id=?",
        (int(registration["id"]),),
    ).fetchone()


def _public_tournament_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    now_iso = _now_utc().isoformat(timespec="seconds")
    return list(
        conn.execute(
            """
            SELECT * FROM club_tournaments
            WHERE is_published=1
              AND status IN ('scheduled','registration_closed')
              AND starts_at>=?
            ORDER BY starts_at,id
            LIMIT 100
            """,
            (now_iso,),
        ).fetchall()
    )


def _admin_tournament_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM club_tournaments
            ORDER BY
              CASE status WHEN 'scheduled' THEN 0 WHEN 'registration_closed' THEN 1
                          WHEN 'draft' THEN 2 ELSE 3 END,
              starts_at DESC,id DESC
            LIMIT 200
            """
        ).fetchall()
    )


def _participant_rows(conn: sqlite3.Connection, tournament_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT r.*,c.first_name,c.nickname,c.username,c.phone_local,c.app_user_id
            FROM club_tournament_registrations r
            JOIN clients c ON c.id=r.client_id
            WHERE r.tournament_id=?
            ORDER BY
              CASE r.status WHEN 'registered' THEN 0 WHEN 'checked_in' THEN 1
                            WHEN 'waitlist' THEN 2 WHEN 'played' THEN 3 ELSE 4 END,
              r.created_at,r.id
            """,
            (int(tournament_id),),
        ).fetchall()
    )


def _clean_text(value: str, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _normalize_tournament_form(
    *,
    title: str,
    starts_at: str,
    description: str,
    format_text: str,
    buy_in_text: str,
    max_slots: int,
    stack_text: str,
    levels_text: str,
    reentry_text: str,
    addon_text: str,
    guarantee_text: str,
    late_registration_text: str,
    venue_text: str,
    registration_opens_at: str,
    registration_closes_at: str,
    timezone_name: str,
) -> dict[str, Any]:
    clean_title = _clean_text(title, limit=120)
    if not clean_title:
        raise ValueError("Укажите название турнира")
    start_utc = _local_form_to_utc(starts_at, timezone_name, required=True)
    opens_utc = _local_form_to_utc(registration_opens_at, timezone_name)
    closes_utc = _local_form_to_utc(registration_closes_at, timezone_name)
    start_dt = _parse_iso_utc(start_utc)
    opens_dt = _parse_iso_utc(opens_utc)
    closes_dt = _parse_iso_utc(closes_utc)
    if opens_dt and closes_dt and opens_dt >= closes_dt:
        raise ValueError("Открытие регистрации должно быть раньше закрытия")
    if closes_dt and start_dt and closes_dt > start_dt:
        raise ValueError("Регистрацию нельзя закрыть позже старта турнира")
    return {
        "title": clean_title,
        "starts_at": start_utc,
        "description": str(description or "").strip()[:4000],
        "format_text": _clean_text(format_text, limit=160),
        "buy_in_text": _clean_text(buy_in_text, limit=160),
        "max_slots": max(0, min(int(max_slots or 0), 10000)),
        "stack_text": _clean_text(stack_text, limit=160),
        "levels_text": _clean_text(levels_text, limit=160),
        "reentry_text": _clean_text(reentry_text, limit=160),
        "addon_text": _clean_text(addon_text, limit=160),
        "guarantee_text": _clean_text(guarantee_text, limit=160),
        "late_registration_text": _clean_text(late_registration_text, limit=160),
        "venue_text": _clean_text(venue_text, limit=200),
        "registration_opens_at": opens_utc,
        "registration_closes_at": closes_utc,
    }


def _message_for_error(code: str) -> str:
    return {
        "tournament_not_found": "Турнир не найден",
        "registration_closed": "Регистрация сейчас закрыта",
        "tournament_started": "Турнир уже начался",
        "registration_not_found": "Активная регистрация не найдена",
        "registration_locked": "Этот статус регистрации нельзя отменить",
        "client_not_found": "Клиент не найден",
    }.get(code, code)


def _wants_json(request: Request) -> bool:
    accept = str(request.headers.get("accept") or "").lower()
    return "application/json" in accept or str(
        request.headers.get("x-requested-with") or ""
    ).lower() == "fetch"


def _member_redirect(tournament_id: int, message: str, *, error: bool = False) -> RedirectResponse:
    key = "error" if error else "ok"
    return RedirectResponse(
        f"/account/tournaments/{int(tournament_id)}?{urlencode({key: message})}",
        status_code=303,
    )


def _admin_redirect(message: str, *, error: bool = False, edit: int | None = None) -> RedirectResponse:
    query: dict[str, Any] = {"error" if error else "ok": message}
    if edit is not None:
        query["edit"] = int(edit)
    return RedirectResponse(
        f"/master/tournaments?{urlencode(query)}",
        status_code=303,
    )


def install_tournaments_core(app: FastAPI) -> FastAPI:
    if getattr(app.state, "tournaments_core_installed", False):
        return app
    app.state.tournaments_core_installed = True
    settings = app.state.settings
    _ensure_tournament_schema(settings.db_path)
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

    @app.get("/api/account/tournaments")
    async def account_tournaments_api(request: Request):
        member = _current_member(request, required=True)
        with connect(settings.db_path) as conn:
            payload = [
                tournament_payload(
                    conn,
                    row,
                    client_id=int(member["client_id"]),
                    timezone_name=settings.timezone_name,
                )
                for row in _public_tournament_rows(conn)
            ]
        return JSONResponse({"tournaments": payload})

    @app.get("/api/account/tournaments/{tournament_id:int}")
    async def account_tournament_api(request: Request, tournament_id: int):
        member = _current_member(request, required=True)
        with connect(settings.db_path) as conn:
            row = conn.execute(
                """
                SELECT * FROM club_tournaments
                WHERE id=? AND is_published=1
                """,
                (int(tournament_id),),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="tournament_not_found")
            payload = tournament_payload(
                conn,
                row,
                client_id=int(member["client_id"]),
                timezone_name=settings.timezone_name,
            )
        return JSONResponse({"tournament": payload})

    @app.get("/account/tournaments/{tournament_id:int}", response_class=HTMLResponse)
    async def account_tournament_page(
        request: Request,
        tournament_id: int,
        ok: str = Query(""),
        error: str = Query(""),
    ):
        member = _current_member(request, required=True)
        with connect(settings.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM club_tournaments WHERE id=? AND is_published=1",
                (int(tournament_id),),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="tournament_not_found")
            payload = tournament_payload(
                conn,
                row,
                client_id=int(member["client_id"]),
                timezone_name=settings.timezone_name,
            )
        return templates.TemplateResponse(
            request,
            "member_tournament.html",
            {
                "request": request,
                "member": member,
                "tournament": payload,
                "current_tab": "quizzes",
                "current_rating_section": "month",
                "csrf_token": _csrf_token(request),
                "asset_version": ASSET_VERSION,
                "ok": ok,
                "error": error,
            },
        )

    @app.post("/api/account/tournaments/{tournament_id:int}/register")
    async def account_tournament_register(
        request: Request,
        tournament_id: int,
        csrf_token: str = Form(...),
    ):
        member = _current_member(request, required=True)
        _check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                registration = register_client_for_tournament(
                    conn,
                    tournament_id=tournament_id,
                    client_id=int(member["client_id"]),
                    account_id=int(member["id"]),
                    source="member",
                )
                row = _get_tournament(conn, tournament_id)
                payload = tournament_payload(
                    conn,
                    row,
                    client_id=int(member["client_id"]),
                    timezone_name=settings.timezone_name,
                )
        except ValueError as exc:
            message = _message_for_error(str(exc))
            if _wants_json(request):
                return JSONResponse({"ok": False, "error": message}, status_code=409)
            return _member_redirect(tournament_id, message, error=True)
        status = str(registration["status"])
        message = (
            "Ты в листе ожидания — сообщим, когда освободится место"
            if status == "waitlist"
            else "Ты зарегистрирован на турнир"
        )
        if _wants_json(request):
            return JSONResponse({"ok": True, "message": message, "tournament": payload})
        return _member_redirect(tournament_id, message)

    @app.post("/api/account/tournaments/{tournament_id:int}/cancel")
    async def account_tournament_cancel(
        request: Request,
        tournament_id: int,
        csrf_token: str = Form(...),
    ):
        member = _current_member(request, required=True)
        _check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                cancel_client_registration(
                    conn,
                    tournament_id=tournament_id,
                    client_id=int(member["client_id"]),
                )
                row = _get_tournament(conn, tournament_id)
                payload = tournament_payload(
                    conn,
                    row,
                    client_id=int(member["client_id"]),
                    timezone_name=settings.timezone_name,
                )
        except ValueError as exc:
            message = _message_for_error(str(exc))
            if _wants_json(request):
                return JSONResponse({"ok": False, "error": message}, status_code=409)
            return _member_redirect(tournament_id, message, error=True)
        message = "Регистрация отменена"
        if _wants_json(request):
            return JSONResponse({"ok": True, "message": message, "tournament": payload})
        return _member_redirect(tournament_id, message)

    @app.get("/master/tournaments", response_class=HTMLResponse)
    async def master_tournaments(
        request: Request,
        edit: int | None = Query(None, ge=1),
        ok: str = Query(""),
        error: str = Query(""),
    ):
        _require_master(request)
        with connect(settings.db_path) as conn:
            tournaments = [
                tournament_payload(
                    conn,
                    row,
                    client_id=None,
                    timezone_name=settings.timezone_name,
                )
                for row in _admin_tournament_rows(conn)
            ]
            selected = None
            participants: list[sqlite3.Row] = []
            if edit is not None:
                row = conn.execute(
                    "SELECT * FROM club_tournaments WHERE id=?", (int(edit),)
                ).fetchone()
                if row:
                    selected = tournament_payload(
                        conn,
                        row,
                        client_id=None,
                        timezone_name=settings.timezone_name,
                    )
                    participants = _participant_rows(conn, int(edit))
        return templates.TemplateResponse(
            request,
            "admin_tournaments_workspace.html",
            {
                "request": request,
                "tournaments": tournaments,
                "selected": selected,
                "participants": participants,
                "csrf_token": _csrf_token(request),
                "admin_name": request.session.get("admin_name", "Администратор"),
                "admin_role": request.session.get("admin_role", ""),
                "asset_version": ASSET_VERSION,
                "ok": ok,
                "error": error,
            },
        )

    @app.post("/api/master/tournaments")
    async def master_tournament_create(
        request: Request,
        title: str = Form(...),
        starts_at: str = Form(...),
        description: str = Form(""),
        format_text: str = Form(""),
        buy_in_text: str = Form(""),
        max_slots: int = Form(0),
        stack_text: str = Form(""),
        levels_text: str = Form(""),
        reentry_text: str = Form(""),
        addon_text: str = Form(""),
        guarantee_text: str = Form(""),
        late_registration_text: str = Form(""),
        venue_text: str = Form(""),
        registration_opens_at: str = Form(""),
        registration_closes_at: str = Form(""),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        try:
            values = _normalize_tournament_form(
                title=title,
                starts_at=starts_at,
                description=description,
                format_text=format_text,
                buy_in_text=buy_in_text,
                max_slots=max_slots,
                stack_text=stack_text,
                levels_text=levels_text,
                reentry_text=reentry_text,
                addon_text=addon_text,
                guarantee_text=guarantee_text,
                late_registration_text=late_registration_text,
                venue_text=venue_text,
                registration_opens_at=registration_opens_at,
                registration_closes_at=registration_closes_at,
                timezone_name=settings.timezone_name,
            )
        except ValueError as exc:
            return _admin_redirect(str(exc), error=True)
        columns = ",".join(values.keys())
        placeholders = ",".join("?" for _ in values)
        with transaction(settings.db_path) as conn:
            cursor = conn.execute(
                f"""
                INSERT INTO club_tournaments(
                    {columns},status,is_published,registration_open,created_by_admin_id
                ) VALUES ({placeholders},'draft',0,1,?)
                """,
                (*values.values(), request.session.get("admin_id")),
            )
            tournament_id = int(cursor.lastrowid)
        return _admin_redirect("Турнир создан как черновик", edit=tournament_id)

    @app.post("/api/master/tournaments/{tournament_id:int}")
    async def master_tournament_update(
        request: Request,
        tournament_id: int,
        title: str = Form(...),
        starts_at: str = Form(...),
        description: str = Form(""),
        format_text: str = Form(""),
        buy_in_text: str = Form(""),
        max_slots: int = Form(0),
        stack_text: str = Form(""),
        levels_text: str = Form(""),
        reentry_text: str = Form(""),
        addon_text: str = Form(""),
        guarantee_text: str = Form(""),
        late_registration_text: str = Form(""),
        venue_text: str = Form(""),
        registration_opens_at: str = Form(""),
        registration_closes_at: str = Form(""),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        try:
            values = _normalize_tournament_form(
                title=title,
                starts_at=starts_at,
                description=description,
                format_text=format_text,
                buy_in_text=buy_in_text,
                max_slots=max_slots,
                stack_text=stack_text,
                levels_text=levels_text,
                reentry_text=reentry_text,
                addon_text=addon_text,
                guarantee_text=guarantee_text,
                late_registration_text=late_registration_text,
                venue_text=venue_text,
                registration_opens_at=registration_opens_at,
                registration_closes_at=registration_closes_at,
                timezone_name=settings.timezone_name,
            )
        except ValueError as exc:
            return _admin_redirect(str(exc), error=True, edit=tournament_id)
        assignments = ",".join(f"{column}=?" for column in values)
        with transaction(settings.db_path) as conn:
            if conn.execute(
                "SELECT 1 FROM club_tournaments WHERE id=?", (int(tournament_id),)
            ).fetchone() is None:
                return _admin_redirect("Турнир не найден", error=True)
            conn.execute(
                f"UPDATE club_tournaments SET {assignments},updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (*values.values(), int(tournament_id)),
            )
        return _admin_redirect("Турнир обновлён", edit=tournament_id)

    @app.post("/api/master/tournaments/{tournament_id:int}/action")
    async def master_tournament_action(
        request: Request,
        tournament_id: int,
        action: str = Form(...),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        action = str(action or "").strip()
        with transaction(settings.db_path) as conn:
            try:
                row = _get_tournament(conn, tournament_id)
            except ValueError:
                return _admin_redirect("Турнир не найден", error=True)
            if action == "publish":
                if _parse_iso_utc(row["starts_at"]) is None:
                    return _admin_redirect("У турнира нет корректного времени старта", error=True, edit=tournament_id)
                conn.execute(
                    """
                    UPDATE club_tournaments
                    SET is_published=1,status='scheduled',published_at=COALESCE(published_at,CURRENT_TIMESTAMP),
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (int(tournament_id),),
                )
                message = "Турнир опубликован"
            elif action == "unpublish":
                conn.execute(
                    """
                    UPDATE club_tournaments
                    SET is_published=0,status=CASE WHEN status IN ('scheduled','registration_closed') THEN 'draft' ELSE status END,
                        registration_open=0,updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (int(tournament_id),),
                )
                message = "Турнир снят с публикации"
            elif action == "open_registration":
                conn.execute(
                    """
                    UPDATE club_tournaments
                    SET registration_open=1,
                        status=CASE WHEN status='registration_closed' THEN 'scheduled' ELSE status END,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (int(tournament_id),),
                )
                message = "Регистрация открыта"
            elif action == "close_registration":
                conn.execute(
                    """
                    UPDATE club_tournaments
                    SET registration_open=0,
                        status=CASE WHEN status='scheduled' THEN 'registration_closed' ELSE status END,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (int(tournament_id),),
                )
                message = "Регистрация закрыта"
            elif action == "cancel":
                conn.execute(
                    """
                    UPDATE club_tournaments
                    SET status='cancelled',registration_open=0,updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (int(tournament_id),),
                )
                message = "Турнир отменён"
            elif action == "complete":
                conn.execute(
                    """
                    UPDATE club_tournaments
                    SET status='completed',registration_open=0,updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (int(tournament_id),),
                )
                message = "Турнир завершён"
            elif action == "restore":
                conn.execute(
                    """
                    UPDATE club_tournaments
                    SET status='draft',is_published=0,registration_open=0,updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (int(tournament_id),),
                )
                message = "Турнир возвращён в черновик"
            else:
                return _admin_redirect("Неизвестное действие", error=True, edit=tournament_id)
        return _admin_redirect(message, edit=tournament_id)

    @app.post("/api/master/tournaments/{tournament_id:int}/registrations")
    async def master_tournament_add_registration(
        request: Request,
        tournament_id: int,
        client_id: int = Form(...),
        status: str = Form("registered"),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        status = str(status or "registered").strip()
        if status not in ALL_REGISTRATION_STATUSES:
            return _admin_redirect("Некорректный статус", error=True, edit=tournament_id)
        with transaction(settings.db_path) as conn:
            if conn.execute("SELECT 1 FROM clients WHERE id=?", (int(client_id),)).fetchone() is None:
                return _admin_redirect("Клиент не найден", error=True, edit=tournament_id)
            try:
                _get_tournament(conn, tournament_id)
            except ValueError:
                return _admin_redirect("Турнир не найден", error=True)
            existing = conn.execute(
                """
                SELECT id FROM club_tournament_registrations
                WHERE tournament_id=? AND client_id=?
                """,
                (int(tournament_id), int(client_id)),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE club_tournament_registrations
                    SET status=?,source='admin',cancelled_at=CASE WHEN ?='cancelled' THEN CURRENT_TIMESTAMP ELSE NULL END,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (status, status, int(existing["id"])),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO club_tournament_registrations(
                        tournament_id,client_id,status,source,cancelled_at
                    ) VALUES (?,?,?,'admin',CASE WHEN ?='cancelled' THEN CURRENT_TIMESTAMP ELSE NULL END)
                    """,
                    (int(tournament_id), int(client_id), status, status),
                )
        return _admin_redirect("Статус участника сохранён", edit=tournament_id)

    return app


__all__ = [
    "install_tournaments_core",
    "ensure_tournament_schema",
    "register_client_for_tournament",
    "cancel_client_registration",
    "tournament_payload",
]
