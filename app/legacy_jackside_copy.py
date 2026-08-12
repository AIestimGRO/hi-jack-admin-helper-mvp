from __future__ import annotations

import sqlite3
from datetime import date, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.db import connect, transaction
from app.product_shell import _check_csrf, _require_master
from app.services.jackside_issues import (
    _copy_campaign_questions,
    create_issue,
    ensure_issue_campaign,
    refresh_issue_question_counts,
)


def list_legacy_daily_campaigns(
    conn: sqlite3.Connection, *, limit: int = 100
) -> list[dict[str, object]]:
    """Return daily_414 campaigns that predate the JACKSIDE issue registry.

    Legacy campaigns remain immutable history. They are only offered as sources
    for creating a brand-new JACKSIDE issue with a new campaign code.
    """
    rows = conn.execute(
        """
        SELECT
            qc.id,
            qc.code,
            qc.title,
            qc.active_from,
            qc.active_until,
            qc.archived_at,
            qc.is_active,
            qc.created_at,
            (
                SELECT COUNT(*)
                FROM quiz_questions qq
                WHERE qq.campaign_code=qc.code
                  AND IFNULL(qq.is_active, 1)=1
                  AND IFNULL(qq.game_round, 'main')='main'
            ) AS main_question_count,
            (
                SELECT COUNT(*)
                FROM quiz_questions qq
                WHERE qq.campaign_code=qc.code
                  AND IFNULL(qq.is_active, 1)=1
                  AND qq.game_round='final'
            ) AS final_question_count,
            (
                SELECT COUNT(*)
                FROM quiz_submissions qs
                WHERE qs.campaign_code=qc.code
            ) AS submission_count
        FROM quiz_campaigns qc
        WHERE qc.campaign_type='daily_414'
          AND qc.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM jackside_issues ji
              WHERE ji.campaign_code=qc.code
          )
          AND EXISTS (
              SELECT 1
              FROM quiz_questions qq
              WHERE qq.campaign_code=qc.code
                AND IFNULL(qq.is_active, 1)=1
          )
        ORDER BY COALESCE(qc.active_from, qc.created_at) DESC, qc.id DESC
        LIMIT ?
        """,
        (max(1, min(200, int(limit))),),
    ).fetchall()
    return [dict(row) for row in rows]


def copy_legacy_campaign_to_issue(
    conn: sqlite3.Connection,
    *,
    source_campaign_id: int,
    issue_date_value: date,
    starts_at: datetime,
    admin_id: int | None = None,
    timezone_name: str = "Europe/Moscow",
) -> sqlite3.Row:
    """Create a new JACKSIDE issue from an unlinked legacy daily_414 campaign."""
    source = conn.execute(
        """
        SELECT *
        FROM quiz_campaigns qc
        WHERE qc.id=?
          AND qc.campaign_type='daily_414'
          AND qc.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM jackside_issues ji
              WHERE ji.campaign_code=qc.code
          )
        """,
        (int(source_campaign_id),),
    ).fetchone()
    if not source:
        raise ValueError("legacy_campaign_not_found")

    question_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM quiz_questions
            WHERE campaign_code=? AND IFNULL(is_active, 1)=1
            """,
            (source["code"],),
        ).fetchone()[0]
    )
    if question_count <= 0:
        raise ValueError("legacy_campaign_has_no_questions")

    created = create_issue(
        conn,
        issue_date_value=issue_date_value,
        starts_at=starts_at,
        admin_id=admin_id,
        final_prize_type=str(source["final_prize_type"] or "none"),
        final_prize_catalog_reward_id=source["final_prize_catalog_reward_id"],
        final_prize_jackcoin_amount=int(source["final_prize_jackcoin_amount"] or 0),
        final_question_time_seconds=int(source["final_question_time_seconds"] or 30),
        timezone_name=timezone_name,
    )
    created = ensure_issue_campaign(
        conn,
        issue=created,
        timezone_name=timezone_name,
    )
    _copy_campaign_questions(
        conn,
        source_campaign=str(source["code"]),
        target_campaign=str(created["campaign_code"]),
    )
    return refresh_issue_question_counts(conn, int(created["id"]))


def _issues_redirect(message: str, *, error: bool = False) -> RedirectResponse:
    key = "error" if error else "ok"
    return RedirectResponse(
        f"/master/jackside-issues?{urlencode({key: message})}",
        status_code=303,
    )


def install_legacy_jackside_copy(app: FastAPI) -> FastAPI:
    if getattr(app.state, "legacy_jackside_copy_installed", False):
        return app
    app.state.legacy_jackside_copy_installed = True
    settings = app.state.settings

    @app.get("/api/master/jackside-issues/legacy-sources")
    async def legacy_jackside_sources(request: Request):
        _require_master(request)
        with connect(settings.db_path) as conn:
            rows = list_legacy_daily_campaigns(conn)
        return JSONResponse({"sources": rows})

    @app.post("/api/master/jackside-issues/copy-legacy")
    async def copy_legacy_jackside_issue(
        request: Request,
        source_campaign_id: int = Form(...),
        issue_date: str = Form(...),
        starts_at: str = Form(...),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        try:
            day = date.fromisoformat(issue_date)
            starts_local = datetime.fromisoformat(starts_at)
        except ValueError:
            return _issues_redirect("Некорректная дата или время", error=True)

        club_tz = ZoneInfo(settings.timezone_name)
        if starts_local.tzinfo is None:
            starts_local = starts_local.replace(tzinfo=club_tz)
        else:
            starts_local = starts_local.astimezone(club_tz)
        if starts_local.date() != day:
            return _issues_redirect(
                "Дата старта должна совпадать с датой нового выпуска",
                error=True,
            )

        try:
            with transaction(settings.db_path) as conn:
                source = conn.execute(
                    "SELECT title FROM quiz_campaigns WHERE id=?",
                    (int(source_campaign_id),),
                ).fetchone()
                issue = copy_legacy_campaign_to_issue(
                    conn,
                    source_campaign_id=source_campaign_id,
                    issue_date_value=day,
                    starts_at=starts_local,
                    admin_id=request.session.get("admin_id"),
                    timezone_name=settings.timezone_name,
                )
        except ValueError as exc:
            messages = {
                "legacy_campaign_not_found": "Старый квиз не найден или уже связан с выпуском JACKSIDE",
                "legacy_campaign_has_no_questions": "В старом квизе нет активных вопросов",
                "issue_date_exists": "На эту дату выпуск JACKSIDE уже существует",
                "issue_end_before_start": "Время старта выходит за дату выпуска",
            }
            return _issues_redirect(messages.get(str(exc), str(exc)), error=True)

        source_title = str(source["title"] or "") if source else "старого квиза"
        return _issues_redirect(
            f"Создан новый выпуск {issue['issue_date']} на основе «{source_title}»"
        )

    return app


__all__ = [
    "copy_legacy_campaign_to_issue",
    "install_legacy_jackside_copy",
    "list_legacy_daily_campaigns",
]
