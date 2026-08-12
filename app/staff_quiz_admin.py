from __future__ import annotations

import re
import sqlite3
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.admin_access_control import ACCESS_MASTER, ACCESS_QUIZ_MANAGER
from app.config import BASE_DIR
from app.db import connect, transaction
from app.product_shell import _check_csrf
from app.services.auth import audit


CAMPAIGN_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")


def _require_quiz_admin(request: Request) -> None:
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401, detail="authentication_required")
    role = str(request.session.get("admin_access_role") or "")
    if role not in {ACCESS_MASTER, ACCESS_QUIZ_MANAGER}:
        raise HTTPException(status_code=403, detail="Недостаточно прав")


def _redirect(message: str, *, error: bool = False) -> RedirectResponse:
    return RedirectResponse(
        f"/staff/quizzes?{urlencode({'error' if error else 'ok': message})}",
        status_code=303,
    )


def _validate_campaign_values(
    *,
    title: str,
    pass_score: int,
    question_time_limit_seconds: int,
    quiz_time_limit_seconds: int,
    max_attempts: int,
    bonus_amount: int,
) -> tuple[str, int, int, int, int, int]:
    clean_title = " ".join(str(title or "").split())[:80]
    if not clean_title:
        raise ValueError("Укажите название квиза")
    if not 0 <= int(pass_score) <= 10000:
        raise ValueError("Некорректный порог баллов")
    if not 0 <= int(question_time_limit_seconds) <= 3600:
        raise ValueError("Некорректное время вопроса")
    if not 0 <= int(quiz_time_limit_seconds) <= 7200:
        raise ValueError("Некорректное время квиза")
    if not 1 <= int(max_attempts) <= 100:
        raise ValueError("Некорректное количество попыток")
    if not 0 <= int(bonus_amount) <= 1000:
        raise ValueError("Некорректное количество бонусов")
    return (
        clean_title,
        int(pass_score),
        int(question_time_limit_seconds),
        int(quiz_time_limit_seconds),
        int(max_attempts),
        int(bonus_amount),
    )


def install_staff_quiz_admin(app: FastAPI) -> FastAPI:
    if getattr(app.state, "staff_quiz_admin_installed", False):
        return app
    app.state.staff_quiz_admin_installed = True
    settings = app.state.settings
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

    @app.get("/staff/quizzes", response_class=HTMLResponse)
    async def staff_quizzes_page(
        request: Request,
        ok: str = "",
        error: str = "",
    ):
        _require_quiz_admin(request)
        with connect(settings.db_path) as conn:
            campaigns = conn.execute(
                """
                SELECT qc.*,pt.title AS bonus_title
                FROM quiz_campaigns qc
                LEFT JOIN preference_types pt ON pt.code=qc.bonus_preference_code
                WHERE qc.deleted_at IS NULL AND qc.archived_at IS NULL
                ORDER BY CASE WHEN qc.campaign_type='daily_414' THEN 1 ELSE 0 END,
                         qc.updated_at DESC,qc.id DESC
                """
            ).fetchall()
            preferences = conn.execute(
                """
                SELECT code,title FROM preference_types
                WHERE is_active=1 AND kind='counter'
                ORDER BY position,title
                """
            ).fetchall()
        return templates.TemplateResponse(
            request,
            "staff_quizzes.html",
            {
                "request": request,
                "campaigns": campaigns,
                "preferences": preferences,
                "ok": ok,
                "error": error,
                "csrf_token": request.session.get("csrf", ""),
                "admin_name": request.session.get("admin_name", "Администратор"),
                "admin_role": request.session.get("admin_role", "admin"),
                "asset_version": "staff-quizzes-v1",
            },
        )

    @app.post("/api/staff/quizzes/create")
    async def staff_quizzes_create(
        request: Request,
        code: str = Form(...),
        title: str = Form(...),
        pass_score: int = Form(0),
        question_time_limit_seconds: int = Form(20),
        quiz_time_limit_seconds: int = Form(120),
        max_attempts: int = Form(3),
        verification_required: bool = Form(False),
        bonus_preference_code: str = Form(""),
        bonus_amount: int = Form(0),
        csrf_token: str = Form(...),
    ):
        _require_quiz_admin(request)
        _check_csrf(request, csrf_token)
        clean_code = str(code or "").strip().lower()
        if not CAMPAIGN_CODE_RE.fullmatch(clean_code):
            return _redirect("Код квиза содержит недопустимые символы", error=True)
        try:
            values = _validate_campaign_values(
                title=title,
                pass_score=pass_score,
                question_time_limit_seconds=question_time_limit_seconds,
                quiz_time_limit_seconds=quiz_time_limit_seconds,
                max_attempts=max_attempts,
                bonus_amount=bonus_amount,
            )
            clean_title, pass_score, q_seconds, quiz_seconds, attempts, bonus_amount = values
            bonus_code = str(bonus_preference_code or "").strip() or None
            with transaction(settings.db_path) as conn:
                if bonus_code and not conn.execute(
                    "SELECT 1 FROM preference_types WHERE code=? AND kind='counter' AND is_active=1",
                    (bonus_code,),
                ).fetchone():
                    raise ValueError("Выбранная награда недоступна")
                cursor = conn.execute(
                    """
                    INSERT INTO quiz_campaigns(
                        code,title,campaign_type,pass_score,question_time_limit_seconds,
                        quiz_time_limit_seconds,max_attempts,verification_required,
                        bonus_preference_code,bonus_amount,reward_delivery_mode
                    ) VALUES (?,?,'classic',?,?,?,?,?,?,?,'automatic')
                    """,
                    (
                        clean_code,
                        clean_title,
                        pass_score,
                        q_seconds,
                        quiz_seconds,
                        attempts,
                        int(bool(verification_required)),
                        bonus_code,
                        bonus_amount,
                    ),
                )
                campaign_id = int(cursor.lastrowid)
                audit(
                    conn,
                    admin_id=int(request.session.get("admin_id")),
                    admin_name=str(request.session.get("admin_name") or "admin"),
                    action="quiz_campaign_created_by_quiz_admin",
                    entity_type="quiz_campaign",
                    entity_id=campaign_id,
                    details={"code": clean_code, "title": clean_title},
                )
        except sqlite3.IntegrityError:
            return _redirect("Кампания с таким кодом уже существует", error=True)
        except ValueError as exc:
            return _redirect(str(exc), error=True)
        return RedirectResponse(f"/master/quiz-builder/{campaign_id}", status_code=303)

    @app.post("/api/staff/quizzes/{campaign_id}/update")
    async def staff_quizzes_update(
        request: Request,
        campaign_id: int,
        title: str = Form(...),
        pass_score: int = Form(0),
        question_time_limit_seconds: int = Form(20),
        quiz_time_limit_seconds: int = Form(120),
        max_attempts: int = Form(3),
        verification_required: bool = Form(False),
        bonus_preference_code: str = Form(""),
        bonus_amount: int = Form(0),
        csrf_token: str = Form(...),
    ):
        _require_quiz_admin(request)
        _check_csrf(request, csrf_token)
        try:
            values = _validate_campaign_values(
                title=title,
                pass_score=pass_score,
                question_time_limit_seconds=question_time_limit_seconds,
                quiz_time_limit_seconds=quiz_time_limit_seconds,
                max_attempts=max_attempts,
                bonus_amount=bonus_amount,
            )
            clean_title, pass_score, q_seconds, quiz_seconds, attempts, bonus_amount = values
            bonus_code = str(bonus_preference_code or "").strip() or None
            with transaction(settings.db_path) as conn:
                campaign = conn.execute(
                    "SELECT id,campaign_type FROM quiz_campaigns WHERE id=? AND deleted_at IS NULL",
                    (campaign_id,),
                ).fetchone()
                if not campaign:
                    raise ValueError("Квиз не найден")
                if str(campaign["campaign_type"]) != "classic":
                    raise ValueError("Настройки JACKSIDE меняются через мастер выпусков")
                if bonus_code and not conn.execute(
                    "SELECT 1 FROM preference_types WHERE code=? AND kind='counter' AND is_active=1",
                    (bonus_code,),
                ).fetchone():
                    raise ValueError("Выбранная награда недоступна")
                conn.execute(
                    """
                    UPDATE quiz_campaigns SET
                        title=?,pass_score=?,question_time_limit_seconds=?,
                        quiz_time_limit_seconds=?,max_attempts=?,verification_required=?,
                        bonus_preference_code=?,bonus_amount=?,updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (
                        clean_title,
                        pass_score,
                        q_seconds,
                        quiz_seconds,
                        attempts,
                        int(bool(verification_required)),
                        bonus_code,
                        bonus_amount,
                        campaign_id,
                    ),
                )
                audit(
                    conn,
                    admin_id=int(request.session.get("admin_id")),
                    admin_name=str(request.session.get("admin_name") or "admin"),
                    action="quiz_campaign_updated_by_quiz_admin",
                    entity_type="quiz_campaign",
                    entity_id=campaign_id,
                    details={},
                )
        except ValueError as exc:
            return _redirect(str(exc), error=True)
        return _redirect("Настройки квиза сохранены")

    @app.post("/api/staff/quizzes/{campaign_id}/toggle")
    async def staff_quizzes_toggle(
        request: Request,
        campaign_id: int,
        csrf_token: str = Form(...),
    ):
        _require_quiz_admin(request)
        _check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                campaign = conn.execute(
                    "SELECT id,campaign_type,is_active FROM quiz_campaigns WHERE id=? AND deleted_at IS NULL",
                    (campaign_id,),
                ).fetchone()
                if not campaign:
                    raise ValueError("Квиз не найден")
                if str(campaign["campaign_type"]) != "classic":
                    raise ValueError("Состояние JACKSIDE меняется через мастер выпусков")
                new_state = 0 if int(campaign["is_active"] or 0) else 1
                conn.execute(
                    "UPDATE quiz_campaigns SET is_active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (new_state, campaign_id),
                )
                audit(
                    conn,
                    admin_id=int(request.session.get("admin_id")),
                    admin_name=str(request.session.get("admin_name") or "admin"),
                    action="quiz_campaign_toggled_by_quiz_admin",
                    entity_type="quiz_campaign",
                    entity_id=campaign_id,
                    details={"is_active": bool(new_state)},
                )
        except ValueError as exc:
            return _redirect(str(exc), error=True)
        return _redirect("Состояние квиза обновлено")

    return app


__all__ = ["install_staff_quiz_admin"]
