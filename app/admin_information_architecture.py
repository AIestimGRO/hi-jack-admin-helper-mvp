from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.db import connect, transaction
from app.legacy_jackside_copy import (
    copy_legacy_campaign_to_issue,
    list_legacy_daily_campaigns,
)
from app.product_shell import _check_csrf, _require_master
from app.services.jackside_analytics import build_jackside_analytics
from app.services.jackside_issues import (
    copy_issue,
    create_issue,
    ensure_issue_campaign,
    list_issues,
    schedule_issue,
)
from app.services.phone import display_phone


def _asset_version() -> str:
    static_dir = BASE_DIR / "app" / "static"
    digest = hashlib.sha256()
    for asset_path in sorted(path for path in static_dir.rglob("*") if path.is_file()):
        digest.update(asset_path.relative_to(static_dir).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(asset_path.read_bytes())
    return digest.hexdigest()[:12]


ASSET_VERSION = _asset_version()


def _csrf(request: Request) -> str:
    token = str(request.session.get("csrf") or "")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def _display_datetime(value: Any, timezone_name: str = "Europe/Moscow") -> str:
    if value is None or not str(value).strip():
        return "—"
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ZoneInfo(timezone_name)).strftime("%d.%m.%Y %H:%M")


def _display_local_datetime(value: Any, timezone_name: str = "Europe/Moscow") -> str:
    """Display a campaign wall-clock timestamp without adding a second UTC offset."""
    if value is None or not str(value).strip():
        return "—"
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        return parsed.strftime("%d.%m.%Y %H:%M")
    return parsed.astimezone(ZoneInfo(timezone_name)).strftime("%d.%m.%Y %H:%M")


def _context(request: Request, **values: Any) -> dict[str, Any]:
    return {
        "request": request,
        "csrf_token": _csrf(request),
        "admin_name": request.session.get("admin_name", "Master"),
        "admin_role": request.session.get("admin_role", "master_admin"),
        "asset_version": ASSET_VERSION,
        **values,
    }


def _redirect(path: str, message: str, *, error: bool = False) -> RedirectResponse:
    key = "error" if error else "ok"
    return RedirectResponse(f"{path}?{urlencode({key: message})}", status_code=303)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


_CLIENT_SORT_KEYS = {
    "id": "id",
    "name": "sort_name COLLATE NOCASE",
    "phone": "sort_phone",
    "rating": "hijack_rating",
    "balance": "jc_balance",
    "earned": "jc_earned",
    "last_jc": "jc_last_at",
    "updated": "sort_updated",
}


def _client_filter_number(value: Any, *, integer: bool) -> int | float | None:
    clean = str(value or "").strip().replace(",", ".")
    if not clean:
        return None
    try:
        return int(clean) if integer else float(clean)
    except ValueError:
        return None


def _client_filter_date(value: Any) -> str:
    clean = str(value or "").strip()
    if not clean:
        return ""
    try:
        return date.fromisoformat(clean).isoformat()
    except ValueError:
        return ""


def _client_rows(
    conn: sqlite3.Connection,
    *,
    query: str,
    page: int,
    filters: dict[str, str] | None = None,
    sort: str = "updated",
    direction: str = "desc",
    page_size: int = 50,
) -> tuple[list[sqlite3.Row], int]:
    filters = filters or {}
    clean = " ".join(str(query or "").split())
    base_clauses = ["IFNULL(c.client_status,'')<>'deleted'"]
    base_params: list[Any] = []
    if clean:
        like = f"%{clean}%"
        base_clauses.append(
            """
            (
              CAST(c.id AS TEXT)=? OR
              IFNULL(c.phone_local,'') LIKE ? OR
              IFNULL(c.phone_raw,'') LIKE ? OR
              IFNULL(c.first_name,'') LIKE ? OR
              IFNULL(c.nickname,'') LIKE ? OR
              IFNULL(c.username,'') LIKE ? OR
              IFNULL(c.email,'') LIKE ? OR
              IFNULL(c.app_user_id,'') LIKE ?
            )
            """
        )
        base_params.extend([clean, like, like, like, like, like, like, like])

    rating_expr = "0"
    if _table_exists(conn, "hi_jack_rating_entries"):
        rating_expr = (
            "COALESCE((SELECT SUM(hre.rating_points) FROM hi_jack_rating_entries hre "
            "WHERE hre.client_id=c.id),0)"
        )

    cte = f"""
        WITH client_metrics AS (
          SELECT c.*,
                 LOWER(
                   COALESCE(
                     NULLIF(c.first_name,''),
                     NULLIF(c.nickname,''),
                     NULLIF(c.username,''),
                     ''
                   )
                 ) AS sort_name,
                 COALESCE(NULLIF(c.phone_local,''),NULLIF(c.phone_raw,''),'') AS sort_phone,
                 COALESCE(c.updated_at,c.created_at) AS sort_updated,
                 COALESCE((SELECT SUM(jl.amount) FROM jackcoin_ledger jl
                           WHERE jl.client_id=c.id),0) AS jc_balance,
                 COALESCE((SELECT SUM(CASE WHEN jl.amount>0 THEN jl.amount ELSE 0 END)
                           FROM jackcoin_ledger jl WHERE jl.client_id=c.id),0) AS jc_earned,
                 (SELECT MAX(jl.created_at) FROM jackcoin_ledger jl
                  WHERE jl.client_id=c.id) AS jc_last_at,
                 {rating_expr} AS hijack_rating
          FROM clients c
          WHERE {" AND ".join(base_clauses)}
        )
    """

    metric_clauses = ["1=1"]
    metric_params: list[Any] = []

    client_id = str(filters.get("client_id") or "").strip()
    if client_id:
        if client_id.isdigit():
            metric_clauses.append("id=?")
            metric_params.append(int(client_id))
        else:
            metric_clauses.append("0=1")

    name = " ".join(str(filters.get("name") or "").split())
    if name:
        like = f"%{name}%"
        metric_clauses.append(
            """
            (
              IFNULL(first_name,'') LIKE ? OR
              IFNULL(nickname,'') LIKE ? OR
              IFNULL(username,'') LIKE ? OR
              IFNULL(email,'') LIKE ?
            )
            """
        )
        metric_params.extend([like, like, like, like])

    phone = "".join(
        character
        for character in str(filters.get("phone") or "")
        if character.isdigit()
    )
    if phone:
        metric_clauses.append("IFNULL(phone_local,'') LIKE ?")
        metric_params.append(f"%{phone[-10:]}%")

    for key, column, integer, operator in (
        ("rating_min", "hijack_rating", False, ">="),
        ("rating_max", "hijack_rating", False, "<="),
        ("balance_min", "jc_balance", True, ">="),
        ("balance_max", "jc_balance", True, "<="),
        ("earned_min", "jc_earned", True, ">="),
        ("earned_max", "jc_earned", True, "<="),
    ):
        value = _client_filter_number(filters.get(key), integer=integer)
        if value is not None:
            metric_clauses.append(f"{column}{operator}?")
            metric_params.append(value)

    last_from = _client_filter_date(filters.get("last_jc_from"))
    if last_from:
        metric_clauses.append("date(jc_last_at)>=date(?)")
        metric_params.append(last_from)
    last_to = _client_filter_date(filters.get("last_jc_to"))
    if last_to:
        metric_clauses.append("date(jc_last_at)<=date(?)")
        metric_params.append(last_to)

    metric_where = " WHERE " + " AND ".join(metric_clauses)
    total = int(
        conn.execute(
            cte + " SELECT COUNT(*) FROM client_metrics" + metric_where,
            (*base_params, *metric_params),
        ).fetchone()[0]
    )

    sort_key = sort if sort in _CLIENT_SORT_KEYS else "updated"
    direction_sql = "ASC" if str(direction).lower() == "asc" else "DESC"
    sort_expr = _CLIENT_SORT_KEYS[sort_key]
    if sort_key == "last_jc":
        order_sql = (
            "CASE WHEN jc_last_at IS NULL THEN 1 ELSE 0 END ASC, "
            f"jc_last_at {direction_sql}, id {direction_sql}"
        )
    else:
        order_sql = f"{sort_expr} {direction_sql}, id {direction_sql}"

    offset = (max(1, page) - 1) * page_size
    rows = conn.execute(
        cte
        + " SELECT * FROM client_metrics"
        + metric_where
        + f" ORDER BY {order_sql} LIMIT ? OFFSET ?",
        (*base_params, *metric_params, page_size, offset),
    ).fetchall()
    return list(rows), total


def _jackside_campaign_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT qc.id,qc.code,qc.title,qc.is_active,qc.active_from,qc.active_until,
                   qc.archived_at,qc.created_at,
                   ji.id AS issue_id,ji.issue_date,ji.status AS issue_status,
                   ji.starts_at AS issue_starts_at,
                   (SELECT COUNT(*) FROM quiz_questions qq
                    WHERE qq.campaign_code=qc.code AND IFNULL(qq.is_active,1)=1
                      AND IFNULL(qq.game_round,'main')='main') AS main_count,
                   (SELECT COUNT(*) FROM quiz_questions qq
                    WHERE qq.campaign_code=qc.code AND IFNULL(qq.is_active,1)=1
                      AND qq.game_round='final') AS final_count,
                   (SELECT COUNT(*) FROM quiz_submissions qs
                    WHERE qs.campaign_code=qc.code) AS submissions
            FROM quiz_campaigns qc
            LEFT JOIN jackside_issues ji ON ji.campaign_code=qc.code
            WHERE qc.campaign_type='daily_414' AND qc.deleted_at IS NULL
            ORDER BY COALESCE(ji.issue_date,qc.active_from,qc.created_at) DESC,qc.id DESC
            LIMIT 60
            """
        ).fetchall()
    )


def _schedule_error_text(exc: ValueError) -> str:
    raw = str(exc)
    if not raw.startswith("issue_invalid:"):
        return raw
    reasons = raw.split(":", 1)[1].split(",")
    labels = {
        "main_questions_must_be_ten": "нужно ровно 10 основных вопросов",
        "final_questions_required": "нужен хотя бы один финальный вопрос",
        "invalid_schedule_start": "неверно задан старт",
        "invalid_schedule_end": "неверно задан конец выпуска",
        "invalid_jackcoin_prize": "неверно задан главный приз JACKCOIN",
        "missing_card_prize": "не выбрана карточка главного приза",
        "invalid_card_prize": "карточка главного приза недоступна",
        "missing_rules_version": "не найдена версия правил",
    }
    readable = [labels.get(reason, reason) for reason in reasons]
    return "; ".join(readable)


def install_admin_information_architecture(app: FastAPI) -> FastAPI:
    if getattr(app.state, "admin_information_architecture_installed", False):
        return app
    app.state.admin_information_architecture_installed = True
    settings = app.state.settings
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
    templates.env.globals["display_phone"] = display_phone
    templates.env.globals["display_datetime"] = lambda value: _display_datetime(
        value, settings.timezone_name
    )
    templates.env.globals["display_local_datetime"] = (
        lambda value: _display_local_datetime(value, settings.timezone_name)
    )

    @app.get("/master/clients", response_class=HTMLResponse)
    async def master_clients_workspace(
        request: Request,
        q: str = Query(""),
        page: int = Query(1, ge=1),
        sort: str = Query("updated"),
        direction: str = Query("desc"),
        client_id: str = Query(""),
        name: str = Query(""),
        phone: str = Query(""),
        rating_min: str = Query(""),
        rating_max: str = Query(""),
        balance_min: str = Query(""),
        balance_max: str = Query(""),
        earned_min: str = Query(""),
        earned_max: str = Query(""),
        last_jc_from: str = Query(""),
        last_jc_to: str = Query(""),
    ):
        _require_master(request)
        sort = sort if sort in _CLIENT_SORT_KEYS else "updated"
        direction = "asc" if str(direction).lower() == "asc" else "desc"
        filters = {
            "client_id": str(client_id or "").strip(),
            "name": str(name or "").strip(),
            "phone": str(phone or "").strip(),
            "rating_min": str(rating_min or "").strip(),
            "rating_max": str(rating_max or "").strip(),
            "balance_min": str(balance_min or "").strip(),
            "balance_max": str(balance_max or "").strip(),
            "earned_min": str(earned_min or "").strip(),
            "earned_max": str(earned_max or "").strip(),
            "last_jc_from": str(last_jc_from or "").strip(),
            "last_jc_to": str(last_jc_to or "").strip(),
        }
        with connect(settings.db_path) as conn:
            rows, total = _client_rows(
                conn,
                query=q,
                page=page,
                filters=filters,
                sort=sort,
                direction=direction,
            )

        preserved = {"q": str(q or "").strip()}
        preserved.update(
            {
                key: value
                for key, value in filters.items()
                if str(value or "").strip()
            }
        )
        has_filters = bool(preserved["q"] or any(filters.values()))
        sort_defaults = {
            "id": "asc",
            "name": "asc",
            "phone": "asc",
            "rating": "desc",
            "balance": "desc",
            "earned": "desc",
            "last_jc": "desc",
        }
        sort_links: dict[str, str] = {}
        for key, default_direction in sort_defaults.items():
            next_direction = (
                ("desc" if direction == "asc" else "asc")
                if sort == key
                else default_direction
            )
            params = {
                **preserved,
                "sort": key,
                "direction": next_direction,
            }
            sort_links[key] = "/master/clients?" + urlencode(params)

        pagination_params = {
            **preserved,
            "sort": sort,
            "direction": direction,
        }
        return templates.TemplateResponse(
            request,
            "admin_clients_workspace.html",
            _context(
                request,
                clients=rows,
                q=q,
                filters=filters,
                sort=sort,
                direction=direction,
                sort_links=sort_links,
                has_filters=has_filters,
                pagination_query=urlencode(pagination_params),
                page=page,
                total=total,
                page_size=50,
            ),
        )

    @app.get("/master/reports", response_class=HTMLResponse)
    async def master_reports_workspace(
        request: Request,
        report: str = Query("overview"),
        campaign: str = Query(""),
    ):
        _require_master(request)
        report = report if report in {"overview", "results", "issues"} else "overview"
        with connect(settings.db_path) as conn:
            snapshot = build_jackside_analytics(
                conn, timezone_name=settings.timezone_name
            )
            campaigns = conn.execute(
                """
                SELECT code,title FROM quiz_campaigns
                WHERE deleted_at IS NULL ORDER BY created_at DESC,id DESC
                """
            ).fetchall()
            result_params: list[Any] = []
            result_where = ""
            if campaign:
                result_where = "WHERE qs.campaign_code=?"
                result_params.append(campaign)
            results = conn.execute(
                f"""
                SELECT qs.id,qs.campaign_code,qs.client_id,qs.correct_count,
                       qs.max_correct_count,qs.completion_time_ms,qs.jackcoin_awarded,
                       qs.created_at,c.first_name,c.nickname,c.username,
                       qc.title AS campaign_title,qc.campaign_type
                FROM quiz_submissions qs
                JOIN clients c ON c.id=qs.client_id
                LEFT JOIN quiz_campaigns qc ON qc.code=qs.campaign_code
                {result_where}
                ORDER BY qs.created_at DESC,qs.id DESC LIMIT 100
                """,
                result_params,
            ).fetchall()
            issue_rows = conn.execute(
                """
                SELECT qc.code,qc.title,ji.issue_date,
                       COUNT(qs.id) AS submissions,
                       COUNT(DISTINCT qs.client_id) AS players,
                       ROUND(AVG(qs.correct_count),1) AS avg_correct,
                       ROUND(AVG(qs.completion_time_ms)/1000.0,1) AS avg_seconds,
                       COALESCE(SUM(qs.jackcoin_awarded),0) AS awarded_jc
                FROM quiz_campaigns qc
                LEFT JOIN jackside_issues ji ON ji.campaign_code=qc.code
                LEFT JOIN quiz_submissions qs ON qs.campaign_code=qc.code
                  AND IFNULL(qs.main_round_completed,1)=1
                WHERE qc.campaign_type='daily_414'
                GROUP BY qc.code,qc.title,ji.issue_date
                ORDER BY COALESCE(ji.issue_date,qc.active_from,qc.created_at) DESC
                LIMIT 60
                """
            ).fetchall()
        return templates.TemplateResponse(
            request,
            "admin_reports_workspace.html",
            _context(
                request,
                report=report,
                campaign=campaign,
                campaigns=campaigns,
                results=results,
                issues=issue_rows,
                analytics=snapshot.get("admin", {}),
                analytics_generated_at=snapshot.get("generated_at"),
            ),
        )

    @app.get("/master/jackside", response_class=HTMLResponse)
    async def master_jackside_workspace(
        request: Request,
        source: str = Query(""),
        ok: str = Query(""),
        error: str = Query(""),
    ):
        _require_master(request)
        with connect(settings.db_path) as conn:
            issues = [dict(row) for row in list_issues(conn, limit=60)]
            campaigns = [dict(row) for row in _jackside_campaign_rows(conn)]
            legacy = list_legacy_daily_campaigns(conn, limit=100)
            campaign_ids = {
                str(row["code"]): int(row["id"])
                for row in conn.execute(
                    "SELECT id,code FROM quiz_campaigns WHERE campaign_type='daily_414'"
                ).fetchall()
            }
            for issue in issues:
                issue["campaign_id"] = campaign_ids.get(str(issue["campaign_code"]))
        return templates.TemplateResponse(
            request,
            "admin_jackside_workspace.html",
            _context(
                request,
                issues=issues,
                campaigns=campaigns,
                legacy=legacy,
                selected_source=source,
                ok=ok,
                error=error,
            ),
        )

    @app.post("/api/master/jackside/create-release")
    async def master_create_release(
        request: Request,
        issue_date: str = Form(...),
        starts_at: str = Form(...),
        title: str = Form(""),
        source: str = Form(""),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        try:
            day = date.fromisoformat(issue_date)
            starts_local = datetime.fromisoformat(starts_at)
        except ValueError:
            return _redirect("/master/jackside", "Некорректная дата или время", error=True)
        club_tz = ZoneInfo(settings.timezone_name)
        if starts_local.tzinfo is None:
            starts_local = starts_local.replace(tzinfo=club_tz)
        else:
            starts_local = starts_local.astimezone(club_tz)
        if starts_local.date() != day:
            return _redirect(
                "/master/jackside",
                "Дата старта должна совпадать с датой выпуска",
                error=True,
            )
        source = str(source or "").strip()
        scheduled = False
        schedule_warning = ""
        try:
            with transaction(settings.db_path) as conn:
                if source.startswith("issue:"):
                    issue = copy_issue(
                        conn,
                        source_issue_id=int(source.split(":", 1)[1]),
                        issue_date_value=day,
                        starts_at=starts_local,
                        admin_id=request.session.get("admin_id"),
                        timezone_name=settings.timezone_name,
                    )
                elif source.startswith("legacy:"):
                    issue = copy_legacy_campaign_to_issue(
                        conn,
                        source_campaign_id=int(source.split(":", 1)[1]),
                        issue_date_value=day,
                        starts_at=starts_local,
                        admin_id=request.session.get("admin_id"),
                        timezone_name=settings.timezone_name,
                    )
                elif source:
                    raise ValueError("invalid_source")
                else:
                    issue = create_issue(
                        conn,
                        issue_date_value=day,
                        starts_at=starts_local,
                        title=title.strip() or None,
                        admin_id=request.session.get("admin_id"),
                        timezone_name=settings.timezone_name,
                    )
                if title.strip():
                    conn.execute(
                        "UPDATE jackside_issues SET title=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (title.strip()[:100], int(issue["id"])),
                    )
                    issue = conn.execute(
                        "SELECT * FROM jackside_issues WHERE id=?", (int(issue["id"]),)
                    ).fetchone()
                campaign_row = ensure_issue_campaign(
                    conn, issue=issue, timezone_name=settings.timezone_name
                )
                if title.strip():
                    conn.execute(
                        "UPDATE quiz_campaigns SET title=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (title.strip()[:100], int(campaign_row["id"])),
                    )
                if source:
                    try:
                        schedule_issue(
                            conn,
                            issue_id=int(issue["id"]),
                            timezone_name=settings.timezone_name,
                        )
                        scheduled = True
                    except ValueError as exc:
                        schedule_warning = _schedule_error_text(exc)
        except (ValueError, sqlite3.IntegrityError) as exc:
            messages = {
                "issue_date_exists": "На эту дату выпуск JACKSIDE уже существует",
                "issue_not_found": "Исходный выпуск не найден",
                "legacy_campaign_not_found": "Старый квиз не найден",
                "legacy_campaign_has_no_questions": "В старом квизе нет активных вопросов",
                "invalid_source": "Неизвестный источник копирования",
            }
            return _redirect(
                "/master/jackside", messages.get(str(exc), str(exc)), error=True
            )
        if scheduled:
            return _redirect(
                "/master/jackside",
                f"Создан и запланирован JACKSIDE на {day.strftime('%d.%m.%Y')}",
            )
        if source and schedule_warning:
            return _redirect(
                "/master/jackside",
                "Черновик создан, но не запланирован: " + schedule_warning,
                error=True,
            )
        return _redirect(
            "/master/jackside",
            f"Создан черновик JACKSIDE на {day.strftime('%d.%m.%Y')}",
        )

    @app.get("/api/master/referral-qualification-settings")
    async def referral_qualification_settings(request: Request):
        _require_master(request)
        with connect(settings.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM jackside_referral_settings WHERE id=1"
            ).fetchone()
            preferences = conn.execute(
                """
                SELECT code,title FROM preference_types
                WHERE is_active=1 AND kind='counter'
                ORDER BY position,id
                """
            ).fetchall()
        return JSONResponse(
            {
                "settings": dict(row) if row else {
                    "referrer_preference_code": "",
                    "referrer_amount": 0,
                    "referrer_delivery_mode": "automatic",
                    "invited_preference_code": "",
                    "invited_amount": 0,
                    "invited_delivery_mode": "automatic",
                },
                "preferences": [dict(item) for item in preferences],
                "csrf_token": _csrf(request),
            }
        )

    @app.get("/master/settings", response_class=HTMLResponse)
    async def master_settings_workspace(request: Request):
        _require_master(request)
        return templates.TemplateResponse(
            request,
            "admin_settings_workspace.html",
            _context(request),
        )

    return app


__all__ = ["install_admin_information_architecture"]
