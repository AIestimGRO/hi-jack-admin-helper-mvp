from __future__ import annotations

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


ASSET_VERSION = "admin-ia-v3"


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


def _client_rows(
    conn: sqlite3.Connection,
    *,
    query: str,
    page: int,
    page_size: int = 50,
) -> tuple[list[sqlite3.Row], int]:
    clean = " ".join(str(query or "").split())
    where = "WHERE IFNULL(c.client_status,'')<>'deleted'"
    params: list[Any] = []
    if clean:
        like = f"%{clean}%"
        where += """
          AND (
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
        params.extend([clean, like, like, like, like, like, like, like])

    total = int(
        conn.execute(f"SELECT COUNT(*) FROM clients c {where}", params).fetchone()[0]
    )
    rating_expr = "0"
    if _table_exists(conn, "hi_jack_rating_entries"):
        rating_expr = (
            "COALESCE((SELECT SUM(hre.rating_points) FROM hi_jack_rating_entries hre "
            "WHERE hre.client_id=c.id),0)"
        )
    offset = (max(1, page) - 1) * page_size
    rows = conn.execute(
        f"""
        SELECT c.*,
               COALESCE((SELECT SUM(jl.amount) FROM jackcoin_ledger jl
                         WHERE jl.client_id=c.id),0) AS jc_balance,
               COALESCE((SELECT SUM(CASE WHEN jl.amount>0 THEN jl.amount ELSE 0 END)
                         FROM jackcoin_ledger jl WHERE jl.client_id=c.id),0) AS jc_earned,
               (SELECT MAX(jl.created_at) FROM jackcoin_ledger jl
                WHERE jl.client_id=c.id) AS jc_last_at,
               {rating_expr} AS hijack_rating
        FROM clients c
        {where}
        ORDER BY COALESCE(c.updated_at,c.created_at) DESC, c.id DESC
        LIMIT ? OFFSET ?
        """,
        (*params, page_size, offset),
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
    ):
        _require_master(request)
        with connect(settings.db_path) as conn:
            rows, total = _client_rows(conn, query=q, page=page)
        return templates.TemplateResponse(
            request,
            "admin_clients_workspace.html",
            _context(
                request,
                clients=rows,
                q=q,
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
