from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

import qrcode
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.concurrency import run_in_threadpool

from app.config import BASE_DIR, Settings
from app.db import QUIZ_CAMPAIGNS, connect, init_db, transaction
from app.services.clients import ensure_preferences
from app.services.auth import audit, authenticate, bootstrap_master, hash_pin, validate_username
from app.services.import_clients import FIELD_LABELS, detect_mapping, import_rows, read_tabular
from app.services.phone import display_phone, normalize_phone
from app.services.preferences import change_counter, set_percent
from app.services.quiz import (
    CAMPAIGN_RE,
    answer_summary,
    attempt_token_hash,
    ip_fingerprint,
    load_builder_questions,
    load_db_questions,
    load_final_questions,
    normalize_campaign,
    normalize_text_answer,
    parse_quick_questions,
    public_questions,
    score_answers,
    seed_questions_from_json,
    validate_answers,
)
from app.services.quiz_identity import (
    email_code_hash,
    find_or_create_quiz_client,
    generate_email_code,
    identity_json,
    normalize_email,
    normalize_username,
)
from app.services.quiz_device import (
    DEVICE_COOKIE_NAME,
    DEVICE_MAX_AGE_SECONDS,
    forget_device,
    issue_or_refresh_device,
    remembered_client,
    remembered_display_name,
)
from app.services.daily_414 import (
    DAILY_414_ENTRY_WINDOW_SECONDS,
    DAILY_414_FINAL_QUESTION_SECONDS,
    DAILY_414_FINAL_TABLE_SIZE,
    DAILY_414_QUESTION_COUNT,
    DAILY_414_TIME_LIMIT_SECONDS,
    award_daily_jackcoin,
    elapsed_milliseconds,
    final_table_candidate_eligible,
    final_table_starts_at as daily_final_table_starts_at,
    issue_date as daily_issue_date,
    public_daily_questions,
    rank_final_candidates,
    validate_daily_questions,
)
from app.services.daily_414_final import (
    ensure_final_table,
    final_table_needs_reconcile,
    question_window as final_question_window,
    reconcile_final_table,
)
from app.services.quiz_mail import send_member_email_code, send_quiz_email_code
from app.services.member_accounts import (
    MEMBER_COOKIE_NAME,
    active_legal_documents,
    authenticate_account,
    authenticated_member,
    consent_payload,
    generate_email_code as generate_member_email_code,
    hash_password,
    issue_session as issue_member_session,
    jackcoin_balance,
    member_code_hash,
    revoke_session as revoke_member_session,
)
from app.services.quiz_retention import cleanup_quiz_data
from app.services.quiz_rewards import issue_referral_reward, issue_reward, redeem_reward, render_campaign_text
from app.services.telegram_oidc import authorization_url, exchange_telegram_code, new_pkce
from app.services.vault import (
    attach_final_table_reward,
    cancel_reward as cancel_vault_reward,
    create_catalog_reward,
    expire_rewards as expire_vault_rewards,
    purchase_reward as purchase_vault_reward,
    purchase_token as vault_purchase_token,
    redeem_reward as redeem_vault_reward,
    update_catalog_reward,
    valid_purchase_token,
)


BUILT_IN_CAMPAIGN_CODES = frozenset(code for code, _title in QUIZ_CAMPAIGNS)
CAMPAIGN_HISTORY_TABLES = (
    "quiz_attempts",
    "quiz_submissions",
    "client_quiz_campaigns",
    "quiz_participation_summary",
    "quiz_participation_versions",
    "quiz_reward_codes",
    "quiz_referral_codes",
    "quiz_referrals",
    "quiz_reward_events",
    "quiz_email_codes",
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    campaign_timezone = ZoneInfo(settings.timezone_name)
    quiz_media_dir = Path(settings.db_path).parent / "quiz-media"
    quiz_media_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.validate()
        init_db(settings.db_path)
        with transaction(settings.db_path) as conn:
            bootstrap_master(
                conn,
                username=settings.master_login,
                display_name=settings.admin_name,
                pin=settings.admin_pin,
            )
            seed_questions_from_json(conn, BASE_DIR / "data" / "quiz_questions.json")
            cleanup_quiz_data(
                conn,
                detail_days=settings.quiz_detail_retention_days,
                reward_days=settings.reward_retention_days,
                action_log_days=settings.action_log_retention_days,
            )
        (BASE_DIR / "data" / "uploads").mkdir(parents=True, exist_ok=True)
        yield

    app = FastAPI(title="Hi Jack Club Admin Helper", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.next_quiz_cleanup_at = datetime.now(timezone.utc) + timedelta(days=1)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key or "development-secret-key-change-before-use",
        session_cookie="hjc_admin_session",
        max_age=settings.session_hours * 3600,
        same_site="lax",
        https_only=settings.secure_cookie,
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
    app.mount("/quiz-media", StaticFiles(directory=quiz_media_dir), name="quiz-media")
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
    templates.env.globals["display_phone"] = display_phone

    def display_datetime(value: Any, with_seconds: bool = False) -> str:
        if value is None or str(value).strip() == "":
            return "—"
        raw_value = str(value).strip()
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            return raw_value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        local_value = parsed.astimezone(campaign_timezone)
        return local_value.strftime("%d.%m.%Y %H:%M:%S" if with_seconds else "%d.%m.%Y %H:%M")

    templates.env.globals["display_datetime"] = display_datetime

    static_dir = BASE_DIR / "app" / "static"
    asset_digest = hashlib.sha256()
    for asset_path in sorted(path for path in static_dir.rglob("*") if path.is_file()):
        asset_digest.update(asset_path.relative_to(static_dir).as_posix().encode("utf-8"))
        asset_digest.update(b"\0")
        asset_digest.update(asset_path.read_bytes())
    templates.env.globals["asset_version"] = asset_digest.hexdigest()[:12]

    @app.middleware("http")
    async def private_cache_control(request: Request, call_next):
        now = datetime.now(timezone.utc)
        if now >= app.state.next_quiz_cleanup_at:
            app.state.next_quiz_cleanup_at = now + timedelta(days=1)
            try:
                with transaction(settings.db_path) as conn:
                    cleanup_quiz_data(
                        conn, detail_days=settings.quiz_detail_retention_days,
                        reward_days=settings.reward_retention_days,
                        action_log_days=settings.action_log_retention_days,
                    )
            except sqlite3.Error:
                pass
        response = await call_next(request)
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "private, no-store")
        return response

    def csrf(request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(32)
            request.session["csrf"] = token
        return token

    def context(request: Request, **values: Any) -> dict[str, Any]:
        return {
            "request": request,
            "csrf_token": csrf(request),
            "admin_name": request.session.get("admin_name", settings.admin_name),
            "admin_role": request.session.get("admin_role", ""),
            **values,
        }

    def authenticated(request: Request) -> bool:
        return bool(request.session.get("authenticated"))

    def require_auth(request: Request, api: bool = False) -> None:
        valid = authenticated(request)
        if valid:
            admin_id = request.session.get("admin_id")
            with connect(settings.db_path) as conn:
                current_admin = conn.execute(
                    "SELECT display_name, role, session_version FROM admins WHERE id = ? AND is_active = 1",
                    (admin_id,),
                ).fetchone()
            valid = bool(
                current_admin
                and current_admin["session_version"] == request.session.get("admin_session_version")
            )
            if valid:
                request.session["admin_name"] = current_admin["display_name"]
                request.session["admin_role"] = current_admin["role"]
        if not valid:
            request.session.clear()
            if api:
                raise HTTPException(status_code=401, detail="authentication_required")
            raise HTTPException(status_code=303, detail="login_required", headers={"Location": "/login"})

    def require_master(request: Request, api: bool = False) -> None:
        require_auth(request, api=api)
        if request.session.get("admin_role") != "master_admin":
            raise HTTPException(status_code=403, detail="Доступ только для мастер-администратора")

    def check_csrf(request: Request, token: str) -> None:
        expected = request.session.get("csrf", "")
        if not expected or not hmac.compare_digest(expected, token):
            raise HTTPException(status_code=403, detail="invalid_csrf_token")

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        if exc.status_code == 303 and exc.headers and exc.headers.get("Location"):
            return RedirectResponse(exc.headers["Location"], status_code=303)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        return templates.TemplateResponse(request, "error.html", context(request, status=exc.status_code, message=exc.detail), status_code=exc.status_code)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if authenticated(request):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", context(request, error=None))

    @app.post("/login")
    async def login(request: Request, username: str = Form(...), pin: str = Form(...), csrf_token: str = Form(...)):
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            admin = authenticate(conn, username, pin)
        if not admin:
            return templates.TemplateResponse(request, "login.html", context(request, error="Неверный логин или PIN"), status_code=401)
        request.session.clear()
        request.session.update({
            "authenticated": True,
            "admin_id": int(admin["id"]),
            "admin_name": admin["display_name"],
            "admin_role": admin["role"],
            "admin_session_version": admin["session_version"],
            "csrf": secrets.token_urlsafe(32),
        })
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request, csrf_token: str = Form(...)):
        require_auth(request)
        check_csrf(request, csrf_token)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if request.url.hostname and request.url.hostname.lower().startswith("quiz."):
            return RedirectResponse("/quiz", status_code=302)
        require_auth(request)
        with connect(settings.db_path) as conn:
            counts = {
                "clients": conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0],
                "operations": conn.execute("SELECT COUNT(*) FROM preference_log").fetchone()[0],
                "imports": conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0],
            }
            recent = conn.execute(
                """
                SELECT pl.*, c.first_name, c.nickname, pt.title, pt.kind
                FROM preference_log pl
                JOIN clients c ON c.id = pl.client_id
                JOIN preference_types pt ON pt.id = pl.preference_type_id
                ORDER BY pl.id DESC LIMIT 8
                """
            ).fetchall()
        return templates.TemplateResponse(request, "dashboard.html", context(request, counts=counts, recent=recent))

    def normalize_campaign_datetime(value: str) -> str | None:
        value = value.strip()
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Проверьте дату и время активности") from exc
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(campaign_timezone).replace(tzinfo=None)
        return parsed.replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")

    def validate_campaign_period(active_from: str, active_until: str) -> tuple[str | None, str | None]:
        start = normalize_campaign_datetime(active_from)
        end = normalize_campaign_datetime(active_until)
        if start and end and datetime.fromisoformat(end) <= datetime.fromisoformat(start):
            raise ValueError("Окончание активности должно быть позже начала")
        return start, end

    def default_daily_414_period(
        active_from: str | None, active_until: str | None
    ) -> tuple[str, str]:
        if active_from:
            start = datetime.fromisoformat(active_from)
        else:
            now = datetime.now(campaign_timezone).replace(tzinfo=None)
            start = now.replace(hour=18, minute=14, second=0, microsecond=0)
            if start <= now:
                start += timedelta(days=1)
        if active_until:
            end = datetime.fromisoformat(active_until)
        else:
            end = (start + timedelta(days=1)).replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        if end <= start:
            raise ValueError("Окончание выпуска 4:14 должно быть позже старта")
        return (
            start.strftime("%Y-%m-%dT%H:%M"),
            end.strftime("%Y-%m-%dT%H:%M"),
        )

    def campaign_schedule_state(campaign_row: sqlite3.Row | dict[str, Any]) -> str:
        if campaign_row["archived_at"] or not campaign_row["is_active"]:
            return "disabled"
        now = datetime.now(campaign_timezone).replace(tzinfo=None)
        start = datetime.fromisoformat(campaign_row["active_from"]) if campaign_row["active_from"] else None
        end = datetime.fromisoformat(campaign_row["active_until"]) if campaign_row["active_until"] else None
        if start and now < start:
            return "upcoming"
        if end and now >= end:
            return "ended"
        return "active"

    def campaign_has_history(conn: sqlite3.Connection, campaign_code: str) -> bool:
        for table in CAMPAIGN_HISTORY_TABLES:
            if conn.execute(
                f"SELECT 1 FROM {table} WHERE campaign_code=? LIMIT 1",
                (campaign_code,),
            ).fetchone():
                return True
        return bool(
            conn.execute(
                """
                SELECT 1 FROM clients
                WHERE acquisition_campaign_code=? LIMIT 1
                """,
                (campaign_code,),
            ).fetchone()
        )

    def format_campaign_datetime(value: str) -> str:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y в %H:%M")

    def quiz_campaign_row(code: str):
        with connect(settings.db_path) as conn:
            row = conn.execute(
                """
                SELECT qc.*, pt.title AS bonus_title
                FROM quiz_campaigns qc
                LEFT JOIN preference_types pt ON pt.code = qc.bonus_preference_code
                WHERE qc.code = ?
                """,
                (code,),
            ).fetchone()
        if not row or campaign_schedule_state(row) == "disabled":
            raise HTTPException(status_code=404, detail="Кампания квиза не найдена или отключена")
        if row["campaign_type"] == "daily_414":
            if not settings.member_portal_enabled:
                raise HTTPException(status_code=404, detail="Режим 4:14 пока отключён")
        return row

    def quiz_campaign_or_404(code: str):
        row = quiz_campaign_row(code)
        state = campaign_schedule_state(row)
        if state == "upcoming":
            raise HTTPException(status_code=403, detail=f"Квиз начнётся {format_campaign_datetime(row['active_from'])}")
        if state == "ended":
            raise HTTPException(status_code=410, detail=f"Квиз завершён {format_campaign_datetime(row['active_until'])}")
        return row

    def quiz_campaign_for_meta(code: str):
        """Allow reading welcome/meta while the campaign is still upcoming."""
        row = quiz_campaign_row(code)
        state = campaign_schedule_state(row)
        if state == "ended":
            raise HTTPException(
                status_code=410,
                detail=f"Квиз завершён {format_campaign_datetime(row['active_until'])}",
            )
        return row

    def quiz_campaign_for_final(code: str):
        """Final table continues after the main quiz window may already be ended."""
        row = quiz_campaign_row(code)
        if row["campaign_type"] != "daily_414":
            raise HTTPException(
                status_code=404,
                detail="Финальный стол доступен только в JACKSIDE 4:14",
            )
        return row

    def campaign_datetime_iso(value: str | None) -> str:
        if not value:
            return ""
        return datetime.fromisoformat(value).replace(tzinfo=campaign_timezone).isoformat()

    def campaign_background(code: str) -> str:
        with connect(settings.db_path) as conn:
            row = conn.execute(
                """
                SELECT background_image FROM quiz_sections
                WHERE campaign_code=? AND background_image IS NOT NULL AND background_image <> ''
                ORDER BY position, id LIMIT 1
                """,
                (code,),
            ).fetchone()
        return str(row["background_image"]) if row else ""

    def member_portal_or_404() -> None:
        if not settings.member_portal_enabled:
            raise HTTPException(status_code=404, detail="Личный кабинет пока недоступен")

    def member_cookie(request: Request) -> str:
        return str(request.cookies.get(MEMBER_COOKIE_NAME, "")).strip()

    def member_ip_hash(request: Request) -> str:
        client_ip = request.client.host if request.client else "unknown"
        return ip_fingerprint(settings.secret_key, client_ip)

    def set_member_cookie(response: Response, token: str) -> None:
        max_age = settings.member_session_days * 24 * 3600
        response.set_cookie(
            MEMBER_COOKIE_NAME,
            token,
            max_age=max_age,
            path="/",
            secure=settings.secure_cookie,
            httponly=True,
            samesite="lax",
        )

    def clear_member_cookie(response: Response) -> None:
        response.delete_cookie(
            MEMBER_COOKIE_NAME,
            path="/",
            secure=settings.secure_cookie,
            httponly=True,
            samesite="lax",
        )

    def current_member(request: Request, *, required: bool = False):
        token = member_cookie(request)
        member = None
        if token:
            with transaction(settings.db_path) as conn:
                member = authenticated_member(
                    conn,
                    secret_key=settings.secret_key,
                    token=token,
                    touch=True,
                )
        if required and not member:
            raise HTTPException(
                status_code=303,
                detail="member_login_required",
                headers={"Location": "/account/login"},
            )
        return member

    def daily_member(
        request: Request,
        campaign_row: sqlite3.Row | dict[str, Any],
        *,
        required: bool = True,
    ):
        if campaign_row["campaign_type"] != "daily_414":
            return None
        member = current_member(request)
        if required and not member:
            raise HTTPException(
                status_code=401,
                detail="Войдите в аккаунт JACKSIDE, чтобы участвовать в 4:14",
            )
        if member and missing_member_documents(int(member["id"])):
            raise HTTPException(
                status_code=403,
                detail="Примите актуальные условия участия в личном кабинете",
            )
        return member

    def member_context(request: Request, **values: Any) -> dict[str, Any]:
        return {
            "request": request,
            "csrf_token": csrf(request),
            "member": current_member(request),
            "member_portal_enabled": settings.member_portal_enabled,
            **values,
        }

    def member_redirect(
        path: str, message: str = "", *, error: bool = False
    ) -> RedirectResponse:
        if message:
            parameter = "error" if error else "ok"
            path = f"{path}?{urlencode({parameter: message})}"
        return RedirectResponse(path, status_code=303)

    def safe_member_next(value: str) -> str:
        candidate = str(value or "").strip()
        parsed = urlsplit(candidate)
        if (
            not candidate.startswith("/")
            or candidate.startswith("//")
            or parsed.scheme
            or parsed.netloc
        ):
            return ""
        return candidate[:1000]

    def remember_member_next(request: Request, value: str) -> str:
        candidate = safe_member_next(value)
        if candidate:
            request.session["member_next"] = candidate
        return candidate

    def member_continue_path(request: Request, *, consume: bool = False) -> str:
        value = safe_member_next(str(request.session.get("member_next", "")))
        if consume:
            request.session.pop("member_next", None)
        return value or "/account"

    def member_registration_flow(request: Request) -> dict[str, Any]:
        flow = request.session.get("member_registration_flow")
        if not isinstance(flow, dict):
            flow = {"accepted": {}}
            request.session["member_registration_flow"] = flow
        if not isinstance(flow.get("accepted"), dict):
            flow["accepted"] = {}
        return flow

    def member_telegram_redirect_uri(request: Request) -> str:
        return str(request.url_for("member_telegram_callback"))

    def missing_member_documents(account_id: int) -> list[sqlite3.Row]:
        with connect(settings.db_path) as conn:
            return conn.execute(
                """
                SELECT ld.* FROM legal_documents ld
                WHERE ld.is_active=1
                  AND NOT EXISTS (
                    SELECT 1 FROM member_consents mc
                    WHERE mc.account_id=? AND mc.document_code=ld.code
                      AND mc.document_version=ld.version
                  )
                ORDER BY CASE ld.code
                    WHEN 'privacy' THEN 1 WHEN 'rewards' THEN 2 ELSE 99 END, ld.id
                """,
                (account_id,),
            ).fetchall()

    @app.get("/account/telegram/start")
    async def member_telegram_start(request: Request):
        member_portal_or_404()
        member = current_member(request)
        if not member:
            return member_redirect(
                "/account/login",
                "Сначала войдите в личный кабинет",
                error=True,
            )
        if not settings.telegram_client_id or not settings.telegram_client_secret:
            return member_redirect(
                "/account/telegram",
                "Подключение Telegram пока не настроено",
                error=True,
            )
        verifier, challenge = new_pkce()
        state = secrets.token_urlsafe(32)
        redirect_uri = member_telegram_redirect_uri(request)
        request.session["member_telegram_oauth"] = {
            "state": state,
            "verifier": verifier,
            "created_at": int(utc_now().timestamp()),
            "account_id": int(member["id"]),
        }
        return RedirectResponse(
            authorization_url(
                client_id=settings.telegram_client_id,
                redirect_uri=redirect_uri,
                state=state,
                code_challenge=challenge,
            ),
            status_code=302,
        )

    @app.get("/account/telegram/callback")
    async def member_telegram_callback(
        request: Request, code: str = "", state: str = "", error: str = ""
    ):
        member_portal_or_404()
        member = current_member(request)
        if not member:
            return member_redirect(
                "/account/login",
                "Сначала войдите в личный кабинет",
                error=True,
            )
        oauth = request.session.pop("member_telegram_oauth", None)
        if error or not code or not state or not isinstance(oauth, dict):
            return member_redirect(
                "/account/telegram",
                "Не удалось подключить Telegram. Попробуйте ещё раз",
                error=True,
            )
        if not hmac.compare_digest(state, str(oauth.get("state", ""))):
            return member_redirect(
                "/account/telegram",
                "Проверка Telegram не совпала с начатым входом. Попробуйте ещё раз",
                error=True,
            )
        if int(oauth.get("account_id", 0)) != int(member["id"]):
            return member_redirect(
                "/account/telegram",
                "Сессия подключения Telegram устарела. Попробуйте ещё раз",
                error=True,
            )
        if int(utc_now().timestamp()) - int(oauth.get("created_at", 0)) > 900:
            return member_redirect(
                "/account/telegram",
                "Подключение Telegram устарело. Попробуйте ещё раз",
                error=True,
            )
        redirect_uri = member_telegram_redirect_uri(request)
        try:
            claims = await run_in_threadpool(
                exchange_telegram_code,
                code=code,
                client_id=settings.telegram_client_id,
                client_secret=settings.telegram_client_secret,
                redirect_uri=redirect_uri,
                code_verifier=str(oauth["verifier"]),
            )
            telegram_user_id = str(claims.get("sub") or "").strip()
            username = normalize_username(claims.get("preferred_username"))
            if not telegram_user_id:
                raise ValueError("telegram_identity_required")
        except Exception:
            return member_redirect(
                "/account/telegram",
                "Не удалось получить данные Telegram. Попробуйте ещё раз",
                error=True,
            )
        try:
            with transaction(settings.db_path) as conn:
                linked = conn.execute(
                    """
                    SELECT id FROM clients
                    WHERE telegram_user_id=? AND id<>?
                    """,
                    (telegram_user_id[:80], int(member["client_id"])),
                ).fetchone()
                if linked:
                    raise ValueError(
                        "Этот Telegram уже связан с другим аккаунтом"
                    )
                conn.execute(
                    """
                    UPDATE clients
                    SET telegram_user_id=?, username=?, updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (
                        telegram_user_id[:80],
                        username,
                        int(member["client_id"]),
                    ),
                )
        except (ValueError, sqlite3.IntegrityError) as exc:
            message = (
                str(exc)
                if isinstance(exc, ValueError)
                else "Этот Telegram уже связан с другим аккаунтом"
            )
            return member_redirect("/account/telegram", message, error=True)
        return member_redirect(
            "/account/telegram", "Telegram успешно подключён"
        )

    @app.get("/account/register", response_class=HTMLResponse)
    async def member_register_page(
        request: Request, ok: str = "", error: str = "", next: str = ""
    ):
        member_portal_or_404()
        remember_member_next(request, next)
        if current_member(request):
            return RedirectResponse(
                member_continue_path(request, consume=True), status_code=303
            )
        flow = member_registration_flow(request)
        with connect(settings.db_path) as conn:
            documents = active_legal_documents(conn)
        accepted = flow["accepted"]
        pending_id = request.session.get("member_registration_code_id")
        pending_email = ""
        draft = request.session.get("member_registration_draft") or {}
        if "privacy" not in accepted:
            step = "privacy"
        elif "rewards" not in accepted:
            step = "rewards"
        elif pending_id:
            step = "verify"
            with connect(settings.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT email_normalized, used_at, expires_at, attempts_left
                    FROM member_email_codes
                    WHERE id=? AND purpose='register'
                    """,
                    (int(pending_id),),
                ).fetchone()
            if (
                not row
                or row["used_at"]
                or row["expires_at"] <= quiz_timestamp(utc_now())
                or int(row["attempts_left"]) <= 0
            ):
                request.session.pop("member_registration_code_id", None)
                step = "profile"
            else:
                pending_email = str(row["email_normalized"])
                draft = {**draft, "email": pending_email}
        else:
            step = "profile"
        return templates.TemplateResponse(
            request,
            "member_register.html",
            member_context(
                request,
                step=step,
                documents=documents,
                email_available=bool(settings.smtp_host and settings.smtp_from),
                email_code_minutes=settings.email_code_minutes,
                pending_email=pending_email,
                draft=draft,
                ok=ok,
                error=error,
            ),
        )

    @app.post("/account/register/consent")
    async def member_register_consent(
        request: Request,
        document_code: str = Form(...),
        accepted: bool = Form(False),
        csrf_token: str = Form(...),
    ):
        member_portal_or_404()
        check_csrf(request, csrf_token)
        if document_code not in {"privacy", "rewards"} or not accepted:
            return member_redirect(
                "/account/register",
                "Для продолжения необходимо принять условия",
                error=True,
            )
        flow = member_registration_flow(request)
        expected = "privacy" if "privacy" not in flow["accepted"] else "rewards"
        if document_code != expected:
            request.session.pop("member_registration_flow", None)
            return member_redirect(
                "/account/register",
                "Последовательность регистрации устарела. Начните заново",
                error=True,
            )
        with connect(settings.db_path) as conn:
            document = active_legal_documents(conn).get(document_code)
        if not document:
            return member_redirect(
                "/account/register", "Документ временно недоступен", error=True
            )
        flow["accepted"][document_code] = {
            "version": str(document["version"]),
            "accepted_at": quiz_timestamp(utc_now()),
        }
        request.session["member_registration_flow"] = flow
        return RedirectResponse("/account/register", status_code=303)

    @app.post("/account/register/request-code")
    async def member_register_request_code(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        password_confirmation: str = Form(...),
        phone: str = Form(...),
        first_name: str = Form(""),
        csrf_token: str = Form(...),
    ):
        member_portal_or_404()
        check_csrf(request, csrf_token)
        if not settings.smtp_host or not settings.smtp_from:
            return member_redirect(
                "/account/register",
                "Отправка писем пока не настроена",
                error=True,
            )
        try:
            try:
                normalized_email = normalize_email(email)
            except ValueError as exc:
                raise ValueError(
                    "Укажите корректный адрес электронной почты, например name@example.com"
                ) from exc
            phone_local = normalize_phone(phone)
            if not normalized_email:
                raise ValueError(
                    "Укажите адрес электронной почты — на него придёт код подтверждения"
                )
            if not phone_local:
                raise ValueError(
                    "Укажите российский номер телефона из 10 цифр, например +7 999 123-45-67"
                )
            flow = member_registration_flow(request)
            if password != password_confirmation:
                raise ValueError("Пароли не совпадают")
            password_hash = hash_password(password)
            with connect(settings.db_path) as conn:
                documents = active_legal_documents(conn)
                consents_json = consent_payload(documents, flow["accepted"])
                if conn.execute(
                    "SELECT 1 FROM member_accounts WHERE email_normalized=?",
                    (normalized_email,),
                ).fetchone():
                    raise ValueError("Аккаунт с такой почтой уже существует")
            code = generate_member_email_code()
            expires_at = quiz_timestamp(
                utc_now() + timedelta(minutes=settings.email_code_minutes)
            )
            payload = json.dumps(
                {
                    "email": normalized_email,
                    "password_hash": password_hash,
                    "phone": phone.strip()[:80],
                    "phone_local": phone_local,
                    "first_name": first_name.strip()[:100],
                    "consents": json.loads(consents_json),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            with transaction(settings.db_path) as conn:
                conn.execute(
                    """
                    UPDATE member_email_codes SET used_at=CURRENT_TIMESTAMP
                    WHERE email_normalized=? AND purpose='register' AND used_at IS NULL
                    """,
                    (normalized_email,),
                )
                cursor = conn.execute(
                    """
                    INSERT INTO member_email_codes(
                        email_normalized, purpose, code_hash, payload_json, expires_at
                    ) VALUES (?, 'register', ?, ?, ?)
                    """,
                    (
                        normalized_email,
                        member_code_hash(
                            settings.secret_key, "register", normalized_email, code
                        ),
                        payload,
                        expires_at,
                    ),
                )
                code_id = int(cursor.lastrowid)
            try:
                await run_in_threadpool(
                    send_member_email_code,
                    host=settings.smtp_host,
                    port=settings.smtp_port,
                    username=settings.smtp_username,
                    password=settings.smtp_password,
                    sender=settings.smtp_from,
                    starttls=settings.smtp_starttls,
                    recipient=normalized_email,
                    code=code,
                    purpose="register",
                    expires_minutes=settings.email_code_minutes,
                )
            except Exception:
                with transaction(settings.db_path) as conn:
                    conn.execute(
                        "UPDATE member_email_codes SET used_at=CURRENT_TIMESTAMP WHERE id=?",
                        (code_id,),
                    )
                return member_redirect(
                    "/account/register",
                    "Не удалось отправить письмо. Попробуйте позже",
                    error=True,
                )
            request.session["member_registration_code_id"] = code_id
            request.session["member_registration_draft"] = {
                "email": normalized_email,
                "phone": phone.strip()[:80],
                "first_name": first_name.strip()[:100],
            }
            return member_redirect(
                "/account/register", "Код отправлен на указанную почту"
            )
        except ValueError as exc:
            return member_redirect("/account/register", str(exc), error=True)

    @app.post("/account/register/resend-code")
    async def member_register_resend_code(
        request: Request,
        csrf_token: str = Form(...),
    ):
        member_portal_or_404()
        check_csrf(request, csrf_token)
        if not settings.smtp_host or not settings.smtp_from:
            return member_redirect(
                "/account/register",
                "Отправка писем пока не настроена",
                error=True,
            )
        code_id = request.session.get("member_registration_code_id")
        if not code_id:
            return member_redirect(
                "/account/register", "Сначала укажите почту и запросите код", error=True
            )
        try:
            with connect(settings.db_path) as conn:
                row = conn.execute(
                    "SELECT * FROM member_email_codes WHERE id=? AND purpose='register'",
                    (int(code_id),),
                ).fetchone()
            if not row or row["used_at"]:
                request.session.pop("member_registration_code_id", None)
                raise ValueError("Сессия подтверждения устарела. Укажите почту снова")
            payload = json.loads(row["payload_json"])
            normalized_email = str(row["email_normalized"])
            code = generate_member_email_code()
            expires_at = quiz_timestamp(
                utc_now() + timedelta(minutes=settings.email_code_minutes)
            )
            with transaction(settings.db_path) as conn:
                conn.execute(
                    """
                    UPDATE member_email_codes SET used_at=CURRENT_TIMESTAMP
                    WHERE email_normalized=? AND purpose='register' AND used_at IS NULL
                    """,
                    (normalized_email,),
                )
                cursor = conn.execute(
                    """
                    INSERT INTO member_email_codes(
                        email_normalized, purpose, code_hash, payload_json, expires_at
                    ) VALUES (?, 'register', ?, ?, ?)
                    """,
                    (
                        normalized_email,
                        member_code_hash(
                            settings.secret_key, "register", normalized_email, code
                        ),
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        expires_at,
                    ),
                )
                new_code_id = int(cursor.lastrowid)
            try:
                await run_in_threadpool(
                    send_member_email_code,
                    host=settings.smtp_host,
                    port=settings.smtp_port,
                    username=settings.smtp_username,
                    password=settings.smtp_password,
                    sender=settings.smtp_from,
                    starttls=settings.smtp_starttls,
                    recipient=normalized_email,
                    code=code,
                    purpose="register",
                    expires_minutes=settings.email_code_minutes,
                )
            except Exception:
                with transaction(settings.db_path) as conn:
                    conn.execute(
                        "UPDATE member_email_codes SET used_at=CURRENT_TIMESTAMP WHERE id=?",
                        (new_code_id,),
                    )
                return member_redirect(
                    "/account/register",
                    "Не удалось отправить письмо. Попробуйте позже или укажите другую почту",
                    error=True,
                )
            request.session["member_registration_code_id"] = new_code_id
            request.session["member_registration_draft"] = {
                "email": normalized_email,
                "phone": str(payload.get("phone") or ""),
                "first_name": str(payload.get("first_name") or ""),
            }
            return member_redirect(
                "/account/register", f"Новый код отправлен на {normalized_email}"
            )
        except (ValueError, json.JSONDecodeError) as exc:
            return member_redirect("/account/register", str(exc), error=True)

    @app.post("/account/register/change-email")
    async def member_register_change_email(
        request: Request,
        csrf_token: str = Form(...),
    ):
        member_portal_or_404()
        check_csrf(request, csrf_token)
        code_id = request.session.get("member_registration_code_id")
        draft = request.session.get("member_registration_draft") or {}
        if code_id:
            with connect(settings.db_path) as conn:
                row = conn.execute(
                    "SELECT payload_json FROM member_email_codes WHERE id=? AND purpose='register'",
                    (int(code_id),),
                ).fetchone()
            if row:
                try:
                    payload = json.loads(row["payload_json"])
                    draft = {
                        "email": str(payload.get("email") or draft.get("email") or ""),
                        "phone": str(payload.get("phone") or draft.get("phone") or ""),
                        "first_name": str(
                            payload.get("first_name") or draft.get("first_name") or ""
                        ),
                    }
                except json.JSONDecodeError:
                    pass
            with transaction(settings.db_path) as conn:
                conn.execute(
                    "UPDATE member_email_codes SET used_at=CURRENT_TIMESTAMP WHERE id=?",
                    (int(code_id),),
                )
        request.session.pop("member_registration_code_id", None)
        if draft:
            request.session["member_registration_draft"] = draft
        return member_redirect(
            "/account/register",
            "Укажите другую почту и запросите новый код",
        )

    @app.post("/account/register/verify")
    async def member_register_verify(
        request: Request,
        code: str = Form(...),
        csrf_token: str = Form(...),
    ):
        member_portal_or_404()
        check_csrf(request, csrf_token)
        code_id = request.session.get("member_registration_code_id")
        if not code_id:
            return member_redirect(
                "/account/register", "Запросите новый код", error=True
            )
        try:
            invalid_code = False
            with transaction(settings.db_path) as conn:
                row = conn.execute(
                    "SELECT * FROM member_email_codes WHERE id=? AND purpose='register'",
                    (int(code_id),),
                ).fetchone()
                if (
                    not row
                    or row["used_at"]
                    or row["expires_at"] <= quiz_timestamp(utc_now())
                    or int(row["attempts_left"]) <= 0
                ):
                    raise ValueError("Код истёк. Запросите новый")
                expected = member_code_hash(
                    settings.secret_key,
                    "register",
                    row["email_normalized"],
                    code.strip(),
                )
                if not hmac.compare_digest(expected, row["code_hash"]):
                    conn.execute(
                        "UPDATE member_email_codes SET attempts_left=attempts_left-1 WHERE id=?",
                        (row["id"],),
                    )
                    invalid_code = True
                else:
                    payload = json.loads(row["payload_json"])
                    if conn.execute(
                        "SELECT 1 FROM member_accounts WHERE email_normalized=?",
                        (row["email_normalized"],),
                    ).fetchone():
                        raise ValueError("Аккаунт с такой почтой уже существует")
                    client_id, _, _ = find_or_create_quiz_client(
                        conn,
                        campaign="member_account",
                        phone_raw=payload["phone"],
                        username="",
                        telegram_user_id="",
                        name=payload.get("first_name", ""),
                        email=payload["email"],
                        source="member_portal",
                        match_username=False,
                    )
                    cursor = conn.execute(
                        """
                        INSERT INTO member_accounts(
                            client_id, email, email_normalized, password_hash, email_verified_at
                        ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        (
                            client_id,
                            payload["email"],
                            payload["email"],
                            payload["password_hash"],
                        ),
                    )
                    account_id = int(cursor.lastrowid)
                    for consent in payload["consents"]:
                        document = conn.execute(
                            """
                            SELECT id FROM legal_documents
                            WHERE id=? AND code=? AND version=? AND is_active=1
                            """,
                            (
                                consent["document_id"],
                                consent["code"],
                                consent["version"],
                            ),
                        ).fetchone()
                        if not document:
                            raise ValueError(
                                "Условия изменились. Начните регистрацию заново"
                            )
                        conn.execute(
                            """
                            INSERT INTO member_consents(
                                account_id, document_id, document_code, document_version,
                                accepted_at, ip_hash, user_agent
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                account_id,
                                document["id"],
                                consent["code"],
                                consent["version"],
                                consent["accepted_at"] or quiz_timestamp(utc_now()),
                                member_ip_hash(request),
                                request.headers.get("user-agent", "")[:500],
                            ),
                        )
                    conn.execute(
                        "UPDATE member_email_codes SET used_at=CURRENT_TIMESTAMP WHERE id=?",
                        (row["id"],),
                    )
                    member_token = issue_member_session(
                        conn,
                        secret_key=settings.secret_key,
                        account_id=account_id,
                        session_version=1,
                        days=settings.member_session_days,
                        ip_hash=member_ip_hash(request),
                        user_agent=request.headers.get("user-agent", ""),
                    )
            if invalid_code:
                raise ValueError("Неверный код")
            request.session.pop("member_registration_code_id", None)
            request.session.pop("member_registration_flow", None)
            request.session.pop("member_registration_draft", None)
            response = RedirectResponse("/account/telegram", status_code=303)
            set_member_cookie(response, member_token)
            return response
        except (ValueError, sqlite3.IntegrityError) as exc:
            message = (
                str(exc)
                if isinstance(exc, ValueError)
                else "Эти данные уже связаны с другим аккаунтом"
            )
            return member_redirect("/account/register", message, error=True)

    @app.get("/account/login", response_class=HTMLResponse)
    async def member_login_page(
        request: Request, error: str = "", ok: str = "", next: str = ""
    ):
        member_portal_or_404()
        next_path = remember_member_next(request, next)
        if current_member(request):
            return RedirectResponse(
                member_continue_path(request, consume=True), status_code=303
            )
        return templates.TemplateResponse(
            request,
            "member_login.html",
            member_context(
                request,
                error=error,
                ok=ok,
                mode="login",
                next_path=next_path or member_continue_path(request),
            ),
        )

    @app.post("/account/login")
    async def member_login(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        csrf_token: str = Form(...),
    ):
        member_portal_or_404()
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            account = authenticate_account(conn, email=email, password=password)
            if not account:
                return member_redirect(
                    "/account/login", "Неверная почта или пароль", error=True
                )
            token = issue_member_session(
                conn,
                secret_key=settings.secret_key,
                account_id=int(account["id"]),
                session_version=int(account["session_version"]),
                days=settings.member_session_days,
                ip_hash=member_ip_hash(request),
                user_agent=request.headers.get("user-agent", ""),
            )
        response = RedirectResponse(
            member_continue_path(request, consume=True), status_code=303
        )
        set_member_cookie(response, token)
        return response

    @app.get("/account/consents", response_class=HTMLResponse)
    async def member_consents_page(
        request: Request, error: str = "", ok: str = ""
    ):
        member_portal_or_404()
        member = current_member(request, required=True)
        missing = missing_member_documents(int(member["id"]))
        if not missing:
            return RedirectResponse(
                member_continue_path(request, consume=True), status_code=303
            )
        return templates.TemplateResponse(
            request,
            "member_consent.html",
            member_context(
                request,
                member=member,
                document=missing[0],
                error=error,
                ok=ok,
            ),
        )

    @app.post("/account/consents")
    async def member_accept_updated_consent(
        request: Request,
        document_id: int = Form(...),
        accepted: bool = Form(False),
        csrf_token: str = Form(...),
    ):
        member_portal_or_404()
        check_csrf(request, csrf_token)
        member = current_member(request, required=True)
        if not accepted:
            return member_redirect(
                "/account/consents",
                "Для продолжения необходимо принять обновлённые условия",
                error=True,
            )
        with transaction(settings.db_path) as conn:
            document = conn.execute(
                "SELECT * FROM legal_documents WHERE id=? AND is_active=1",
                (document_id,),
            ).fetchone()
            if not document:
                return member_redirect(
                    "/account/consents",
                    "Документ изменился. Ознакомьтесь с новой редакцией",
                    error=True,
                )
            conn.execute(
                """
                INSERT OR IGNORE INTO member_consents(
                    account_id, document_id, document_code, document_version,
                    ip_hash, user_agent
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    member["id"],
                    document["id"],
                    document["code"],
                    document["version"],
                    member_ip_hash(request),
                    request.headers.get("user-agent", "")[:500],
                ),
            )
        return RedirectResponse("/account/consents", status_code=303)

    @app.post("/account/logout")
    async def member_logout(request: Request, csrf_token: str = Form(...)):
        member_portal_or_404()
        check_csrf(request, csrf_token)
        token = member_cookie(request)
        with transaction(settings.db_path) as conn:
            revoke_member_session(
                conn, secret_key=settings.secret_key, token=token
            )
        response = RedirectResponse("/account/login", status_code=303)
        clear_member_cookie(response)
        return response

    @app.get("/account/forgot-password", response_class=HTMLResponse)
    async def member_forgot_page(request: Request, error: str = "", ok: str = ""):
        member_portal_or_404()
        return templates.TemplateResponse(
            request,
            "member_login.html",
            member_context(request, error=error, ok=ok, mode="forgot"),
        )

    @app.post("/account/forgot-password")
    async def member_forgot_password(
        request: Request,
        email: str = Form(...),
        csrf_token: str = Form(...),
    ):
        member_portal_or_404()
        check_csrf(request, csrf_token)
        if not settings.smtp_host or not settings.smtp_from:
            return member_redirect(
                "/account/forgot-password",
                "Отправка писем пока не настроена",
                error=True,
            )
        try:
            normalized = normalize_email(email)
        except ValueError:
            normalized = None
        account = None
        if normalized:
            with connect(settings.db_path) as conn:
                account = conn.execute(
                    "SELECT id FROM member_accounts WHERE email_normalized=? AND is_active=1",
                    (normalized,),
                ).fetchone()
        if account and normalized:
            code = generate_member_email_code()
            with transaction(settings.db_path) as conn:
                conn.execute(
                    """
                    UPDATE member_email_codes SET used_at=CURRENT_TIMESTAMP
                    WHERE email_normalized=? AND purpose='reset_password' AND used_at IS NULL
                    """,
                    (normalized,),
                )
                cursor = conn.execute(
                    """
                    INSERT INTO member_email_codes(
                        email_normalized, purpose, code_hash, payload_json, expires_at
                    ) VALUES (?, 'reset_password', ?, ?, ?)
                    """,
                    (
                        normalized,
                        member_code_hash(
                            settings.secret_key, "reset_password", normalized, code
                        ),
                        json.dumps({"account_id": int(account["id"])}),
                        quiz_timestamp(
                            utc_now()
                            + timedelta(minutes=settings.email_code_minutes)
                        ),
                    ),
                )
                code_id = int(cursor.lastrowid)
            try:
                await run_in_threadpool(
                    send_member_email_code,
                    host=settings.smtp_host,
                    port=settings.smtp_port,
                    username=settings.smtp_username,
                    password=settings.smtp_password,
                    sender=settings.smtp_from,
                    starttls=settings.smtp_starttls,
                    recipient=normalized,
                    code=code,
                    purpose="reset_password",
                    expires_minutes=settings.email_code_minutes,
                )
                request.session["member_reset_code_id"] = code_id
            except Exception:
                with transaction(settings.db_path) as conn:
                    conn.execute(
                        "UPDATE member_email_codes SET used_at=CURRENT_TIMESTAMP WHERE id=?",
                        (code_id,),
                    )
        return member_redirect(
            "/account/reset-password",
            "Если аккаунт найден, код отправлен на почту",
        )

    @app.get("/account/reset-password", response_class=HTMLResponse)
    async def member_reset_page(request: Request, error: str = "", ok: str = ""):
        member_portal_or_404()
        return templates.TemplateResponse(
            request,
            "member_login.html",
            member_context(request, error=error, ok=ok, mode="reset"),
        )

    @app.post("/account/reset-password")
    async def member_reset_password(
        request: Request,
        code: str = Form(...),
        password: str = Form(...),
        password_confirmation: str = Form(...),
        csrf_token: str = Form(...),
    ):
        member_portal_or_404()
        check_csrf(request, csrf_token)
        code_id = request.session.get("member_reset_code_id")
        if not code_id:
            return member_redirect(
                "/account/forgot-password", "Запросите новый код", error=True
            )
        try:
            if password != password_confirmation:
                raise ValueError("Пароли не совпадают")
            password_hash = hash_password(password)
            invalid_code = False
            with transaction(settings.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT * FROM member_email_codes
                    WHERE id=? AND purpose='reset_password'
                    """,
                    (int(code_id),),
                ).fetchone()
                if (
                    not row
                    or row["used_at"]
                    or row["expires_at"] <= quiz_timestamp(utc_now())
                    or int(row["attempts_left"]) <= 0
                ):
                    raise ValueError("Код истёк. Запросите новый")
                expected = member_code_hash(
                    settings.secret_key,
                    "reset_password",
                    row["email_normalized"],
                    code.strip(),
                )
                if not hmac.compare_digest(expected, row["code_hash"]):
                    conn.execute(
                        "UPDATE member_email_codes SET attempts_left=attempts_left-1 WHERE id=?",
                        (row["id"],),
                    )
                    invalid_code = True
                else:
                    payload = json.loads(row["payload_json"])
                    conn.execute(
                        """
                        UPDATE member_accounts
                        SET password_hash=?, session_version=session_version+1,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                        """,
                        (password_hash, int(payload["account_id"])),
                    )
                    conn.execute(
                        """
                        UPDATE member_sessions SET revoked_at=CURRENT_TIMESTAMP
                        WHERE account_id=? AND revoked_at IS NULL
                        """,
                        (int(payload["account_id"]),),
                    )
                    conn.execute(
                        "UPDATE member_email_codes SET used_at=CURRENT_TIMESTAMP WHERE id=?",
                        (row["id"],),
                    )
            if invalid_code:
                raise ValueError("Неверный код")
            request.session.pop("member_reset_code_id", None)
            return member_redirect(
                "/account/login", "Пароль обновлён. Войдите в аккаунт"
            )
        except ValueError as exc:
            return member_redirect("/account/reset-password", str(exc), error=True)

    @app.get("/account", response_class=HTMLResponse)
    async def member_account_page(
        request: Request, tab: str = "personal", ok: str = "", error: str = ""
    ):
        member_portal_or_404()
        member = current_member(request, required=True)
        if missing_member_documents(int(member["id"])):
            return RedirectResponse("/account/consents", status_code=303)
        if tab not in {"personal", "stats", "rewards"}:
            tab = "personal"
        vault_catalog = []
        vault_active_rewards = []
        vault_reward_history = []
        with connect(settings.db_path) as conn:
            balance = jackcoin_balance(conn, int(member["client_id"]))
            history = conn.execute(
                """
                SELECT qs.*, qc.title AS campaign_title
                FROM quiz_submissions qs
                LEFT JOIN quiz_campaigns qc ON qc.code=qs.campaign_code
                WHERE qs.client_id=?
                ORDER BY qs.created_at DESC LIMIT 50
                """,
                (member["client_id"],),
            ).fetchall()
            rating = conn.execute(
                """
                SELECT cre.*, crs.snapshot_date
                FROM club_rating_entries cre
                JOIN club_rating_snapshots crs ON crs.id=cre.snapshot_id
                WHERE cre.client_id=?
                ORDER BY crs.snapshot_date DESC, crs.id DESC LIMIT 1
                """,
                (member["client_id"],),
            ).fetchone()
            ledger = conn.execute(
                """
                SELECT * FROM jackcoin_ledger WHERE client_id=?
                ORDER BY created_at DESC, id DESC LIMIT 50
                """,
                (member["client_id"],),
            ).fetchall()
            consents = conn.execute(
                """
                SELECT mc.*, ld.title FROM member_consents mc
                JOIN legal_documents ld ON ld.id=mc.document_id
                WHERE mc.account_id=? ORDER BY mc.accepted_at
                """,
                (member["id"],),
            ).fetchall()
            if tab == "rewards":
                expire_vault_rewards(conn, client_id=int(member["client_id"]))
                vault_catalog = conn.execute(
                    """
                    SELECT vcr.*,
                           COUNT(vmr.id) AS allocated_count
                    FROM vault_catalog_rewards vcr
                    LEFT JOIN vault_member_rewards vmr
                      ON vmr.catalog_reward_id=vcr.id
                     AND vmr.status<>'cancelled'
                    WHERE vcr.is_active=1
                    GROUP BY vcr.id
                    ORDER BY vcr.position, vcr.id
                    """
                ).fetchall()
                member_rewards = conn.execute(
                    """
                    SELECT vmr.*, vcr.title, vcr.description, vcr.category,
                           vcr.redeem_instructions
                    FROM vault_member_rewards vmr
                    JOIN vault_catalog_rewards vcr ON vcr.id=vmr.catalog_reward_id
                    WHERE vmr.client_id=?
                    ORDER BY CASE vmr.status WHEN 'active' THEN 0 ELSE 1 END,
                             vmr.created_at DESC, vmr.id DESC
                    """,
                    (member["client_id"],),
                ).fetchall()
                vault_active_rewards = [
                    reward for reward in member_rewards if reward["status"] == "active"
                ]
                vault_reward_history = [
                    reward for reward in member_rewards if reward["status"] != "active"
                ]
        vault_purchase_tokens = {
            int(reward["id"]): vault_purchase_token(
                settings.secret_key,
                account_id=int(member["id"]),
                catalog_reward_id=int(reward["id"]),
                price_jc=int(reward["price_jc"]),
            )
            for reward in vault_catalog
        }
        return templates.TemplateResponse(
            request,
            "member_account.html",
            member_context(
                request,
                member=member,
                current_tab=tab,
                balance=balance,
                history=history,
                rating=rating,
                ledger=ledger,
                consents=consents,
                vault_catalog=vault_catalog,
                vault_active_rewards=vault_active_rewards,
                vault_reward_history=vault_reward_history,
                vault_purchase_tokens=vault_purchase_tokens,
                ok=ok,
                error=error,
            ),
        )

    def vault_member_redirect(message: str, *, error: bool = False) -> RedirectResponse:
        parameter = "error" if error else "ok"
        return RedirectResponse(
            f"/account?{urlencode({'tab': 'rewards', parameter: message})}",
            status_code=303,
        )

    @app.post("/account/rewards/{catalog_reward_id:int}/purchase")
    async def member_purchase_vault_reward(
        request: Request,
        catalog_reward_id: int,
        purchase_id: str = Form(...),
        expected_price_jc: int = Form(...),
        csrf_token: str = Form(...),
    ):
        member_portal_or_404()
        member = current_member(request, required=True)
        check_csrf(request, csrf_token)
        if missing_member_documents(int(member["id"])):
            return member_redirect(
                "/account/consents",
                "Сначала примите актуальные условия программы",
                error=True,
            )
        if not valid_purchase_token(
            settings.secret_key,
            purchase_id,
            account_id=int(member["id"]),
            catalog_reward_id=catalog_reward_id,
            price_jc=expected_price_jc,
        ):
            raise HTTPException(status_code=403, detail="invalid_purchase_token")
        try:
            with transaction(settings.db_path) as conn:
                reward = purchase_vault_reward(
                    conn,
                    client_id=int(member["client_id"]),
                    catalog_reward_id=catalog_reward_id,
                    purchase_id=purchase_id,
                    expected_price_jc=expected_price_jc,
                )
                title = conn.execute(
                    "SELECT title FROM vault_catalog_rewards WHERE id=?",
                    (catalog_reward_id,),
                ).fetchone()[0]
        except ValueError as exc:
            messages = {
                "catalog_reward_not_found": "Награда не найдена",
                "catalog_reward_inactive": "Эта награда сейчас недоступна",
                "catalog_reward_sold_out": "Тираж этой награды закончился",
                "catalog_reward_price_changed": "Стоимость награды изменилась. Обновите страницу перед обменом",
                "insufficient_jackcoin": "Недостаточно JACKCOIN для обмена",
                "invalid_purchase_token": "Заявка на обмен устарела. Обновите страницу",
            }
            return vault_member_redirect(
                messages.get(str(exc), "Не удалось получить награду"), error=True
            )
        return vault_member_redirect(
            f"Карта «{title}» добавлена в активные награды. Код: {reward['code']}"
        )

    @app.get("/account/rewards/{member_reward_id:int}/qr.png")
    async def member_vault_reward_qr(request: Request, member_reward_id: int):
        member_portal_or_404()
        member = current_member(request, required=True)
        with connect(settings.db_path) as conn:
            reward = conn.execute(
                """
                SELECT vmr.*, vcr.title
                FROM vault_member_rewards vmr
                JOIN vault_catalog_rewards vcr ON vcr.id=vmr.catalog_reward_id
                WHERE vmr.id=? AND vmr.client_id=?
                """,
                (member_reward_id, member["client_id"]),
            ).fetchone()
        if not reward:
            raise HTTPException(status_code=404, detail="Награда не найдена")
        if reward["status"] != "active":
            raise HTTPException(status_code=410, detail="Карта уже не активна")
        if reward["valid_until"] and utc_now() > datetime.fromisoformat(
            str(reward["valid_until"])
        ).astimezone(timezone.utc):
            raise HTTPException(status_code=410, detail="Срок карты истёк")
        redeem_url = (
            f"{settings.public_base_url.rstrip('/')}/admin/vault?"
            f"{urlencode({'code': reward['code']})}"
        )
        image = qrcode.make(redeem_url)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return Response(
            buffer.getvalue(),
            media_type="image/png",
            headers={"Cache-Control": "private, no-store"},
        )

    @app.get("/account/telegram", response_class=HTMLResponse)
    async def member_telegram_page(
        request: Request, ok: str = "", error: str = ""
    ):
        member_portal_or_404()
        member = current_member(request, required=True)
        return templates.TemplateResponse(
            request,
            "member_telegram.html",
            member_context(
                request,
                member=member,
                telegram_available=bool(
                    settings.telegram_client_id
                    and settings.telegram_client_secret
                ),
                continue_path=member_continue_path(request),
                ok=ok,
                error=error,
            ),
        )

    @app.get("/account/continue")
    async def member_continue(request: Request):
        member_portal_or_404()
        current_member(request, required=True)
        return RedirectResponse(
            member_continue_path(request, consume=True), status_code=303
        )

    @app.get("/quiz", response_class=HTMLResponse)
    async def quiz_page(
        request: Request,
        campaign: str = "default",
        ref: str = "",
        referrer_id: str = "",
        source: str = "",
        telegram_error: str = "",
        telegram_verified: bool = False,
    ):
        campaign = normalize_campaign(campaign)
        campaign_row = quiz_campaign_row(campaign)
        if campaign_row["campaign_type"] == "daily_414":
            current_path = request.url.path
            if request.url.query:
                current_path += f"?{request.url.query}"
            member = current_member(request)
            if not member:
                return RedirectResponse(
                    f"/account/login?{urlencode({'next': current_path})}",
                    status_code=303,
                )
            if missing_member_documents(int(member["id"])):
                remember_member_next(request, current_path)
                return RedirectResponse("/account/consents", status_code=303)
        schedule_state = campaign_schedule_state(campaign_row)
        return templates.TemplateResponse(
            request,
            "quiz.html",
            {
                "request": request,
                "campaign": campaign,
                "campaign_title": campaign_row["title"],
                "campaign_type": campaign_row["campaign_type"],
                "referrer_id": (ref or referrer_id).strip()[:80],
                "source": source.strip()[:80],
                "telegram_available": bool(settings.telegram_client_id and settings.telegram_client_secret),
                "email_auth_available": bool(settings.smtp_host and settings.smtp_from),
                "telegram_error": telegram_error[:300],
                "telegram_verified": telegram_verified,
                "schedule_state": schedule_state,
                "active_from_iso": campaign_datetime_iso(campaign_row["active_from"]),
                "active_until_iso": campaign_datetime_iso(campaign_row["active_until"]),
                "server_now_iso": datetime.now(campaign_timezone).isoformat(),
                "campaign_background": campaign_background(campaign),
                "daily_prize_title": campaign_row["bonus_title"] or "Главный приз дня",
                "daily_prize_amount": int(campaign_row["bonus_amount"] or 0),
            },
        )

    @app.get("/api/quiz/questions")
    async def quiz_questions(request: Request, campaign: str = "default"):
        campaign = normalize_campaign(campaign)
        campaign_row = quiz_campaign_for_meta(campaign)
        schedule_state = campaign_schedule_state(campaign_row)
        try:
            with connect(settings.db_path) as conn:
                questions = load_db_questions(conn, campaign)
                if campaign_row["campaign_type"] == "daily_414":
                    validate_daily_questions(questions, campaign)
        except ValueError as exc:
            messages = {
                "daily_414_requires_ten_questions": (
                    "Для выпуска 4:14 нужно опубликовать ровно 10 вопросов"
                ),
                "daily_414_requires_own_questions": (
                    "Добавьте 10 собственных вопросов выпуска 4:14"
                ),
            }
            raise HTTPException(
                status_code=409,
                detail=messages.get(str(exc), str(exc)),
            )
        member = daily_member(
            request,
            campaign_row,
            required=campaign_row["campaign_type"] == "daily_414",
        )
        return {
            "campaign": campaign,
            "campaign_version": int(campaign_row["current_version"] or 1),
            "title": campaign_row["title"],
            "campaign_type": campaign_row["campaign_type"],
            "schedule_state": schedule_state,
            "active_from": campaign_datetime_iso(campaign_row["active_from"]),
            "active_until": campaign_datetime_iso(campaign_row["active_until"]),
            "questions": [],
            "questions_count": len(questions),
            "timed": campaign_row["quiz_time_limit_seconds"] > 0,
            "time_limit_seconds": campaign_row["quiz_time_limit_seconds"],
            "max_attempts": campaign_row["max_attempts"],
            "verification_required": bool(campaign_row["verification_required"]),
            "member_authenticated": bool(member),
            "telegram_available": bool(settings.telegram_client_id and settings.telegram_client_secret),
            "email_available": bool(settings.smtp_host and settings.smtp_from),
            "content": {
                "welcome_kicker": campaign_row["welcome_kicker"],
                "welcome_text": campaign_row["welcome_text"],
                "start_button_text": campaign_row["start_button_text"],
                "identity_text": campaign_row["identity_text"],
            },
            "daily_414": (
                {
                    "question_count": DAILY_414_QUESTION_COUNT,
                    "time_limit_seconds": DAILY_414_TIME_LIMIT_SECONDS,
                    "entry_window_seconds": DAILY_414_ENTRY_WINDOW_SECONDS,
                    "final_question_seconds": DAILY_414_FINAL_QUESTION_SECONDS,
                    "final_table_size": DAILY_414_FINAL_TABLE_SIZE,
                    "one_attempt": True,
                    "jackcoin_per_correct": int(
                        campaign_row["jackcoin_per_correct"]
                    ),
                    "completion_jackcoin": int(
                        campaign_row["jackcoin_completion_bonus"]
                    ),
                    "perfect_jackcoin": int(
                        campaign_row["jackcoin_perfect_bonus"]
                    ),
                    "prize_title": campaign_row["bonus_title"]
                    or "Главный приз дня",
                    "prize_amount": int(campaign_row["bonus_amount"] or 0),
                }
                if campaign_row["campaign_type"] == "daily_414"
                else None
            ),
        }

    def utc_now() -> datetime:
        return datetime.now(timezone.utc)

    def quiz_timestamp(value: datetime) -> str:
        return value.isoformat(timespec="milliseconds")

    def quiz_device_cookie(request: Request) -> str:
        return str(request.cookies.get(DEVICE_COOKIE_NAME, "")).strip()

    def set_quiz_device_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            DEVICE_COOKIE_NAME,
            token,
            max_age=DEVICE_MAX_AGE_SECONDS,
            path="/",
            secure=settings.secure_cookie,
            httponly=True,
            samesite="lax",
        )

    def clear_quiz_device_cookie(response: Response) -> None:
        response.delete_cookie(
            DEVICE_COOKIE_NAME,
            path="/",
            secure=settings.secure_cookie,
            httponly=True,
            samesite="lax",
        )

    def parse_quiz_payload(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Некорректные данные")
        return payload

    async def request_json(request: Request) -> dict[str, Any]:
        try:
            return parse_quiz_payload(await request.json())
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="Некорректные данные")

    def verified_identity(request: Request, campaign: str) -> dict[str, Any]:
        value = request.session.get("quiz_verified_identity")
        if not isinstance(value, dict) or value.get("campaign") != campaign:
            return {}
        return value

    @app.get("/api/quiz/identity")
    async def quiz_identity_status(request: Request, campaign: str = "default"):
        campaign = normalize_campaign(campaign)
        campaign_row = quiz_campaign_for_meta(campaign)
        if campaign_row["campaign_type"] == "daily_414":
            member = daily_member(request, campaign_row)
            return {
                "verified": True,
                "remembered": False,
                "method": "member",
                "display_name": (
                    member["first_name"]
                    or member["nickname"]
                    or (
                        f"@{member['username']}"
                        if member["username"]
                        else "участник JACKSIDE"
                    )
                ),
            }
        campaign_row = quiz_campaign_or_404(campaign)
        device_token = quiz_device_cookie(request)
        if device_token:
            with transaction(settings.db_path) as conn:
                client = remembered_client(
                    conn,
                    secret_key=settings.secret_key,
                    token=device_token,
                    touch=True,
                )
            if client:
                response = JSONResponse(
                    {
                        "verified": False,
                        "remembered": True,
                        "display_name": remembered_display_name(client),
                    }
                )
                set_quiz_device_cookie(response, device_token)
                return response
        identity = verified_identity(request, campaign)
        return {
            "verified": bool(identity),
            "remembered": False,
            "method": identity.get("method"),
            "username": identity.get("username"),
            "email": identity.get("email"),
        }

    @app.post("/api/quiz/identity/confirm")
    async def quiz_identity_confirm(request: Request):
        payload = await request_json(request)
        campaign = normalize_campaign(payload.get("campaign"))
        quiz_campaign_or_404(campaign)
        device_token = quiz_device_cookie(request)
        with transaction(settings.db_path) as conn:
            client = remembered_client(
                conn,
                secret_key=settings.secret_key,
                token=device_token,
                touch=True,
            )
        if not client:
            raise HTTPException(status_code=401, detail="Сохранённый вход устарел. Подтвердите данные заново")
        request.session["quiz_verified_identity"] = {
            "campaign": campaign,
            "method": "device",
            "client_id": int(client["client_id"]),
        }
        response = JSONResponse(
            {
                "ok": True,
                "identity": {
                    "verified": True,
                    "method": "device",
                    "display_name": remembered_display_name(client),
                },
            }
        )
        set_quiz_device_cookie(response, device_token)
        return response

    @app.post("/api/quiz/identity/forget")
    async def quiz_identity_forget(request: Request):
        device_token = quiz_device_cookie(request)
        if device_token:
            with transaction(settings.db_path) as conn:
                forget_device(conn, secret_key=settings.secret_key, token=device_token)
        request.session.pop("quiz_verified_identity", None)
        response = JSONResponse({"ok": True})
        clear_quiz_device_cookie(response)
        return response

    @app.get("/quiz/telegram/start")
    async def quiz_telegram_start(
        request: Request,
        campaign: str = "default",
        referrer_id: str = "",
        source: str = "",
    ):
        campaign = normalize_campaign(campaign)
        quiz_campaign_or_404(campaign)
        if not settings.telegram_client_id or not settings.telegram_client_secret:
            raise HTTPException(status_code=503, detail="Вход через Telegram пока не настроен")
        verifier, challenge = new_pkce()
        state = secrets.token_urlsafe(32)
        redirect_uri = settings.quiz_public_base_url.rstrip("/") + "/quiz/telegram/callback"
        request.session["telegram_oauth"] = {
            "state": state,
            "verifier": verifier,
            "campaign": campaign,
            "referrer_id": referrer_id.strip()[:80],
            "source": source.strip()[:80],
            "created_at": int(utc_now().timestamp()),
        }
        return RedirectResponse(
            authorization_url(
                client_id=settings.telegram_client_id, redirect_uri=redirect_uri,
                state=state, code_challenge=challenge,
            ),
            status_code=302,
        )

    @app.get("/quiz/telegram/callback")
    async def quiz_telegram_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        oauth = request.session.pop("telegram_oauth", None)
        campaign = normalize_campaign(oauth.get("campaign") if isinstance(oauth, dict) else "default")
        return_query = {
            "campaign": campaign,
            "referrer_id": str(oauth.get("referrer_id", ""))[:80] if isinstance(oauth, dict) else "",
            "source": str(oauth.get("source", ""))[:80] if isinstance(oauth, dict) else "",
        }
        failure_url = f"/quiz?{urlencode({**return_query, 'telegram_error': 'Не удалось подтвердить Telegram. Попробуйте ещё раз'})}"
        if error or not code or not state or not isinstance(oauth, dict):
            return RedirectResponse(failure_url, status_code=303)
        if not hmac.compare_digest(state, str(oauth.get("state", ""))):
            return RedirectResponse(failure_url, status_code=303)
        if int(utc_now().timestamp()) - int(oauth.get("created_at", 0)) > 900:
            return RedirectResponse(
                f"/quiz?{urlencode({**return_query, 'telegram_error': 'Вход Telegram устарел. Попробуйте ещё раз'})}",
                status_code=303,
            )
        redirect_uri = settings.quiz_public_base_url.rstrip("/") + "/quiz/telegram/callback"
        try:
            claims = await run_in_threadpool(
                exchange_telegram_code,
                code=code, client_id=settings.telegram_client_id,
                client_secret=settings.telegram_client_secret, redirect_uri=redirect_uri,
                code_verifier=str(oauth["verifier"]),
            )
        except Exception:
            return RedirectResponse(failure_url, status_code=303)
        username = str(claims.get("preferred_username") or "")
        name = str(claims.get("name") or " ".join(filter(None, [claims.get("given_name"), claims.get("family_name")])))
        request.session["quiz_verified_identity"] = {
            "campaign": campaign, "method": "telegram", "telegram_user_id": str(claims["sub"]),
            "username": username, "name": name.strip()[:100],
        }
        return RedirectResponse(
            f"/quiz?{urlencode({**return_query, 'telegram_verified': '1'})}",
            status_code=303,
        )

    @app.post("/api/quiz/email/request")
    async def quiz_email_request(request: Request):
        if not settings.smtp_host or not settings.smtp_from:
            raise HTTPException(status_code=503, detail="Вход по email пока не настроен")
        payload = await request_json(request)
        campaign = normalize_campaign(payload.get("campaign"))
        campaign_row = quiz_campaign_or_404(campaign)
        try:
            email = normalize_email(payload.get("email"))
        except ValueError:
            raise HTTPException(status_code=422, detail="Проверьте адрес электронной почты")
        if not email:
            raise HTTPException(status_code=422, detail="Укажите электронную почту")
        phone = str(payload.get("phone", "")).strip()[:80]
        username = str(payload.get("username", "")).strip()[:100]
        if not phone and not username:
            raise HTTPException(status_code=422, detail="Укажите номер телефона или Telegram username")
        code = generate_email_code()
        expires = utc_now() + timedelta(minutes=settings.email_code_minutes)
        identity = {
            "phone": phone,
            "username": username,
            "name": str(payload.get("name", "")).strip()[:100],
            "nickname": str(payload.get("nickname", "")).strip()[:100],
            "email": email,
        }
        with transaction(settings.db_path) as conn:
            recent = conn.execute(
                "SELECT COUNT(*) FROM quiz_email_codes WHERE email_normalized=? AND created_at >= datetime('now', '-1 hour')",
                (email,),
            ).fetchone()[0]
            if recent >= 5:
                raise HTTPException(status_code=429, detail="Слишком много кодов. Попробуйте позже")
            conn.execute(
                """
                INSERT INTO quiz_email_codes(email_normalized, code_hash, identity_json, campaign_code, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (email, email_code_hash(settings.secret_key, email, code), identity_json(**identity), campaign, quiz_timestamp(expires)),
            )
        try:
            await run_in_threadpool(
                send_quiz_email_code,
                host=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_username,
                password=settings.smtp_password,
                sender=settings.smtp_from,
                starttls=settings.smtp_starttls,
                recipient=email,
                code=code,
                campaign_title=campaign_row["title"],
                expires_minutes=settings.email_code_minutes,
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Не удалось отправить письмо. Попробуйте позже") from exc
        return {"ok": True, "expires_minutes": settings.email_code_minutes}

    @app.post("/api/quiz/email/verify")
    async def quiz_email_verify(request: Request):
        payload = await request_json(request)
        campaign = normalize_campaign(payload.get("campaign"))
        try:
            email = normalize_email(payload.get("email"))
        except ValueError:
            raise HTTPException(status_code=422, detail="Проверьте адрес электронной почты")
        code = str(payload.get("code", "")).strip()
        if not email or not re.fullmatch(r"\d{6}", code):
            raise HTTPException(status_code=422, detail="Введите шестизначный код")
        with transaction(settings.db_path) as conn:
            row = conn.execute(
                """
                SELECT * FROM quiz_email_codes
                WHERE email_normalized=? AND campaign_code=? AND used_at IS NULL
                ORDER BY id DESC LIMIT 1
                """,
                (email, campaign),
            ).fetchone()
            if not row or datetime.fromisoformat(row["expires_at"]) < utc_now():
                raise HTTPException(status_code=410, detail="Код истёк. Запросите новый")
            if row["attempts_left"] <= 0:
                raise HTTPException(status_code=429, detail="Лимит проверок исчерпан. Запросите новый код")
            expected = email_code_hash(settings.secret_key, email, code)
            if not hmac.compare_digest(expected, row["code_hash"]):
                conn.execute("UPDATE quiz_email_codes SET attempts_left=attempts_left-1 WHERE id=?", (row["id"],))
                raise HTTPException(status_code=422, detail="Неверный код")
            conn.execute("UPDATE quiz_email_codes SET used_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
            identity = json.loads(row["identity_json"])
        identity.update({"campaign": campaign, "method": "email", "email": email})
        request.session["quiz_verified_identity"] = identity
        return {"ok": True, "identity": {"method": "email", "email": email}}

    def attempt_response(
        attempt: sqlite3.Row,
        token: str,
        *,
        resumed: bool,
        campaign_type: str = "classic",
    ) -> dict[str, Any]:
        questions = json.loads(attempt["questions_snapshot_json"])
        answers = json.loads(attempt["answers_json"] or "{}")
        public = public_questions(questions)
        if campaign_type == "daily_414":
            public = public_daily_questions(public)
        return {
            "ok": True,
            "resumed": resumed,
            "campaign_type": campaign_type,
            "attempt_token": token,
            "attempt_number": int(attempt["attempt_number"]),
            "questions": public,
            "answers": answers,
            "current_index": min(max(0, int(attempt["current_index"] or 0)), max(0, len(questions) - 1)),
            "total": len(questions),
            "time_limit_seconds": 0 if not attempt["attempt_deadline_at"] else max(0, int((datetime.fromisoformat(attempt["attempt_deadline_at"]) - datetime.fromisoformat(attempt["question_started_at"])).total_seconds())),
            "deadline_at": attempt["attempt_deadline_at"],
        }

    @app.post("/api/quiz/start")
    async def quiz_start(request: Request):
        payload = await request_json(request)
        campaign = normalize_campaign(payload.get("campaign"))
        campaign_row = quiz_campaign_or_404(campaign)
        member = daily_member(
            request,
            campaign_row,
            required=campaign_row["campaign_type"] == "daily_414",
        )
        if member:
            verified = {
                "campaign": campaign,
                "method": "member",
                "client_id": int(member["client_id"]),
            }
        else:
            verified = verified_identity(request, campaign)
        if campaign_row["verification_required"] and not verified:
            raise HTTPException(status_code=403, detail="Подтвердите вход через Telegram или email")
        phone = str(verified.get("phone") or payload.get("phone") or "").strip()[:80]
        username = str(verified.get("username") or payload.get("username") or "").strip()[:100]
        name = str(verified.get("name") or payload.get("name") or "").strip()[:100]
        nickname = str(verified.get("nickname") or payload.get("nickname") or "").strip()[:100]
        email = str(verified.get("email") or payload.get("email") or "").strip()[:254]
        telegram_user_id = str(verified.get("telegram_user_id") or "")[:80]
        identity_method = str(verified.get("method") or "contact")
        source = str(payload.get("source", "")).strip()[:80]
        referrer_id = str(payload.get("referrer_id", "")).strip()[:80]
        client_ip = request.client.host if request.client else "unknown"
        ip_hash = ip_fingerprint(settings.secret_key, client_ip)
        now = utc_now()
        seconds = max(0, int(campaign_row["quiz_time_limit_seconds"] or 0))
        campaign_version = max(1, int(campaign_row["current_version"] or 1))
        user_agent = request.headers.get("user-agent", "")[:300]
        try:
            with transaction(settings.db_path) as conn:
                verified_client_id = verified.get("client_id")
                if verified_client_id is not None:
                    client = conn.execute(
                        "SELECT id FROM clients WHERE id=?",
                        (int(verified_client_id),),
                    ).fetchone()
                    if not client:
                        raise HTTPException(
                            status_code=401,
                            detail="Сохранённый вход устарел. Подтвердите данные заново",
                        )
                    client_id = int(client["id"])
                    is_new_client = False
                    conn.execute(
                        """
                        INSERT INTO client_quiz_campaigns(
                            client_id, campaign_code, first_source, first_referrer_id
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(client_id, campaign_code)
                        DO UPDATE SET last_seen_at=CURRENT_TIMESTAMP
                        """,
                        (
                            client_id,
                            campaign,
                            source or f"quiz:{campaign}",
                            referrer_id or None,
                        ),
                    )
                else:
                    client_id, is_new_client, _ = find_or_create_quiz_client(
                        conn, campaign=campaign, phone_raw=phone, username=username, name=name, nickname=nickname,
                        email=email, telegram_user_id=telegram_user_id, source=source, referrer_id=referrer_id,
                    )
                active = conn.execute(
                    "SELECT * FROM quiz_attempts WHERE client_id=? AND campaign_code=? AND campaign_version=? AND status='in_progress' ORDER BY id DESC LIMIT 1",
                    (client_id, campaign, campaign_version),
                ).fetchone()
                if active and active["attempt_deadline_at"] and datetime.fromisoformat(active["attempt_deadline_at"]) <= now:
                    conn.execute("UPDATE quiz_attempts SET status='expired', finished_at=?, last_activity_at=? WHERE id=?", (quiz_timestamp(now), quiz_timestamp(now), active["id"]))
                    active = None
                token = secrets.token_urlsafe(32)
                token_hash = attempt_token_hash(settings.secret_key, token)
                if active:
                    conn.execute("UPDATE quiz_attempts SET token_hash=?, last_activity_at=? WHERE id=?", (token_hash, quiz_timestamp(now), active["id"]))
                    active = conn.execute("SELECT * FROM quiz_attempts WHERE id=?", (active["id"],)).fetchone()
                    device_token = issue_or_refresh_device(
                        conn,
                        secret_key=settings.secret_key,
                        client_id=client_id,
                        current_token=quiz_device_cookie(request),
                    )
                    if verified:
                        request.session["quiz_verified_identity"] = {
                            "campaign": campaign,
                            "method": identity_method,
                            "client_id": client_id,
                        }
                    api_response = JSONResponse(
                        attempt_response(
                            active,
                            token,
                            resumed=True,
                            campaign_type=campaign_row["campaign_type"],
                        )
                    )
                    set_quiz_device_cookie(api_response, device_token)
                    return api_response
                summary = conn.execute(
                    "SELECT * FROM quiz_participation_versions WHERE client_id=? AND campaign_code=? AND campaign_version=?",
                    (client_id, campaign, campaign_version),
                ).fetchone()
                attempts_used = int(summary["attempts_used"]) if summary else 0
                if summary and summary["successful"]:
                    raise HTTPException(status_code=409, detail="Этот квиз уже успешно пройден")
                max_attempts = max(1, int(campaign_row["max_attempts"] or 3))
                if attempts_used >= max_attempts:
                    raise HTTPException(status_code=429, detail="Лимит попыток для этого квиза исчерпан")
                recent_attempts = conn.execute(
                    "SELECT COUNT(*) FROM quiz_attempts WHERE ip_hash=? AND created_at >= datetime('now', '-1 hour')",
                    (ip_hash,),
                ).fetchone()[0]
                if recent_attempts >= 10:
                    raise HTTPException(status_code=429, detail="Слишком много попыток. Попробуйте позже")
                questions = load_db_questions(conn, campaign)
                if campaign_row["campaign_type"] == "daily_414":
                    try:
                        validate_daily_questions(questions, campaign)
                    except ValueError as exc:
                        messages = {
                            "daily_414_requires_ten_questions": (
                                "Для выпуска 4:14 нужно опубликовать ровно 10 вопросов"
                            ),
                            "daily_414_requires_own_questions": (
                                "Добавьте 10 собственных вопросов выпуска 4:14"
                            ),
                        }
                        raise HTTPException(
                            status_code=409,
                            detail=messages.get(str(exc), "Выпуск 4:14 не готов"),
                        ) from exc
                deadline = now + timedelta(seconds=seconds) if seconds > 0 else None
                cursor = conn.execute(
                """
                INSERT INTO quiz_attempts(
                    campaign_code, campaign_version, client_id, attempt_number, identity_method, is_new_client,
                    quiz_referrer_id, source, token_hash, questions_snapshot_json, answers_json, current_index,
                    status, question_started_at, question_deadline_at, attempt_deadline_at, ip_hash, user_agent, last_activity_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', 0, 'in_progress', ?, NULL, ?, ?, ?, ?)
                """,
                (
                    campaign, campaign_version, client_id, attempts_used + 1, identity_method, int(is_new_client), referrer_id or None,
                    source or None, token_hash, json.dumps(questions, ensure_ascii=False, sort_keys=True),
                    quiz_timestamp(now), quiz_timestamp(deadline) if deadline else None, ip_hash, user_agent, quiz_timestamp(now),
                ),
                )
                conn.execute(
                    """
                    INSERT INTO quiz_participation_versions(client_id, campaign_code, campaign_version, attempts_used, last_attempt_at)
                    VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(client_id, campaign_code, campaign_version) DO UPDATE SET
                        attempts_used=attempts_used+1, last_attempt_at=excluded.last_attempt_at, updated_at=CURRENT_TIMESTAMP
                    """,
                    (client_id, campaign, campaign_version, quiz_timestamp(now)),
                )
                conn.execute(
                    """
                    INSERT INTO quiz_participation_summary(client_id, campaign_code, attempts_used, last_attempt_at)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(client_id, campaign_code) DO UPDATE SET
                        attempts_used=attempts_used+1, last_attempt_at=excluded.last_attempt_at, updated_at=CURRENT_TIMESTAMP
                    """,
                    (client_id, campaign, quiz_timestamp(now)),
                )
                attempt = conn.execute("SELECT * FROM quiz_attempts WHERE id=?", (cursor.lastrowid,)).fetchone()
                response = attempt_response(
                    attempt,
                    token,
                    resumed=False,
                    campaign_type=campaign_row["campaign_type"],
                )
                response["time_limit_seconds"] = seconds
                response["max_attempts"] = max_attempts
                response["attempts_left"] = max_attempts - attempts_used - 1
                device_token = issue_or_refresh_device(
                    conn,
                    secret_key=settings.secret_key,
                    client_id=client_id,
                    current_token=quiz_device_cookie(request),
                )
                if verified:
                    request.session["quiz_verified_identity"] = {
                        "campaign": campaign,
                        "method": identity_method,
                        "client_id": client_id,
                    }
                api_response = JSONResponse(response)
                set_quiz_device_cookie(api_response, device_token)
                return api_response
        except ValueError as exc:
            messages = {
                "phone_invalid": "Проверьте номер телефона",
                "username_invalid": "Проверьте Telegram username",
                "identity_required": "Укажите номер телефона или Telegram username",
                "identity_conflict": "Телефон и Telegram относятся к разным карточкам. Обратитесь к администратору",
                "email_invalid": "Проверьте адрес электронной почты",
            }
            raise HTTPException(status_code=422, detail=messages.get(str(exc), "Не удалось определить участника"))

    @app.post("/api/quiz/answer")
    async def quiz_answer(request: Request):
        payload = await request_json(request)
        token = str(payload.get("attempt_token", ""))
        if len(token) < 32:
            raise HTTPException(status_code=404, detail="Попытка не найдена")
        token_hash = attempt_token_hash(settings.secret_key, token)
        with transaction(settings.db_path) as conn:
            attempt = conn.execute("SELECT * FROM quiz_attempts WHERE token_hash=?", (token_hash,)).fetchone()
            if not attempt or attempt["status"] != "in_progress":
                raise HTTPException(status_code=409, detail="Эта попытка уже завершена")
            campaign_type = conn.execute(
                "SELECT campaign_type FROM quiz_campaigns WHERE code=?",
                (attempt["campaign_code"],),
            ).fetchone()
            is_daily_414 = bool(
                campaign_type and campaign_type["campaign_type"] == "daily_414"
            )
            questions = json.loads(attempt["questions_snapshot_json"])
            question_id = str(payload.get("question_id", ""))
            question = next((item for item in questions if item["id"] == question_id), None)
            if not question:
                raise HTTPException(status_code=422, detail="Вопрос не найден в этой попытке")
            question_index = next(
                index
                for index, item in enumerate(questions)
                if item["id"] == question_id
            )
            if is_daily_414 and question_index != int(attempt["current_index"] or 0):
                raise HTTPException(
                    status_code=409,
                    detail="В 4:14 вопросы проходят только по порядку",
                )
            now = utc_now()
            deadline = datetime.fromisoformat(attempt["attempt_deadline_at"]) if attempt["attempt_deadline_at"] else None
            if deadline and now > deadline + timedelta(seconds=1):
                raise HTTPException(status_code=409, detail="Время теста закончилось")
            try:
                value = validate_answers([question], {question["id"]: payload.get("answer")})[question["id"]]
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="Выберите ответ") from exc
            answers = json.loads(attempt["answers_json"])
            if is_daily_414 and question["id"] in answers:
                if answers[question["id"]] != value:
                    raise HTTPException(
                        status_code=409,
                        detail="В 4:14 сохранённый ответ нельзя изменить",
                    )
                return {
                    "ok": True,
                    "question_id": question_id,
                    "saved": True,
                }
            answers[question["id"]] = value
            conn.execute(
                "UPDATE quiz_attempts SET answers_json=?, current_index=MAX(current_index, ?), last_activity_at=? WHERE id=?",
                (json.dumps(answers, ensure_ascii=False, sort_keys=True), min(question_index + 1, len(questions) - 1), quiz_timestamp(now), attempt["id"]),
            )
        return {"ok": True, "question_id": question_id, "saved": True}

    @app.post("/api/quiz/finish")
    async def quiz_finish(request: Request):
        payload = await request_json(request)
        token = str(payload.get("attempt_token", ""))
        if len(token) < 32:
            raise HTTPException(status_code=404, detail="Попытка не найдена")
        token_hash = attempt_token_hash(settings.secret_key, token)
        now = utc_now()
        with transaction(settings.db_path) as conn:
            attempt = conn.execute("SELECT * FROM quiz_attempts WHERE token_hash=?", (token_hash,)).fetchone()
            if not attempt or attempt["status"] != "in_progress":
                raise HTTPException(status_code=409, detail="Эта попытка уже завершена")
            campaign_row = conn.execute(
                """
                SELECT qc.*, pt.title AS bonus_title FROM quiz_campaigns qc
                LEFT JOIN preference_types pt ON pt.code=qc.bonus_preference_code
                WHERE qc.code=?
                """,
                (attempt["campaign_code"],),
            ).fetchone()
            if not campaign_row or not attempt["client_id"]:
                raise HTTPException(status_code=409, detail="Данные попытки устарели")
            questions = json.loads(attempt["questions_snapshot_json"])
            answers = json.loads(attempt["answers_json"])
            for question in questions:
                if question["id"] not in answers:
                    answers[question["id"]] = [] if question["type"] == "multi_choice" else ""
            scoring = score_answers(questions, answers)
            pass_score = int(campaign_row["pass_score"] or 0)
            deadline = datetime.fromisoformat(attempt["attempt_deadline_at"]) if attempt["attempt_deadline_at"] else None
            timed_out = bool(deadline and now > deadline + timedelta(seconds=1))
            passed = bool(not timed_out and (pass_score <= 0 or scoring["score"] >= pass_score))
            client_row = conn.execute("SELECT * FROM clients WHERE id=?", (attempt["client_id"],)).fetchone()
            is_daily_414 = campaign_row["campaign_type"] == "daily_414"
            started_at = datetime.fromisoformat(
                attempt["question_started_at"] or attempt["created_at"]
            )
            completion_time_ms = (
                elapsed_milliseconds(started_at=started_at, finished_at=now)
                if is_daily_414
                else None
            )
            started_local = started_at.astimezone(campaign_timezone).replace(
                tzinfo=None
            )
            local_finished_at = now.astimezone(campaign_timezone).replace(
                tzinfo=None
            )
            prize_eligible = bool(
                is_daily_414
                and final_table_candidate_eligible(
                    campaign_row,
                    started_at=started_local,
                    finished_at=local_finished_at,
                    timed_out=timed_out,
                )
            )
            bonus_eligible = bool(
                not is_daily_414
                and passed
                and campaign_row["bonus_preference_code"]
                and int(campaign_row["bonus_amount"] or 0) > 0
            )
            cursor = conn.execute(
                """
                INSERT INTO quiz_submissions(
                    attempt_id, campaign_code, campaign_version, client_id, phone_raw, phone_local, name, username, nickname,
                    answers_json, questions_snapshot_json, score, max_score, correct_count, max_correct_count, passed,
                    bonus_granted, bonus_pending, bonus_type, is_duplicate, is_new_client,
                    quiz_referrer_id, source, completion_time_ms, main_prize_eligible,
                    user_agent, ip_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt["id"], attempt["campaign_code"], attempt["campaign_version"], attempt["client_id"], client_row["phone_raw"] or "",
                    client_row["phone_local"] or "", client_row["first_name"], client_row["username"], client_row["nickname"],
                    json.dumps(answers, ensure_ascii=False, sort_keys=True),
                    json.dumps(questions, ensure_ascii=False, sort_keys=True), scoring["score"], scoring["max_score"],
                    scoring["correct_count"], scoring["max_correct_count"], int(passed), int(bonus_eligible),
                    campaign_row["bonus_preference_code"], attempt["is_new_client"], attempt["quiz_referrer_id"],
                    attempt["source"], completion_time_ms, int(prize_eligible),
                    attempt["user_agent"], attempt["ip_hash"],
                ),
            )
            submission_id = int(cursor.lastrowid)
            daily_award = None
            daily_place = None
            final_table_start_iso = None
            final_questions: list[dict[str, Any]] = []
            if is_daily_414:
                final_start_local = daily_final_table_starts_at(campaign_row)
                if final_start_local:
                    final_start_utc = final_start_local.replace(
                        tzinfo=campaign_timezone
                    ).astimezone(timezone.utc)
                    final_table_start_iso = final_start_utc.isoformat(
                        timespec="milliseconds"
                    )
                    final_questions = load_final_questions(
                        conn, attempt["campaign_code"]
                    )
                    ensure_final_table(
                        conn,
                        campaign_code=attempt["campaign_code"],
                        campaign_version=int(attempt["campaign_version"] or 1),
                        starts_at=final_start_utc,
                        questions=final_questions,
                    )
                daily_award = award_daily_jackcoin(
                    conn,
                    client_id=int(attempt["client_id"]),
                    submission_id=submission_id,
                    issue_day=daily_issue_date(
                        campaign_row,
                        local_finished_at=local_finished_at,
                    ),
                    correct_count=int(scoring["correct_count"]),
                    max_correct_count=int(scoring["max_correct_count"]),
                    jackcoin_per_correct=int(campaign_row["jackcoin_per_correct"]),
                    jackcoin_completion_bonus=int(campaign_row["jackcoin_completion_bonus"]),
                    jackcoin_perfect_bonus=int(campaign_row["jackcoin_perfect_bonus"]),
                )
                conn.execute(
                    """
                    UPDATE quiz_submissions
                    SET jackcoin_awarded=?, streak_days=?
                    WHERE id=?
                    """,
                    (
                        daily_award["total"],
                        daily_award["streak_days"],
                        submission_id,
                    ),
                )
                if prize_eligible:
                    ranked = rank_final_candidates(
                        conn.execute(
                            """
                            SELECT id, client_id, correct_count, completion_time_ms
                            FROM quiz_submissions
                            WHERE campaign_code=? AND campaign_version=?
                              AND main_prize_eligible=1
                            """,
                            (
                                attempt["campaign_code"],
                                attempt["campaign_version"],
                            ),
                        ).fetchall()
                    )
                    daily_place = next(
                        (
                            item["place"]
                            for item in ranked
                            if item["submission_id"] == submission_id
                        ),
                        None,
                    )
            reward = issue_reward(
                conn, client_id=int(attempt["client_id"]), campaign=campaign_row,
                submission_id=submission_id, timezone_name=settings.timezone_name,
                campaign_version=int(attempt["campaign_version"] or 1),
            ) if bonus_eligible else None
            referral_reward = None
            referral_count = 0
            referral_code = str(attempt["quiz_referrer_id"] or "")
            if campaign_row["referral_enabled"] and referral_code:
                referral_owner = conn.execute(
                    "SELECT * FROM quiz_referral_codes WHERE code=? AND campaign_code=?",
                    (referral_code, attempt["campaign_code"]),
                ).fetchone()
                if referral_owner and int(referral_owner["client_id"]) != int(attempt["client_id"]):
                    referral_cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO quiz_referrals(
                            campaign_code, referrer_client_id, invited_client_id, referral_code_id, submission_id
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (attempt["campaign_code"], referral_owner["client_id"], attempt["client_id"], referral_owner["id"], submission_id),
                    )
                    referral_count = int(conn.execute(
                        "SELECT COUNT(*) FROM quiz_referrals WHERE campaign_code=? AND referrer_client_id=?",
                        (attempt["campaign_code"], referral_owner["client_id"]),
                    ).fetchone()[0])
                    threshold = max(1, int(campaign_row["referral_threshold"] or 1))
                    milestone = referral_count // threshold
                    max_rewards = max(0, int(campaign_row["referral_max_rewards"] or 0))
                    eligible_milestone = bool(
                        referral_cursor.rowcount and referral_count % threshold == 0 and milestone >= 1
                        and (campaign_row["referral_repeatable"] or milestone == 1)
                        and (max_rewards == 0 or milestone <= max_rewards)
                    )
                    if eligible_milestone:
                        referral_reward = issue_referral_reward(
                            conn, client_id=int(referral_owner["client_id"]), campaign=campaign_row,
                            submission_id=submission_id, milestone=milestone, timezone_name=settings.timezone_name,
                        )
                        if referral_reward:
                            conn.execute(
                                "UPDATE quiz_referrals SET reward_id=? WHERE id=(SELECT MAX(id) FROM quiz_referrals WHERE campaign_code=? AND invited_client_id=?)",
                                (referral_reward["id"], attempt["campaign_code"], attempt["client_id"]),
                            )
            own_referral = None
            if campaign_row["referral_enabled"]:
                own_referral = conn.execute(
                    "SELECT * FROM quiz_referral_codes WHERE client_id=? AND campaign_code=?",
                    (attempt["client_id"], attempt["campaign_code"]),
                ).fetchone()
                if not own_referral:
                    for _ in range(20):
                        candidate = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
                        try:
                            conn.execute(
                                "INSERT INTO quiz_referral_codes(code, client_id, campaign_code) VALUES (?, ?, ?)",
                                (candidate, attempt["client_id"], attempt["campaign_code"]),
                            )
                            break
                        except sqlite3.IntegrityError:
                            continue
                    own_referral = conn.execute(
                        "SELECT * FROM quiz_referral_codes WHERE client_id=? AND campaign_code=?",
                        (attempt["client_id"], attempt["campaign_code"]),
                    ).fetchone()
            conn.execute(
                """
                UPDATE quiz_participation_versions SET successful=MAX(successful, ?),
                    reward_issued=MAX(reward_issued, ?), completed_at=CASE WHEN ?=1 THEN ? ELSE completed_at END,
                    updated_at=CURRENT_TIMESTAMP WHERE client_id=? AND campaign_code=? AND campaign_version=?
                """,
                (int(passed), int(bool(reward)), int(passed), quiz_timestamp(now), attempt["client_id"],
                 attempt["campaign_code"], attempt["campaign_version"]),
            )
            conn.execute(
                """
                UPDATE quiz_attempts SET answers_json=?, current_index=?, status='submitted',
                    completed_questions_at=?, finished_at=?, last_activity_at=? WHERE id=?
                """,
                (json.dumps(answers, ensure_ascii=False, sort_keys=True), len(questions), quiz_timestamp(now), quiz_timestamp(now), quiz_timestamp(now), attempt["id"]),
            )
            conn.execute(
                """
                UPDATE quiz_participation_summary SET successful=MAX(successful, ?),
                    reward_issued=MAX(reward_issued, ?), completed_at=CASE WHEN ?=1 THEN ? ELSE completed_at END,
                    updated_at=CURRENT_TIMESTAMP WHERE client_id=? AND campaign_code=?
                """,
                (int(passed), int(bool(reward)), int(passed), quiz_timestamp(now), attempt["client_id"], attempt["campaign_code"]),
            )
            summary = conn.execute(
                "SELECT * FROM quiz_participation_versions WHERE client_id=? AND campaign_code=? AND campaign_version=?",
                (attempt["client_id"], attempt["campaign_code"], attempt["campaign_version"]),
            ).fetchone()
        attempts_used = int(summary["attempts_used"])
        max_attempts = int(campaign_row["max_attempts"] or 3)
        values = {
            "bonus": campaign_row["bonus_title"] or campaign_row["bonus_preference_code"] or "",
            "score": scoring["score"], "max_score": scoring["max_score"],
            "correct_count": scoring["correct_count"], "max_correct_count": scoring["max_correct_count"],
            "attempts_used": attempts_used, "max_attempts": max_attempts,
        }
        if is_daily_414:
            outcome = "completed"
            title = "Раздача завершена"
            message = (
                f"{scoring['correct_count']} из {scoring['max_correct_count']} "
                f"правильно · +{daily_award['total']} JACKCOIN"
            )
        elif reward:
            outcome = "won"
            title = render_campaign_text(campaign_row["victory_title"], values)
            message = render_campaign_text(campaign_row["victory_text"], values)
        elif not passed:
            outcome = "not_won"
            title = render_campaign_text(campaign_row["failure_title"], values)
            message = render_campaign_text(campaign_row["failure_text"], values)
        else:
            outcome = "completed"
            title = render_campaign_text(campaign_row["completion_title"], values)
            message = render_campaign_text(campaign_row["completion_text"], values)
        return {
            "ok": True, "completed": True, "submission_id": submission_id, "outcome": outcome,
            "campaign_version": int(attempt["campaign_version"] or 1),
            "title": title, "message": message, "passed": passed, "score": scoring["score"],
            "max_score": scoring["max_score"], "correct_count": scoring["correct_count"],
            "max_correct_count": scoring["max_correct_count"], "attempts_used": attempts_used,
            "max_attempts": max_attempts, "attempts_left": max(0, max_attempts - attempts_used),
            "retry_allowed": bool(not passed and attempts_used < max_attempts),
            "timed_out": timed_out,
            "reward_code": reward["code"] if reward and campaign_row["reward_delivery_mode"] == "code" else None,
            "reward_valid_from": reward["valid_from"] if reward and campaign_row["reward_delivery_mode"] == "code" else None,
            "reward_valid_until": reward["valid_until"] if reward and campaign_row["reward_delivery_mode"] == "code" else None,
            "bonus_message": (
                f"Покажи код администратору: {reward['code']}"
                if reward and campaign_row["reward_delivery_mode"] == "code"
                else f"{campaign_row['bonus_title'] or 'Бонус'} уже начислен на твою карточку"
                if reward else None
            ),
            "bonus_granted": bool(reward and campaign_row["reward_delivery_mode"] == "automatic"),
            "campaign_type": campaign_row["campaign_type"],
            "completion_time_ms": completion_time_ms,
            "main_prize_eligible": prize_eligible,
            "daily_place": daily_place,
            "final_table_starts_at": final_table_start_iso,
            "final_table_available": bool(
                is_daily_414 and final_table_start_iso and final_questions
            ),
            "jackcoin_awarded": daily_award["total"] if daily_award else 0,
            "jackcoin_breakdown": daily_award,
            "streak_days": daily_award["streak_days"] if daily_award else 0,
            "share_url": (
                f"{(settings.public_base_url if is_daily_414 else settings.quiz_public_base_url).rstrip('/')}/quiz?campaign={quote(attempt['campaign_code'])}&ref={quote(own_referral['code'])}"
                if own_referral else None
            ),
            "referral_count": referral_count,
            "referral_reward_issued": bool(referral_reward),
        }

    def daily_final_start_utc(campaign_row: sqlite3.Row) -> datetime:
        local_start = daily_final_table_starts_at(campaign_row)
        if not local_start:
            raise HTTPException(
                status_code=409,
                detail="Для выпуска не задано время начала",
            )
        return local_start.replace(tzinfo=campaign_timezone).astimezone(
            timezone.utc
        )

    @app.get("/api/quiz/final-table/status")
    async def daily_final_table_status(
        request: Request, campaign: str = "default"
    ):
        campaign = normalize_campaign(campaign)
        campaign_row = quiz_campaign_for_final(campaign)
        member = daily_member(request, campaign_row, required=True)
        now = utc_now()
        campaign_version = max(1, int(campaign_row["current_version"] or 1))
        final_start = daily_final_start_utc(campaign_row)
        client_id = int(member["client_id"])
        base = {
            "ok": True,
            "campaign": campaign,
            "campaign_version": campaign_version,
            "server_now": now.isoformat(timespec="milliseconds"),
            "starts_at": final_start.isoformat(timespec="milliseconds"),
            "entry_window_seconds": DAILY_414_ENTRY_WINDOW_SECONDS,
            "question_seconds": DAILY_414_FINAL_QUESTION_SECONDS,
            "table_size": DAILY_414_FINAL_TABLE_SIZE,
        }

        def build_status() -> dict[str, Any]:
            with connect(settings.db_path) as conn:
                submission = conn.execute(
                    """
                    SELECT * FROM quiz_submissions
                    WHERE campaign_code=? AND campaign_version=? AND client_id=?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (campaign, campaign_version, client_id),
                ).fetchone()
                if not submission:
                    return {
                        **base,
                        "state": "main_round",
                        "message": "Сначала завершите основной раунд из 10 вопросов.",
                    }
                table = conn.execute(
                    """
                    SELECT * FROM daily_414_final_tables
                    WHERE campaign_code=? AND campaign_version=?
                    """,
                    (campaign, campaign_version),
                ).fetchone()

            try:
                with connect(settings.db_path) as conn:
                    final_questions = load_final_questions(conn, campaign)
            except ValueError:
                final_questions = []
            except Exception:
                final_questions = []

            mutate = table is None or final_table_needs_reconcile(
                table, now=now, schedule_starts_at=final_start
            ) or (
                table is not None
                and table["status"] in {"waiting", "unavailable"}
                and bool(final_questions)
                and str(table["questions_snapshot_json"] or "").strip() in {"", "[]"}
            ) or (
                table is not None
                and table["status"] == "completed"
                and table["winner_submission_id"]
                and not table["winner_reward_id"]
                and table["prize_catalog_reward_id"]
                and not table["winner_reward_error"]
            )
            if mutate:
                with transaction(settings.db_path) as conn:
                    table = ensure_final_table(
                        conn,
                        campaign_code=campaign,
                        campaign_version=campaign_version,
                        starts_at=final_start,
                        questions=final_questions,
                        prize_catalog_reward_id=campaign_row[
                            "final_prize_catalog_reward_id"
                        ],
                    )
                    if final_table_needs_reconcile(
                        table, now=now, schedule_starts_at=final_start
                    ):
                        table = reconcile_final_table(
                            conn,
                            final_table_id=int(table["id"]),
                            now=now,
                            schedule_starts_at=final_start,
                        )
                    if table["status"] == "completed" and table["winner_submission_id"]:
                        attach_final_table_reward(
                            conn, final_table_id=int(table["id"]), now=now
                        )
                        table = conn.execute(
                            "SELECT * FROM daily_414_final_tables WHERE id=?",
                            (table["id"],),
                        ).fetchone()
            elif table is None:
                raise HTTPException(
                    status_code=409, detail="Финальный стол ещё не сформирован"
                )

            if table is None:
                raise HTTPException(
                    status_code=500, detail="Не удалось создать финальный стол"
                )

            if table["status"] == "unavailable":
                return {
                    **base,
                    "state": "unavailable",
                    "message": (
                        "Финальные вопросы ещё не опубликованы. "
                        "Результат основного раунда и JACKCOIN сохранены."
                    ),
                }

            with connect(settings.db_path) as conn:
                submission = conn.execute(
                    """
                    SELECT * FROM quiz_submissions
                    WHERE campaign_code=? AND campaign_version=? AND client_id=?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (campaign, campaign_version, client_id),
                ).fetchone()
                candidate = bool(submission["main_prize_eligible"])
                provisional_place = None
                ranked = rank_final_candidates(
                    conn.execute(
                        """
                        SELECT id, client_id, correct_count, completion_time_ms
                        FROM quiz_submissions
                        WHERE campaign_code=? AND campaign_version=?
                          AND main_prize_eligible=1
                        """,
                        (campaign, campaign_version),
                    ).fetchall()
                )
                standings = [
                    {
                        "place": item["place"],
                        "correct_count": item["correct_count"],
                        "is_you": item["client_id"] == client_id,
                    }
                    for item in ranked[:10]
                ]
                if candidate:
                    provisional_place = next(
                        (
                            item["place"]
                            for item in ranked
                            if item["client_id"] == client_id
                        ),
                        None,
                    )
                lobby_stats = {
                    "correct_count": int(submission["correct_count"] or 0),
                    "max_correct_count": int(submission["max_correct_count"] or 0),
                    "completion_time_ms": submission["completion_time_ms"],
                    "jackcoin_awarded": int(submission["jackcoin_awarded"] or 0),
                    "standings": standings,
                }
                if now < final_start:
                    return {
                        **base,
                        "state": "lobby" if candidate else "not_eligible",
                        "candidate": candidate,
                        "provisional_place": provisional_place,
                        **lobby_stats,
                        "message": (
                            "Основной раунд завершён. Ждём финальный стол."
                            if candidate
                            else (
                                "Отбор за финальный стол уже завершён. "
                                "JACKCOIN и награда за основной квиз сохранены."
                            )
                        ),
                    }

                finalist = conn.execute(
                    """
                    SELECT * FROM daily_414_finalists
                    WHERE final_table_id=? AND client_id=?
                    """,
                    (table["id"], client_id),
                ).fetchone()
                if not finalist:
                    return {
                        **base,
                        "state": "not_qualified" if candidate else "not_eligible",
                        "candidate": candidate,
                        "provisional_place": provisional_place,
                        "message": (
                            "В этот раз результат не вошёл в топ-10 финального стола."
                            if candidate
                            else (
                                "Вы прошли основной квиз после закрытия отбора "
                                "и не участвуете в финальном столе."
                            )
                        ),
                    }

                active_count = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM daily_414_finalists
                        WHERE final_table_id=? AND status='active'
                        """,
                        (table["id"],),
                    ).fetchone()[0]
                )
                if finalist["status"] == "winner":
                    winner_reward = None
                    if table["winner_reward_id"]:
                        winner_reward = conn.execute(
                            """
                            SELECT vmr.code, vcr.title
                            FROM vault_member_rewards vmr
                            JOIN vault_catalog_rewards vcr
                              ON vcr.id=vmr.catalog_reward_id
                            WHERE vmr.id=?
                            """,
                            (table["winner_reward_id"],),
                        ).fetchone()
                    return {
                        **base,
                        "state": "winner",
                        "seed": int(finalist["seed"]),
                        "active_count": 1,
                        "reward_code": winner_reward["code"] if winner_reward else None,
                        "reward_title": winner_reward["title"] if winner_reward else None,
                        "message": (
                            f"Вы победили! Карта «{winner_reward['title']}» уже в THE VAULT."
                            if winner_reward
                            else "Вы победили за финальным столом!"
                        ),
                    }
                if finalist["status"] == "eliminated":
                    return {
                        **base,
                        "state": "eliminated",
                        "seed": int(finalist["seed"]),
                        "active_count": active_count,
                        "eliminated_question": int(
                            finalist["eliminated_question_index"] or 0
                        )
                        + 1,
                        "message": "Вы выбыли из финального стола.",
                    }
                if table["status"] == "completed":
                    return {
                        **base,
                        "state": "completed",
                        "seed": int(finalist["seed"]),
                        "active_count": active_count,
                        "message": (
                            "Финальные вопросы закончились. "
                            "Результат сохранён для ведущего."
                        ),
                    }

                try:
                    questions = json.loads(table["questions_snapshot_json"] or "[]")
                except json.JSONDecodeError as exc:
                    raise HTTPException(
                        status_code=500,
                        detail="Снимок финальных вопросов повреждён",
                    ) from exc
                if not isinstance(questions, list) or not questions:
                    return {
                        **base,
                        "state": "unavailable",
                        "message": (
                            "Финальные вопросы ещё не опубликованы. "
                            "Результат основного раунда и JACKCOIN сохранены."
                        ),
                    }
                question_index, question_start, question_deadline = (
                    final_question_window(table)
                )
                if question_index >= len(questions):
                    return {
                        **base,
                        "state": "completed",
                        "seed": int(finalist["seed"]),
                        "active_count": active_count,
                        "message": "Финальный стол завершён.",
                    }
                saved_answer = conn.execute(
                    """
                    SELECT id FROM daily_414_final_answers
                    WHERE finalist_id=? AND question_index=?
                    """,
                    (finalist["id"], question_index),
                ).fetchone()
                public = public_questions([questions[question_index]])
                if not public:
                    raise HTTPException(
                        status_code=500,
                        detail="Финальный вопрос имеет некорректный формат",
                    )
                question = public[0]
                question["final_number"] = question_index + 1
                return {
                    **base,
                    "state": "final_question",
                    "seed": int(finalist["seed"]),
                    "active_count": active_count,
                    "heads_up": active_count == 2,
                    "question_index": question_index,
                    "question_started_at": question_start.isoformat(
                        timespec="milliseconds"
                    ),
                    "question_deadline_at": question_deadline.isoformat(
                        timespec="milliseconds"
                    ),
                    "answered": bool(saved_answer),
                    "question": question,
                }

        last_error: Exception | None = None
        for attempt in range(5):
            try:
                return await run_in_threadpool(build_status)
            except HTTPException:
                raise
            except sqlite3.OperationalError as exc:
                last_error = exc
                if "locked" not in str(exc).lower() or attempt >= 4:
                    break
                await run_in_threadpool(time.sleep, 0.05 * (attempt + 1))
            except Exception as exc:
                logging.exception("final-table status failed for %s", campaign)
                raise HTTPException(
                    status_code=500,
                    detail=f"Ошибка финального стола: {exc}",
                ) from exc
        raise HTTPException(
            status_code=503,
            detail="Финальный стол обновляется, подождите пару секунд",
        ) from last_error

    @app.post("/api/quiz/final-table/answer")
    async def daily_final_table_answer(request: Request):
        payload = await request_json(request)
        campaign = normalize_campaign(payload.get("campaign"))
        campaign_row = quiz_campaign_for_final(campaign)
        member = daily_member(request, campaign_row, required=True)
        try:
            requested_index = int(payload.get("question_index"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail="Некорректный номер вопроса"
            ) from exc
        now = utc_now()
        campaign_version = max(1, int(campaign_row["current_version"] or 1))
        final_start = daily_final_start_utc(campaign_row)
        with transaction(settings.db_path) as conn:
            table = conn.execute(
                """
                SELECT * FROM daily_414_final_tables
                WHERE campaign_code=? AND campaign_version=?
                """,
                (campaign, campaign_version),
            ).fetchone()
            if not table:
                raise HTTPException(
                    status_code=409, detail="Финальный стол ещё не сформирован"
                )
            table = reconcile_final_table(
                conn,
                final_table_id=int(table["id"]),
                now=now,
                schedule_starts_at=final_start,
            )
            if table["status"] != "live":
                raise HTTPException(
                    status_code=409, detail="Сейчас ответ не принимается"
                )
            finalist = conn.execute(
                """
                SELECT * FROM daily_414_finalists
                WHERE final_table_id=? AND client_id=?
                """,
                (table["id"], int(member["client_id"])),
            ).fetchone()
            if not finalist or finalist["status"] != "active":
                raise HTTPException(
                    status_code=403, detail="Вы не участвуете в текущем вопросе"
                )
            question_index, question_start, question_deadline = (
                final_question_window(table)
            )
            if requested_index != question_index:
                raise HTTPException(
                    status_code=409, detail="Этот вопрос уже закрыт"
                )
            if now < question_start or now >= question_deadline:
                raise HTTPException(
                    status_code=409, detail="Время ответа закончилось"
                )
            questions = json.loads(table["questions_snapshot_json"] or "[]")
            if question_index >= len(questions):
                raise HTTPException(
                    status_code=409, detail="Финальные вопросы закончились"
                )
            question = questions[question_index]
            try:
                answer = validate_answers(
                    [question],
                    {question["id"]: payload.get("answer")},
                )[question["id"]]
            except ValueError as exc:
                raise HTTPException(
                    status_code=422, detail="Выберите ответ"
                ) from exc
            answer_json = json.dumps(answer, ensure_ascii=False, sort_keys=True)
            existing = conn.execute(
                """
                SELECT * FROM daily_414_final_answers
                WHERE finalist_id=? AND question_index=?
                """,
                (finalist["id"], question_index),
            ).fetchone()
            if existing:
                if existing["answer_json"] != answer_json:
                    raise HTTPException(
                        status_code=409,
                        detail="Сохранённый ответ нельзя изменить",
                    )
                return {"ok": True, "saved": True, "answered": True}
            scoring = score_answers(
                [question],
                {question["id"]: answer},
            )
            is_correct = int(scoring["correct_count"] == 1)
            response_time_ms = max(
                0, int((now - question_start).total_seconds() * 1000)
            )
            conn.execute(
                """
                INSERT INTO daily_414_final_answers(
                    final_table_id, finalist_id, question_index, question_code,
                    answer_json, is_correct, response_time_ms, answered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    table["id"],
                    finalist["id"],
                    question_index,
                    question["id"],
                    answer_json,
                    is_correct,
                    response_time_ms,
                    quiz_timestamp(now),
                ),
            )
            if is_correct:
                conn.execute(
                    """
                    UPDATE daily_414_finalists
                    SET final_correct_count=final_correct_count+1,
                        final_response_time_ms=final_response_time_ms+?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (response_time_ms, finalist["id"]),
                )
        return {"ok": True, "saved": True, "answered": True}

    @app.post("/api/quiz/submit")
    async def quiz_submit(request: Request):
        raise HTTPException(status_code=410, detail="Форма обновилась. Вернитесь на страницу квиза и начните снова")

    def quiz_result_filters(
        campaign: str, phone: str, username: str, bonus: str, date_from: str, date_to: str
    ) -> tuple[str, list[Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if campaign:
            clauses.append("qs.campaign_code = ?")
            params.append(campaign)
        if phone:
            clauses.append("qs.phone_local LIKE ?")
            params.append(f"%{''.join(ch for ch in phone if ch.isdigit())[-10:]}%")
        if username:
            clauses.append("qs.username LIKE ?")
            params.append(f"%{username.lstrip('@')}%")
        if bonus == "granted":
            clauses.append("qs.bonus_granted = 1")
        elif bonus == "pending":
            clauses.append("qs.bonus_pending = 1")
        elif bonus == "none":
            clauses.append("qs.bonus_granted = 0 AND qs.bonus_pending = 0")
        if date_from:
            clauses.append("date(qs.created_at) >= date(?)")
            params.append(date_from)
        if date_to:
            clauses.append("date(qs.created_at) <= date(?)")
            params.append(date_to)
        return " AND ".join(clauses), params

    def load_quiz_results(where: str, params: list[Any], limit: int = 500):
        with connect(settings.db_path) as conn:
            return conn.execute(
                f"""
                SELECT qs.*, qc.title AS campaign_title,
                       qc.campaign_type AS campaign_type,
                       qrc.code AS reward_code,
                       qrc.status AS reward_status, qrc.valid_until AS reward_valid_until
                FROM quiz_submissions qs
                LEFT JOIN quiz_campaigns qc ON qc.code = qs.campaign_code
                LEFT JOIN quiz_reward_codes qrc ON qrc.submission_id = qs.id AND qrc.reward_kind='quiz'
                WHERE {where} ORDER BY qs.id DESC LIMIT ?
                """,
                (*params, limit),
            ).fetchall()

    @app.get("/admin/quiz-results", response_class=HTMLResponse)
    async def quiz_results_page(
        request: Request,
        campaign: str = "", phone: str = "", username: str = "", bonus: str = "",
        date_from: str = "", date_to: str = "",
    ):
        require_auth(request)
        where, params = quiz_result_filters(campaign, phone, username, bonus, date_from, date_to)
        results = load_quiz_results(where, params)
        with connect(settings.db_path) as conn:
            campaigns = conn.execute("SELECT code, title FROM quiz_campaigns ORDER BY title").fetchall()
            referral_params: list[Any] = []
            referral_where = ""
            if campaign:
                referral_where = "WHERE qr.campaign_code=?"
                referral_params.append(campaign)
            referral_stats = conn.execute(
                f"""
                SELECT qr.referrer_client_id, qr.campaign_code, qc.title AS campaign_title,
                       c.first_name, c.nickname, c.username, c.phone_local, c.phone_raw,
                       COUNT(DISTINCT qr.id) AS completed_referrals,
                       COUNT(DISTINCT qrw.id) AS rewards_issued,
                       MAX(qr.created_at) AS last_referral_at
                FROM quiz_referrals qr
                JOIN clients c ON c.id=qr.referrer_client_id
                LEFT JOIN quiz_campaigns qc ON qc.code=qr.campaign_code
                LEFT JOIN quiz_reward_codes qrw ON qrw.client_id=qr.referrer_client_id
                    AND qrw.campaign_code=qr.campaign_code AND qrw.reward_kind='referral'
                {referral_where}
                GROUP BY qr.referrer_client_id, qr.campaign_code
                ORDER BY completed_referrals DESC, last_referral_at DESC
                LIMIT 200
                """,
                referral_params,
            ).fetchall()
        filters = {"campaign": campaign, "phone": phone, "username": username, "bonus": bonus, "date_from": date_from, "date_to": date_to}
        return templates.TemplateResponse(
            request, "quiz_results.html",
            context(request, results=results, campaigns=campaigns, filters=filters, referral_stats=referral_stats),
        )

    @app.get("/admin/quiz-results/{submission_id:int}", response_class=HTMLResponse)
    async def quiz_result_detail_page(request: Request, submission_id: int):
        require_auth(request)
        with connect(settings.db_path) as conn:
            row = conn.execute(
                """
                SELECT qs.*, qc.title AS campaign_title,
                       qc.campaign_type AS campaign_type,
                       pt.title AS bonus_title,
                       qrc.code AS reward_code, qrc.status AS reward_status,
                       qrc.valid_from AS reward_valid_from, qrc.valid_until AS reward_valid_until,
                       qrc.used_at AS reward_used_at
                FROM quiz_submissions qs
                LEFT JOIN quiz_campaigns qc ON qc.code=qs.campaign_code
                LEFT JOIN preference_types pt ON pt.code=qs.bonus_type
                LEFT JOIN quiz_reward_codes qrc ON qrc.submission_id=qs.id AND qrc.reward_kind='quiz'
                WHERE qs.id=?
                """,
                (submission_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Результат квиза не найден")
            snapshot = None
            if row["questions_snapshot_json"]:
                try:
                    snapshot = json.loads(row["questions_snapshot_json"])
                except json.JSONDecodeError:
                    snapshot = None
            if snapshot is None:
                try:
                    snapshot = load_db_questions(conn, row["campaign_code"])
                except ValueError:
                    snapshot = load_builder_questions(conn, row["campaign_code"])
                    if not snapshot and row["campaign_code"] != "default":
                        snapshot = load_builder_questions(conn, "default")
        return templates.TemplateResponse(
            request,
            "quiz_result_detail.html",
            context(request, result=row, answers=answer_summary(snapshot, row["answers_json"])),
        )

    def rewards_redirect(message: str, *, error: bool = False) -> RedirectResponse:
        return RedirectResponse(f"/admin/rewards?{'error' if error else 'ok'}={quote(message)}", status_code=303)

    def vault_admin_redirect(message: str, *, error: bool = False) -> RedirectResponse:
        parameter = "error" if error else "ok"
        return RedirectResponse(
            f"/admin/vault?{urlencode({parameter: message})}", status_code=303
        )

    def vault_inventory_value(value: str) -> int | None:
        clean = str(value or "").strip()
        if not clean:
            return None
        try:
            return int(clean)
        except ValueError as exc:
            raise ValueError("invalid_inventory") from exc

    @app.get("/admin/vault", response_class=HTMLResponse)
    async def vault_admin_page(
        request: Request,
        status: str = "",
        code: str = "",
        ok: str = "",
        error: str = "",
    ):
        require_auth(request)
        clauses = ["1=1"]
        params: list[Any] = []
        if status in {"active", "redeemed", "expired", "cancelled"}:
            clauses.append("vmr.status=?")
            params.append(status)
        if code.strip():
            clauses.append("vmr.code LIKE ?")
            params.append(f"%{code.strip().upper()}%")
        with transaction(settings.db_path) as conn:
            expire_vault_rewards(conn)
            catalog = conn.execute(
                """
                SELECT vcr.*, COUNT(vmr.id) AS allocated_count,
                       SUM(CASE WHEN vmr.status='active' THEN 1 ELSE 0 END) AS active_count,
                       SUM(CASE WHEN vmr.status='redeemed' THEN 1 ELSE 0 END) AS redeemed_count
                FROM vault_catalog_rewards vcr
                LEFT JOIN vault_member_rewards vmr
                  ON vmr.catalog_reward_id=vcr.id
                 AND vmr.status<>'cancelled'
                GROUP BY vcr.id
                ORDER BY vcr.position, vcr.id
                """
            ).fetchall()
            rewards = conn.execute(
                f"""
                SELECT vmr.*, vcr.title, vcr.category,
                       c.first_name, c.nickname, c.username,
                       c.phone_local, c.phone_raw
                FROM vault_member_rewards vmr
                JOIN vault_catalog_rewards vcr ON vcr.id=vmr.catalog_reward_id
                JOIN clients c ON c.id=vmr.client_id
                WHERE {' AND '.join(clauses)}
                ORDER BY vmr.id DESC LIMIT 500
                """,
                params,
            ).fetchall()
            events = conn.execute(
                "SELECT * FROM vault_reward_events ORDER BY id DESC LIMIT 150"
            ).fetchall()
            final_prize_errors = conn.execute(
                """
                SELECT dft.id, dft.campaign_code, dft.campaign_version,
                       dft.winner_reward_error, qc.title AS campaign_title,
                       vcr.title AS reward_title, qs.client_id,
                       c.first_name, c.nickname, c.username
                FROM daily_414_final_tables dft
                JOIN quiz_campaigns qc ON qc.code=dft.campaign_code
                LEFT JOIN vault_catalog_rewards vcr
                  ON vcr.id=dft.prize_catalog_reward_id
                LEFT JOIN quiz_submissions qs ON qs.id=dft.winner_submission_id
                LEFT JOIN clients c ON c.id=qs.client_id
                WHERE dft.winner_reward_error IS NOT NULL
                  AND dft.winner_reward_id IS NULL
                ORDER BY dft.id DESC
                """
            ).fetchall()
        return templates.TemplateResponse(
            request,
            "vault.html",
            context(
                request,
                catalog=catalog,
                rewards=rewards,
                events=events,
                final_prize_errors=final_prize_errors,
                filters={"status": status, "code": code},
                ok=ok,
                error=error,
            ),
        )

    @app.post("/api/vault/catalog/create")
    async def vault_catalog_create(
        request: Request,
        code: str = Form(...),
        title: str = Form(...),
        description: str = Form(""),
        category: str = Form("club"),
        price_jc: int = Form(...),
        validity_days: int = Form(30),
        inventory_total: str = Form(""),
        redeem_instructions: str = Form(""),
        position: int = Form(100),
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                reward = create_catalog_reward(
                    conn,
                    code=code,
                    title=title,
                    description=description,
                    category=category,
                    price_jc=price_jc,
                    validity_days=validity_days,
                    inventory_total=vault_inventory_value(inventory_total),
                    redeem_instructions=redeem_instructions,
                    position=position,
                    admin_id=int(request.session["admin_id"]),
                )
                audit(
                    conn,
                    admin_id=request.session["admin_id"],
                    admin_name=request.session["admin_name"],
                    action="create",
                    entity_type="vault_catalog_reward",
                    entity_id=int(reward["id"]),
                    details={
                        "code": reward["code"],
                        "price_jc": reward["price_jc"],
                        "inventory_total": reward["inventory_total"],
                    },
                )
        except ValueError as exc:
            messages = {
                "invalid_catalog_code": "Код должен содержать 3–50 строчных латинских символов, цифр, _ или -",
                "catalog_code_exists": "Награда с таким кодом уже существует",
                "invalid_title": "Проверьте название награды",
                "invalid_category": "Проверьте категорию",
                "invalid_price": "Стоимость JACKCOIN указана неверно",
                "invalid_validity": "Срок действия указан неверно",
                "invalid_inventory": "Тираж должен быть целым неотрицательным числом или пустым",
                "invalid_position": "Порядок указан неверно",
            }
            return vault_admin_redirect(
                messages.get(str(exc), "Не удалось создать награду"), error=True
            )
        return vault_admin_redirect(f"Награда «{reward['title']}» добавлена в каталог")

    @app.post("/api/vault/catalog/{reward_id:int}/update")
    async def vault_catalog_update(
        request: Request,
        reward_id: int,
        title: str = Form(...),
        description: str = Form(""),
        category: str = Form("club"),
        price_jc: int = Form(...),
        validity_days: int = Form(30),
        inventory_total: str = Form(""),
        redeem_instructions: str = Form(""),
        position: int = Form(100),
        is_active: bool = Form(False),
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                reward = update_catalog_reward(
                    conn,
                    reward_id=reward_id,
                    title=title,
                    description=description,
                    category=category,
                    price_jc=price_jc,
                    validity_days=validity_days,
                    inventory_total=vault_inventory_value(inventory_total),
                    redeem_instructions=redeem_instructions,
                    position=position,
                    is_active=is_active,
                )
                audit(
                    conn,
                    admin_id=request.session["admin_id"],
                    admin_name=request.session["admin_name"],
                    action="update",
                    entity_type="vault_catalog_reward",
                    entity_id=reward_id,
                    details={
                        "price_jc": reward["price_jc"],
                        "inventory_total": reward["inventory_total"],
                        "is_active": bool(reward["is_active"]),
                    },
                )
        except ValueError as exc:
            messages = {
                "catalog_reward_not_found": "Награда не найдена",
                "inventory_below_allocated": "Тираж нельзя сделать меньше количества уже выданных карт",
                "invalid_title": "Проверьте название награды",
                "invalid_category": "Проверьте категорию",
                "invalid_price": "Стоимость JACKCOIN указана неверно",
                "invalid_validity": "Срок действия указан неверно",
                "invalid_inventory": "Тираж должен быть целым неотрицательным числом или пустым",
                "invalid_position": "Порядок указан неверно",
            }
            return vault_admin_redirect(
                messages.get(str(exc), "Не удалось сохранить награду"), error=True
            )
        return vault_admin_redirect(f"Награда «{reward['title']}» обновлена")

    @app.post("/api/vault/redeem")
    async def vault_redeem(
        request: Request, code: str = Form(...), csrf_token: str = Form(...)
    ):
        require_auth(request, api=True)
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            expire_vault_rewards(conn)
        try:
            with transaction(settings.db_path) as conn:
                reward = redeem_vault_reward(
                    conn,
                    code=code,
                    admin_id=int(request.session["admin_id"]),
                    admin_name=request.session["admin_name"],
                )
                audit(
                    conn,
                    admin_id=request.session["admin_id"],
                    admin_name=request.session["admin_name"],
                    action="redeem",
                    entity_type="vault_member_reward",
                    entity_id=int(reward["id"]),
                    details={"code": reward["code"], "client_id": reward["client_id"]},
                )
        except ValueError as exc:
            messages = {
                "vault_reward_not_found": "Карта не найдена",
                "vault_reward_redeemed": "Эта карта уже сыграна",
                "vault_reward_expired": "Срок действия карты истёк",
                "vault_reward_cancelled": "Карта отменена",
                "vault_reward_not_started": "Срок действия карты ещё не начался",
            }
            return vault_admin_redirect(
                messages.get(str(exc), "Не удалось погасить карту"), error=True
            )
        return vault_admin_redirect(f"Карта {reward['code']} погашена")

    @app.post("/api/vault/final-tables/{final_table_id:int}/retry-prize")
    async def vault_retry_final_prize(
        request: Request, final_table_id: int, csrf_token: str = Form(...)
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            table = conn.execute(
                "SELECT * FROM daily_414_final_tables WHERE id=?",
                (final_table_id,),
            ).fetchone()
            if not table:
                return vault_admin_redirect("Финальный стол не найден", error=True)
            if table["winner_reward_id"]:
                return vault_admin_redirect("Главный приз уже выдан")
            conn.execute(
                "UPDATE daily_414_final_tables SET winner_reward_error=NULL WHERE id=?",
                (final_table_id,),
            )
            reward = attach_final_table_reward(
                conn, final_table_id=final_table_id, now=utc_now()
            )
            refreshed = conn.execute(
                "SELECT winner_reward_error FROM daily_414_final_tables WHERE id=?",
                (final_table_id,),
            ).fetchone()
            if not reward:
                message = {
                    "catalog_reward_not_found": "Награда удалена из каталога",
                    "catalog_reward_sold_out": "Тираж награды закончился",
                }.get(
                    str(refreshed["winner_reward_error"] or ""),
                    "Не удалось выдать главный приз",
                )
                return vault_admin_redirect(message, error=True)
            audit(
                conn,
                admin_id=request.session["admin_id"],
                admin_name=request.session["admin_name"],
                action="retry_final_prize",
                entity_type="daily_414_final_table",
                entity_id=final_table_id,
                details={"reward_id": reward["id"], "code": reward["code"]},
            )
        return vault_admin_redirect(f"Главный приз выдан: {reward['code']}")

    @app.post("/api/vault/rewards/{reward_id:int}/cancel")
    async def vault_cancel(
        request: Request, reward_id: int, csrf_token: str = Form(...)
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                reward = cancel_vault_reward(
                    conn,
                    reward_id=reward_id,
                    admin_id=int(request.session["admin_id"]),
                    admin_name=request.session["admin_name"],
                )
                audit(
                    conn,
                    admin_id=request.session["admin_id"],
                    admin_name=request.session["admin_name"],
                    action="cancel",
                    entity_type="vault_member_reward",
                    entity_id=reward_id,
                    details={
                        "code": reward["code"],
                        "jackcoin_refunded": reward["price_paid_jc"],
                    },
                )
        except ValueError as exc:
            messages = {
                "vault_reward_not_found": "Карта не найдена",
                "vault_reward_redeemed": "Сыгранную карту отменить нельзя",
                "vault_reward_expired": "Истёкшую карту отменить нельзя",
                "vault_reward_cancelled": "Карта уже отменена",
            }
            return vault_admin_redirect(
                messages.get(str(exc), "Не удалось отменить карту"), error=True
            )
        suffix = (
            f"; клиенту возвращено {reward['price_paid_jc']} JC"
            if reward["price_paid_jc"]
            else ""
        )
        return vault_admin_redirect(f"Карта {reward['code']} отменена{suffix}")

    @app.get("/admin/rewards", response_class=HTMLResponse)
    async def rewards_page(request: Request, status: str = "", code: str = "", ok: str = "", error: str = ""):
        require_auth(request)
        clauses = ["1=1"]
        params: list[Any] = []
        if status in {"issued", "used", "expired", "cancelled"}:
            clauses.append("qrc.status=?")
            params.append(status)
        if code.strip():
            clauses.append("qrc.code LIKE ?")
            params.append(f"%{code.strip().upper()}%")
        with transaction(settings.db_path) as conn:
            cleanup_quiz_data(
                conn, detail_days=settings.quiz_detail_retention_days,
                reward_days=settings.reward_retention_days,
                action_log_days=settings.action_log_retention_days,
            )
            rewards = conn.execute(
                f"""
                SELECT qrc.*, qc.title AS campaign_title, pt.title AS preference_title,
                       c.first_name, c.nickname, c.username, c.phone_local, c.phone_raw
                FROM quiz_reward_codes qrc
                JOIN clients c ON c.id=qrc.client_id
                LEFT JOIN quiz_campaigns qc ON qc.code=qrc.campaign_code
                LEFT JOIN preference_types pt ON pt.code=qrc.preference_code
                WHERE {' AND '.join(clauses)} ORDER BY qrc.id DESC LIMIT 500
                """,
                params,
            ).fetchall()
            events = conn.execute("SELECT * FROM quiz_reward_events ORDER BY id DESC LIMIT 100").fetchall()
        return templates.TemplateResponse(
            request, "rewards.html",
            context(request, rewards=rewards, events=events, filters={"status": status, "code": code}, ok=ok, error=error),
        )

    @app.post("/api/rewards/redeem")
    async def redeem_reward_code(request: Request, code: str = Form(...), csrf_token: str = Form(...)):
        require_auth(request, api=True)
        check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                reward = redeem_reward(
                    conn, code=code, admin_id=int(request.session["admin_id"]),
                    admin_name=request.session["admin_name"],
                )
                audit(
                    conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                    action="redeem", entity_type="quiz_reward", entity_id=int(reward["id"]),
                    details={"code": reward["code"], "campaign": reward["campaign_code"], "client_id": reward["client_id"]},
                )
        except ValueError as exc:
            messages = {
                "reward_not_found": "Код не найден", "reward_used": "Код уже использован",
                "reward_expired": "Срок действия кода истёк", "reward_cancelled": "Код отменён",
                "reward_not_started": "Срок действия кода ещё не начался",
                "reward_preference_unavailable": "Этот тип бонуса сейчас недоступен",
            }
            return rewards_redirect(messages.get(str(exc), "Не удалось применить код"), error=True)
        return rewards_redirect(f"Код {reward['code']} применён. Бонус начислен клиенту")

    @app.post("/api/rewards/{reward_id:int}/cancel")
    async def cancel_reward_code(request: Request, reward_id: int, csrf_token: str = Form(...)):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            reward = conn.execute("SELECT * FROM quiz_reward_codes WHERE id=?", (reward_id,)).fetchone()
            if not reward:
                return rewards_redirect("Код не найден", error=True)
            if reward["status"] != "issued":
                return rewards_redirect("Можно отменить только неиспользованный активный код", error=True)
            conn.execute("UPDATE quiz_reward_codes SET status='cancelled', cancelled_at=CURRENT_TIMESTAMP WHERE id=?", (reward_id,))
            conn.execute(
                "INSERT INTO quiz_reward_events(reward_id, code, client_id, campaign_code, action, admin_name) VALUES (?, ?, ?, ?, 'cancelled', ?)",
                (reward_id, reward["code"], reward["client_id"], reward["campaign_code"], request.session["admin_name"]),
            )
            audit(
                conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="cancel", entity_type="quiz_reward", entity_id=reward_id,
                details={"code": reward["code"], "campaign": reward["campaign_code"]},
            )
        return rewards_redirect(f"Код {reward['code']} отменён")

    @app.get("/api/quiz/results")
    async def quiz_results_api(
        request: Request,
        campaign: str = "", phone: str = "", username: str = "", bonus: str = "",
        date_from: str = "", date_to: str = "",
    ):
        require_auth(request, api=True)
        where, params = quiz_result_filters(campaign, phone, username, bonus, date_from, date_to)
        rows = load_quiz_results(where, params)
        return [{key: row[key] for key in row.keys() if key not in {"ip_hash", "user_agent"}} for row in rows]

    @app.get("/api/quiz/campaigns")
    async def quiz_campaigns_api(request: Request):
        require_auth(request, api=True)
        with connect(settings.db_path) as conn:
            rows = conn.execute("SELECT * FROM quiz_campaigns ORDER BY title").fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    @app.get("/api/quiz/stats")
    async def quiz_stats_api(request: Request):
        require_auth(request, api=True)
        with connect(settings.db_path) as conn:
            totals = conn.execute(
                """
                SELECT COUNT(*) AS submissions, COUNT(DISTINCT client_id) AS unique_people,
                    SUM(CASE WHEN is_duplicate=1 THEN 1 ELSE 0 END) AS duplicates,
                    (SELECT COUNT(*) FROM quiz_reward_codes WHERE status='used') AS bonuses_granted,
                    SUM(CASE WHEN is_new_client=1 THEN 1 ELSE 0 END) AS new_clients,
                    SUM(CASE WHEN passed=1 THEN 1 ELSE 0 END) AS passed,
                    COUNT(DISTINCT quiz_referrer_id) AS referrers
                FROM quiz_submissions
                """
            ).fetchone()
            campaigns = conn.execute(
                "SELECT campaign_code, COUNT(*) AS submissions FROM quiz_submissions GROUP BY campaign_code ORDER BY submissions DESC"
            ).fetchall()
        return {
            "totals": {key: totals[key] or 0 for key in totals.keys()},
            "campaigns": [{key: row[key] for key in row.keys()} for row in campaigns],
        }

    @app.get("/master", response_class=HTMLResponse)
    async def master_page(request: Request, ok: str = "", error: str = "", tab: str = "preferences"):
        require_master(request)
        if tab not in {"preferences", "admins", "campaigns", "audit"}:
            tab = "preferences"
        with connect(settings.db_path) as conn:
            preference_types = conn.execute(
                """
                SELECT pt.*, COUNT(cp.id) AS client_count,
                    COALESCE(SUM(CASE WHEN pt.kind='counter' THEN cp.balance_int ELSE 0 END), 0) AS total_balance
                FROM preference_types pt
                LEFT JOIN client_preferences cp ON cp.preference_type_id = pt.id
                GROUP BY pt.id ORDER BY pt.position, pt.id
                """
            ).fetchall()
            admins = conn.execute(
                "SELECT id, username, display_name, role, is_active, last_login_at, created_at FROM admins ORDER BY role DESC, display_name"
            ).fetchall()
            campaign_rows = conn.execute(
                """
                SELECT * FROM quiz_campaigns
                WHERE deleted_at IS NULL
                ORDER BY archived_at IS NOT NULL, title
                """
            ).fetchall()
            quiz_campaigns_classic = []
            quiz_campaigns_daily_414 = []
            quiz_campaigns_archived = []
            for campaign_row in campaign_rows:
                campaign_item = dict(campaign_row)
                campaign_item["schedule_state"] = campaign_schedule_state(campaign_row)
                if campaign_row["archived_at"]:
                    has_history = campaign_has_history(conn, campaign_row["code"])
                    campaign_item["delete_permanently"] = (
                        campaign_row["code"] not in BUILT_IN_CAMPAIGN_CODES
                        and not has_history
                    )
                    campaign_item["delete_note"] = (
                        "Системная кампания будет скрыта и не появится снова после перезапуска."
                        if campaign_row["code"] in BUILT_IN_CAMPAIGN_CODES
                        else "Кампания исчезнет из списка, а история участников и наград сохранится."
                        if has_history
                        else "Пустая кампания будет удалена навсегда вместе с вопросами."
                    )
                    quiz_campaigns_archived.append(campaign_item)
                elif campaign_row["campaign_type"] == "daily_414":
                    quiz_campaigns_daily_414.append(campaign_item)
                else:
                    quiz_campaigns_classic.append(campaign_item)
            counter_preferences = conn.execute(
                "SELECT code, title FROM preference_types WHERE kind='counter' ORDER BY position, id"
            ).fetchall()
            vault_catalog_rewards = conn.execute(
                """
                SELECT id, code, title, price_jc, is_active
                FROM vault_catalog_rewards
                ORDER BY is_active DESC, position, id
                """
            ).fetchall()
            admin_audit = conn.execute(
                "SELECT * FROM admin_audit_log ORDER BY id DESC LIMIT 100"
            ).fetchall()
        return templates.TemplateResponse(
            request,
            "master.html",
            context(
                request,
                preference_types=preference_types,
                admins=admins,
                quiz_campaigns_classic=quiz_campaigns_classic,
                quiz_campaigns_daily_414=quiz_campaigns_daily_414,
                quiz_campaigns_archived=quiz_campaigns_archived,
                counter_preferences=counter_preferences,
                vault_catalog_rewards=vault_catalog_rewards,
                admin_audit=admin_audit,
                current_tab=tab,
                quiz_public_base_url=settings.quiz_public_base_url.rstrip("/"),
                club_public_base_url=settings.public_base_url.rstrip("/"),
                campaign_timezone_name=settings.timezone_name,
                ok=ok,
                error=error,
            ),
        )

    def master_redirect(message: str, *, error: bool = False, tab: str = "preferences") -> RedirectResponse:
        parameter = "error" if error else "ok"
        return RedirectResponse(f"/master?tab={tab}&{parameter}={message}", status_code=303)

    @app.post("/api/master/preferences/create")
    async def create_preference(
        request: Request,
        title: str = Form(...),
        kind: str = Form(...),
        code: str = Form(""),
        position: int = Form(100),
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        title = title.strip()
        code = code.strip().lower() or f"custom_{uuid.uuid4().hex[:10]}"
        if len(title) < 2 or len(title) > 80:
            return master_redirect("Название должно содержать от 2 до 80 символов", error=True)
        if kind not in {"counter", "percent"}:
            return master_redirect("Некорректный тип преференции", error=True)
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,49}", code):
            return master_redirect("Код: 3–50 латинских символов, цифр или _", error=True)
        position = max(0, min(position, 9999))
        try:
            with transaction(settings.db_path) as conn:
                cursor = conn.execute(
                    "INSERT INTO preference_types(code, title, kind, position, created_by_admin_id) VALUES (?, ?, ?, ?, ?)",
                    (code, title, kind, position, request.session["admin_id"]),
                )
                audit(
                    conn,
                    admin_id=request.session["admin_id"],
                    admin_name=request.session["admin_name"],
                    action="create",
                    entity_type="preference_type",
                    entity_id=int(cursor.lastrowid),
                    details={"code": code, "title": title, "kind": kind, "position": position},
                )
        except sqlite3.IntegrityError:
            return master_redirect("Преференция с таким кодом уже существует", error=True)
        return master_redirect("Преференция создана")

    @app.post("/api/master/preferences/{preference_id}/update")
    async def update_preference(
        request: Request,
        preference_id: int,
        title: str = Form(...),
        position: int = Form(100),
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        title = title.strip()
        if len(title) < 2 or len(title) > 80:
            return master_redirect("Некорректное название", error=True)
        position = max(0, min(position, 9999))
        with transaction(settings.db_path) as conn:
            row = conn.execute("SELECT * FROM preference_types WHERE id = ?", (preference_id,)).fetchone()
            if not row:
                return master_redirect("Преференция не найдена", error=True)
            conn.execute(
                "UPDATE preference_types SET title = ?, position = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (title, position, preference_id),
            )
            audit(
                conn,
                admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="update", entity_type="preference_type", entity_id=preference_id,
                details={"old_title": row["title"], "new_title": title, "old_position": row["position"], "new_position": position},
            )
        return master_redirect("Преференция обновлена")

    @app.post("/api/master/preferences/{preference_id}/toggle")
    async def toggle_preference(request: Request, preference_id: int, csrf_token: str = Form(...)):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            row = conn.execute("SELECT * FROM preference_types WHERE id = ?", (preference_id,)).fetchone()
            if not row:
                return master_redirect("Преференция не найдена", error=True)
            new_state = 0 if row["is_active"] else 1
            conn.execute("UPDATE preference_types SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_state, preference_id))
            audit(
                conn,
                admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="activate" if new_state else "deactivate", entity_type="preference_type", entity_id=preference_id,
                details={"code": row["code"], "title": row["title"]},
            )
        return master_redirect("Преференция включена" if new_state else "Преференция отключена")

    @app.post("/api/master/admins/create")
    async def create_admin(
        request: Request,
        username: str = Form(...),
        display_name: str = Form(...),
        pin: str = Form(...),
        role: str = Form("admin"),
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        display_name = display_name.strip()
        try:
            username = validate_username(username)
            pin_hash = hash_pin(pin)
        except ValueError as exc:
            return master_redirect("Проверьте логин и PIN: логин от 3 символов, PIN от 4", error=True, tab="admins")
        if not display_name or len(display_name) > 80 or role not in {"admin", "master_admin"}:
            return master_redirect("Некорректные данные администратора", error=True, tab="admins")
        try:
            with transaction(settings.db_path) as conn:
                cursor = conn.execute(
                    "INSERT INTO admins(username, display_name, pin_hash, role) VALUES (?, ?, ?, ?)",
                    (username, display_name, pin_hash, role),
                )
                audit(
                    conn,
                    admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                    action="create", entity_type="admin", entity_id=int(cursor.lastrowid),
                    details={"username": username, "display_name": display_name, "role": role},
                )
        except sqlite3.IntegrityError:
            return master_redirect("Такой логин уже существует", error=True, tab="admins")
        return master_redirect("Администратор создан", tab="admins")

    @app.post("/api/master/admins/{admin_id}/toggle")
    async def toggle_admin(request: Request, admin_id: int, csrf_token: str = Form(...)):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        if admin_id == request.session.get("admin_id"):
            return master_redirect("Нельзя отключить собственный аккаунт", error=True, tab="admins")
        with transaction(settings.db_path) as conn:
            row = conn.execute("SELECT * FROM admins WHERE id = ?", (admin_id,)).fetchone()
            if not row:
                return master_redirect("Администратор не найден", error=True, tab="admins")
            new_state = 0 if row["is_active"] else 1
            if row["role"] == "master_admin" and not new_state:
                active_masters = conn.execute("SELECT COUNT(*) FROM admins WHERE role='master_admin' AND is_active=1").fetchone()[0]
                if active_masters <= 1:
                    return master_redirect("Нельзя отключить последнего мастер-администратора", error=True, tab="admins")
            conn.execute("UPDATE admins SET is_active = ?, session_version = session_version + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_state, admin_id))
            audit(
                conn,
                admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="activate" if new_state else "deactivate", entity_type="admin", entity_id=admin_id,
                details={"username": row["username"]},
            )
        return master_redirect("Администратор включён" if new_state else "Администратор отключён", tab="admins")

    @app.post("/api/master/admins/{admin_id}/reset-pin")
    async def reset_admin_pin(request: Request, admin_id: int, pin: str = Form(...), csrf_token: str = Form(...)):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        try:
            pin_hash = hash_pin(pin)
        except ValueError:
            return master_redirect("PIN должен содержать от 4 до 128 символов", error=True, tab="admins")
        with transaction(settings.db_path) as conn:
            row = conn.execute("SELECT username FROM admins WHERE id = ?", (admin_id,)).fetchone()
            if not row:
                return master_redirect("Администратор не найден", error=True, tab="admins")
            conn.execute("UPDATE admins SET pin_hash = ?, session_version = session_version + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (pin_hash, admin_id))
            audit(
                conn,
                admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="reset_pin", entity_type="admin", entity_id=admin_id,
                details={"username": row["username"]},
            )
        return master_redirect("PIN обновлён", tab="admins")

    def validate_campaign_bonus(conn, bonus_code: str, bonus_amount: int) -> tuple[str | None, int]:
        bonus_code = bonus_code.strip()
        if not bonus_code:
            return None, 0
        preference = conn.execute(
            "SELECT kind FROM preference_types WHERE code = ?",
            (bonus_code,),
        ).fetchone()
        if not preference or preference["kind"] != "counter":
            raise ValueError("Некорректная бонусная преференция")
        if bonus_amount < 1 or bonus_amount > 100:
            raise ValueError("Количество бонусов должно быть от 1 до 100")
        return bonus_code, bonus_amount

    def campaign_content_values(**values: str) -> dict[str, str]:
        defaults = {
            "welcome_kicker": "Короткий опрос клуба",
            "welcome_text": "Ответь на несколько вопросов — это займёт пару минут и поможет нам делать события интереснее.",
            "start_button_text": "Начать",
            "identity_text": "Укажи номер телефона или Telegram username, чтобы мы сохранили попытку и смогли продолжить её после закрытия страницы.",
            "victory_title": "Поздравляем!",
            "victory_text": "Отличная игра! Твой результат — {score} из {max_score}.",
            "failure_title": "Не расстраивайся",
            "failure_text": "Попробуй ещё раз — использовано попыток: {attempts_used} из {max_attempts}.",
            "completion_title": "Спасибо!",
            "completion_text": "Твои ответы сохранены.",
        }
        result = {}
        for key, default in defaults.items():
            value = str(values.get(key, "")).strip() or default
            if len(value) > 1000:
                raise ValueError("Текст квиза не должен превышать 1000 символов")
            result[key] = value
        return result

    def campaign_reward_values(
        mode: str, value: int, valid_from: str, valid_until: str
    ) -> tuple[str, int, str | None, str | None]:
        if mode not in {"end_of_day", "hours", "days", "fixed", "unlimited"}:
            raise ValueError("Проверьте срок действия награды")
        if mode in {"hours", "days"} and not 1 <= value <= 365:
            raise ValueError("Количество часов или дней должно быть от 1 до 365")
        start, end = validate_campaign_period(valid_from, valid_until)
        if mode == "fixed" and not end:
            raise ValueError("Для фиксированного срока укажите дату окончания")
        if mode != "fixed":
            start, end = None, None
        return mode, max(0, value), start, end

    def validate_final_prize(
        conn: sqlite3.Connection,
        *,
        campaign_type: str,
        catalog_reward_id: int,
    ) -> int | None:
        reward_id = max(0, int(catalog_reward_id or 0))
        if campaign_type != "daily_414" or reward_id == 0:
            return None
        reward = conn.execute(
            "SELECT id, is_active FROM vault_catalog_rewards WHERE id=?",
            (reward_id,),
        ).fetchone()
        if not reward:
            raise ValueError("Главный приз не найден в THE VAULT")
        if not reward["is_active"]:
            raise ValueError("Сначала включите выбранный главный приз в THE VAULT")
        return int(reward["id"])

    @app.post("/api/master/quiz-campaigns/create")
    async def create_quiz_campaign(
        request: Request,
        code: str = Form(...),
        title: str = Form(...),
        campaign_type: str = Form("classic"),
        bonus_preference_code: str = Form(""),
        bonus_amount: int = Form(0),
        reward_delivery_mode: str = Form("automatic"),
        pass_score: int = Form(0),
        quiz_time_limit_seconds: int = Form(120),
        max_attempts: int = Form(3),
        verification_required: bool = Form(False),
        jackcoin_per_correct: int = Form(5),
        jackcoin_completion_bonus: int = Form(10),
        jackcoin_perfect_bonus: int = Form(20),
        final_prize_catalog_reward_id: int = Form(0),
        referral_enabled: bool = Form(False),
        referral_preference_code: str = Form(""),
        referral_amount: int = Form(0),
        referral_delivery_mode: str = Form("automatic"),
        referral_threshold: int = Form(1),
        referral_repeatable: bool = Form(False),
        referral_max_rewards: int = Form(1),
        reward_validity_mode: str = Form("end_of_day"),
        reward_validity_value: int = Form(0),
        reward_valid_from: str = Form(""),
        reward_valid_until: str = Form(""),
        active_from: str = Form(""),
        active_until: str = Form(""),
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        code = code.strip().lower()
        title = title.strip()
        if campaign_type not in {"classic", "daily_414"}:
            return master_redirect(
                "Проверьте тип кампании", error=True, tab="campaigns"
            )
        if campaign_type == "daily_414":
            quiz_time_limit_seconds = 254
            max_attempts = 1
            verification_required = True
        if not CAMPAIGN_RE.fullmatch(code) or len(title) < 2 or len(title) > 80:
            return master_redirect("Проверьте код и название кампании", error=True, tab="campaigns")
        if pass_score < 0 or pass_score > 10_000:
            return master_redirect("Порог должен быть от 0 до 10000", error=True, tab="campaigns")
        if quiz_time_limit_seconds < 0 or quiz_time_limit_seconds > 7200:
            return master_redirect("Общий таймер должен быть от 0 до 7200 секунд", error=True, tab="campaigns")
        if max_attempts < 1 or max_attempts > 100:
            return master_redirect("Количество попыток должно быть от 1 до 100", error=True, tab="campaigns")
        if any(
            value < 0 or value > 10_000
            for value in (
                jackcoin_per_correct,
                jackcoin_completion_bonus,
                jackcoin_perfect_bonus,
            )
        ):
            return master_redirect(
                "Начисление JACKCOIN должно быть от 0 до 10000",
                error=True,
                tab="campaigns",
            )
        if reward_delivery_mode not in {"automatic", "code"} or referral_delivery_mode not in {"automatic", "code"}:
            return master_redirect("Проверьте способ выдачи награды", error=True, tab="campaigns")
        if campaign_type == "classic" and verification_required and not (
            (settings.telegram_client_id and settings.telegram_client_secret) or (settings.smtp_host and settings.smtp_from)
        ):
            return master_redirect("Сначала настройте Telegram или email в .env", error=True, tab="campaigns")
        try:
            active_from_value, active_until_value = validate_campaign_period(active_from, active_until)
            if campaign_type == "daily_414":
                active_from_value, active_until_value = default_daily_414_period(
                    active_from_value,
                    active_until_value,
                )
            reward_mode, reward_value, reward_from, reward_until = campaign_reward_values(
                reward_validity_mode, reward_validity_value, reward_valid_from, reward_valid_until
            )
            with transaction(settings.db_path) as conn:
                bonus_code, bonus_amount = validate_campaign_bonus(conn, bonus_preference_code, bonus_amount)
                referral_code, referral_amount = validate_campaign_bonus(
                    conn, referral_preference_code if referral_enabled else "",
                    referral_amount if referral_enabled else 0,
                )
                if referral_enabled and not 1 <= referral_threshold <= 1000:
                    raise ValueError("Количество приглашённых должно быть от 1 до 1000")
                if referral_max_rewards < 0 or referral_max_rewards > 1000:
                    raise ValueError("Лимит реферальных наград должен быть от 0 до 1000")
                final_prize_id = validate_final_prize(
                    conn,
                    campaign_type=campaign_type,
                    catalog_reward_id=final_prize_catalog_reward_id,
                )
                cursor = conn.execute(
                    """
                    INSERT INTO quiz_campaigns(
                        code, title, campaign_type, bonus_preference_code, bonus_amount, reward_delivery_mode,
                        pass_score, quiz_time_limit_seconds,
                        max_attempts, verification_required, reward_validity_mode, reward_validity_value,
                        reward_valid_from, reward_valid_until, referral_enabled, referral_preference_code,
                        referral_amount, referral_delivery_mode, referral_threshold, referral_repeatable, referral_max_rewards,
                        final_prize_catalog_reward_id, active_from, active_until
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (code, title, campaign_type, bonus_code, bonus_amount, reward_delivery_mode, pass_score, quiz_time_limit_seconds, max_attempts,
                     int(verification_required), reward_mode, reward_value, reward_from, reward_until,
                     int(referral_enabled), referral_code, referral_amount, referral_delivery_mode, referral_threshold,
                     int(referral_repeatable), referral_max_rewards, final_prize_id,
                     active_from_value, active_until_value),
                )
                if campaign_type == "daily_414":
                    conn.execute(
                        """
                        UPDATE quiz_campaigns
                        SET welcome_kicker='JACKSIDE 4:14',
                            welcome_text='10 вопросов. 4 минуты 14 секунд. Одна попытка. Возвращаться к предыдущим вопросам и менять ответы нельзя.',
                            start_button_text='ПОСМОТРЕТЬ ПРИЗ ДНЯ',
                            identity_text='Один человек. Один аккаунт. Одна попытка.',
                            jackcoin_per_correct=?,
                            jackcoin_completion_bonus=?,
                            jackcoin_perfect_bonus=?
                        WHERE id=?
                        """,
                        (
                            jackcoin_per_correct,
                            jackcoin_completion_bonus,
                            jackcoin_perfect_bonus,
                            int(cursor.lastrowid),
                        ),
                    )
                audit(
                    conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                    action="create", entity_type="quiz_campaign", entity_id=int(cursor.lastrowid),
                    details={"code": code, "title": title, "campaign_type": campaign_type, "bonus": bonus_code, "amount": bonus_amount, "pass_score": pass_score, "quiz_timer": quiz_time_limit_seconds, "active_from": active_from_value, "active_until": active_until_value},
                )
        except sqlite3.IntegrityError:
            return master_redirect("Кампания с таким кодом уже существует", error=True, tab="campaigns")
        except ValueError as exc:
            return master_redirect(str(exc), error=True, tab="campaigns")
        return master_redirect("Кампания квиза создана", tab="campaigns")

    @app.post("/api/master/quiz-campaigns/{campaign_id}/update")
    async def update_quiz_campaign(
        request: Request,
        campaign_id: int,
        title: str = Form(...),
        bonus_preference_code: str = Form(""),
        bonus_amount: int = Form(0),
        reward_delivery_mode: str = Form("automatic"),
        pass_score: int = Form(0),
        quiz_time_limit_seconds: int = Form(120),
        max_attempts: int = Form(3),
        verification_required: bool = Form(False),
        jackcoin_per_correct: int = Form(5),
        jackcoin_completion_bonus: int = Form(10),
        jackcoin_perfect_bonus: int = Form(20),
        final_prize_catalog_reward_id: int = Form(0),
        referral_enabled: bool = Form(False),
        referral_preference_code: str = Form(""),
        referral_amount: int = Form(0),
        referral_delivery_mode: str = Form("automatic"),
        referral_threshold: int = Form(1),
        referral_repeatable: bool = Form(False),
        referral_max_rewards: int = Form(1),
        reward_validity_mode: str = Form("end_of_day"),
        reward_validity_value: int = Form(0),
        reward_valid_from: str = Form(""),
        reward_valid_until: str = Form(""),
        welcome_kicker: str = Form(""),
        welcome_text: str = Form(""),
        start_button_text: str = Form(""),
        identity_text: str = Form(""),
        victory_title: str = Form(""),
        victory_text: str = Form(""),
        failure_title: str = Form(""),
        failure_text: str = Form(""),
        completion_title: str = Form(""),
        completion_text: str = Form(""),
        active_from: str = Form(""),
        active_until: str = Form(""),
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        title = title.strip()
        with connect(settings.db_path) as conn:
            existing_campaign = conn.execute(
                """
                SELECT campaign_type, active_from, active_until
                FROM quiz_campaigns WHERE id=?
                """,
                (campaign_id,),
            ).fetchone()
        if not existing_campaign:
            return master_redirect(
                "Кампания не найдена", error=True, tab="campaigns"
            )
        if existing_campaign["campaign_type"] == "daily_414":
            quiz_time_limit_seconds = 254
            max_attempts = 1
            verification_required = True
        if len(title) < 2 or len(title) > 80:
            return master_redirect("Проверьте название кампании", error=True, tab="campaigns")
        if pass_score < 0 or pass_score > 10_000:
            return master_redirect("Порог должен быть от 0 до 10000", error=True, tab="campaigns")
        if quiz_time_limit_seconds < 0 or quiz_time_limit_seconds > 7200:
            return master_redirect("Общий таймер должен быть от 0 до 7200 секунд", error=True, tab="campaigns")
        if max_attempts < 1 or max_attempts > 100:
            return master_redirect("Количество попыток должно быть от 1 до 100", error=True, tab="campaigns")
        if any(
            value < 0 or value > 10_000
            for value in (
                jackcoin_per_correct,
                jackcoin_completion_bonus,
                jackcoin_perfect_bonus,
            )
        ):
            return master_redirect(
                "Начисление JACKCOIN должно быть от 0 до 10000",
                error=True,
                tab="campaigns",
            )
        if reward_delivery_mode not in {"automatic", "code"} or referral_delivery_mode not in {"automatic", "code"}:
            return master_redirect("Проверьте способ выдачи награды", error=True, tab="campaigns")
        if (
            existing_campaign["campaign_type"] == "classic"
            and verification_required
            and not (
            (settings.telegram_client_id and settings.telegram_client_secret) or (settings.smtp_host and settings.smtp_from)
            )
        ):
            return master_redirect("Сначала настройте Telegram или email в .env", error=True, tab="campaigns")
        try:
            active_from_value, active_until_value = validate_campaign_period(active_from, active_until)
            if existing_campaign["campaign_type"] == "daily_414":
                active_from_value, active_until_value = default_daily_414_period(
                    active_from_value or existing_campaign["active_from"],
                    active_until_value or existing_campaign["active_until"],
                )
            reward_mode, reward_value, reward_from, reward_until = campaign_reward_values(
                reward_validity_mode, reward_validity_value, reward_valid_from, reward_valid_until
            )
            content_values = campaign_content_values(
                welcome_kicker=welcome_kicker, welcome_text=welcome_text, start_button_text=start_button_text,
                identity_text=identity_text, victory_title=victory_title, victory_text=victory_text,
                failure_title=failure_title, failure_text=failure_text,
                completion_title=completion_title, completion_text=completion_text,
            )
            with transaction(settings.db_path) as conn:
                row = conn.execute("SELECT * FROM quiz_campaigns WHERE id = ?", (campaign_id,)).fetchone()
                if not row:
                    return master_redirect("Кампания не найдена", error=True, tab="campaigns")
                bonus_code, bonus_amount = validate_campaign_bonus(conn, bonus_preference_code, bonus_amount)
                referral_code, referral_amount = validate_campaign_bonus(
                    conn, referral_preference_code if referral_enabled else "",
                    referral_amount if referral_enabled else 0,
                )
                if referral_enabled and not 1 <= referral_threshold <= 1000:
                    raise ValueError("Количество приглашённых должно быть от 1 до 1000")
                if referral_max_rewards < 0 or referral_max_rewards > 1000:
                    raise ValueError("Лимит реферальных наград должен быть от 0 до 1000")
                final_prize_id = validate_final_prize(
                    conn,
                    campaign_type=str(row["campaign_type"]),
                    catalog_reward_id=final_prize_catalog_reward_id,
                )
                conn.execute(
                    """
                    UPDATE quiz_campaigns SET title=?, bonus_preference_code=?, bonus_amount=?, reward_delivery_mode=?, pass_score=?,
                        quiz_time_limit_seconds=?, max_attempts=?, verification_required=?,
                        reward_validity_mode=?, reward_validity_value=?, reward_valid_from=?, reward_valid_until=?,
                        welcome_kicker=?, welcome_text=?, start_button_text=?, identity_text=?,
                        victory_title=?, victory_text=?, failure_title=?, failure_text=?,
                        completion_title=?, completion_text=?, referral_enabled=?, referral_preference_code=?,
                        referral_amount=?, referral_delivery_mode=?, referral_threshold=?, referral_repeatable=?, referral_max_rewards=?,
                        jackcoin_per_correct=?, jackcoin_completion_bonus=?, jackcoin_perfect_bonus=?,
                        final_prize_catalog_reward_id=?, active_from=?, active_until=?, updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (title, bonus_code, bonus_amount, reward_delivery_mode, pass_score, quiz_time_limit_seconds, max_attempts,
                     int(verification_required), reward_mode, reward_value, reward_from, reward_until,
                     content_values["welcome_kicker"], content_values["welcome_text"], content_values["start_button_text"],
                     content_values["identity_text"], content_values["victory_title"], content_values["victory_text"],
                     content_values["failure_title"], content_values["failure_text"], content_values["completion_title"],
                     content_values["completion_text"], int(referral_enabled), referral_code, referral_amount, referral_delivery_mode,
                     referral_threshold, int(referral_repeatable), referral_max_rewards,
                     jackcoin_per_correct, jackcoin_completion_bonus, jackcoin_perfect_bonus,
                     final_prize_id, active_from_value, active_until_value, campaign_id),
                )
                audit(
                    conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                    action="update", entity_type="quiz_campaign", entity_id=campaign_id,
                    details={"code": row["code"], "title": title, "bonus": bonus_code, "amount": bonus_amount, "pass_score": pass_score, "quiz_timer": quiz_time_limit_seconds, "active_from": active_from_value, "active_until": active_until_value},
                )
        except ValueError as exc:
            return master_redirect(str(exc), error=True, tab="campaigns")
        return master_redirect("Кампания квиза обновлена", tab="campaigns")

    @app.post("/api/master/quiz-campaigns/{campaign_id}/toggle")
    async def toggle_quiz_campaign(request: Request, campaign_id: int, csrf_token: str = Form(...)):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            row = conn.execute("SELECT * FROM quiz_campaigns WHERE id = ?", (campaign_id,)).fetchone()
            if not row:
                return master_redirect("Кампания не найдена", error=True, tab="campaigns")
            if row["archived_at"]:
                return master_redirect(
                    "Сначала восстановите кампанию из архива",
                    error=True,
                    tab="campaigns",
                )
            new_state = 0 if row["is_active"] else 1
            conn.execute("UPDATE quiz_campaigns SET is_active=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_state, campaign_id))
            audit(
                conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="activate" if new_state else "deactivate", entity_type="quiz_campaign", entity_id=campaign_id,
                details={"code": row["code"], "title": row["title"]},
            )
        return master_redirect("Кампания включена" if new_state else "Кампания отключена", tab="campaigns")

    @app.post("/api/master/quiz-campaigns/{campaign_id}/archive")
    async def archive_quiz_campaign(
        request: Request,
        campaign_id: int,
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM quiz_campaigns WHERE id=?",
                (campaign_id,),
            ).fetchone()
            if not row:
                return master_redirect(
                    "Кампания не найдена", error=True, tab="campaigns"
                )
            if row["archived_at"]:
                return master_redirect("Кампания уже находится в архиве", tab="campaigns")
            conn.execute(
                """
                UPDATE quiz_campaigns
                SET is_active=0, archived_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (campaign_id,),
            )
            audit(
                conn,
                admin_id=request.session["admin_id"],
                admin_name=request.session["admin_name"],
                action="archive",
                entity_type="quiz_campaign",
                entity_id=campaign_id,
                details={"code": row["code"], "title": row["title"]},
            )
        return master_redirect("Кампания перемещена в архив", tab="campaigns")

    @app.post("/api/master/quiz-campaigns/{campaign_id}/restore")
    async def restore_quiz_campaign(
        request: Request,
        campaign_id: int,
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM quiz_campaigns WHERE id=?",
                (campaign_id,),
            ).fetchone()
            if not row:
                return master_redirect(
                    "Кампания не найдена", error=True, tab="campaigns"
                )
            if not row["archived_at"]:
                return master_redirect("Кампания уже восстановлена", tab="campaigns")
            conn.execute(
                """
                UPDATE quiz_campaigns
                SET archived_at=NULL, is_active=0, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (campaign_id,),
            )
            audit(
                conn,
                admin_id=request.session["admin_id"],
                admin_name=request.session["admin_name"],
                action="restore",
                entity_type="quiz_campaign",
                entity_id=campaign_id,
                details={"code": row["code"], "title": row["title"]},
            )
        return master_redirect(
            "Кампания восстановлена и оставлена отключённой",
            tab="campaigns",
        )

    @app.post("/api/master/quiz-campaigns/{campaign_id}/delete")
    async def delete_quiz_campaign(
        request: Request,
        campaign_id: int,
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM quiz_campaigns WHERE id=?",
                (campaign_id,),
            ).fetchone()
            if not row:
                return master_redirect(
                    "Кампания не найдена", error=True, tab="campaigns"
                )
            if not row["archived_at"]:
                return master_redirect(
                    "Перед удалением переместите кампанию в архив",
                    error=True,
                    tab="campaigns",
                )
            delete_permanently = (
                row["code"] not in BUILT_IN_CAMPAIGN_CODES
                and not campaign_has_history(conn, row["code"])
            )
            if delete_permanently:
                conn.execute(
                    "DELETE FROM daily_414_final_tables WHERE campaign_code=?",
                    (row["code"],),
                )
                conn.execute(
                    """
                    DELETE FROM quiz_options
                    WHERE question_id IN (
                        SELECT id FROM quiz_questions WHERE campaign_code=?
                    )
                    """,
                    (row["code"],),
                )
                conn.execute(
                    "DELETE FROM quiz_questions WHERE campaign_code=?",
                    (row["code"],),
                )
                conn.execute(
                    "DELETE FROM quiz_sections WHERE campaign_code=?",
                    (row["code"],),
                )
                conn.execute("DELETE FROM quiz_campaigns WHERE id=?", (campaign_id,))
            else:
                conn.execute(
                    """
                    UPDATE quiz_campaigns
                    SET is_active=0, deleted_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (campaign_id,),
                )
            audit(
                conn,
                admin_id=request.session["admin_id"],
                admin_name=request.session["admin_name"],
                action="delete",
                entity_type="quiz_campaign",
                entity_id=campaign_id,
                details={
                    "code": row["code"],
                    "title": row["title"],
                    "mode": "permanent" if delete_permanently else "hidden",
                },
            )
        return master_redirect(
            "Пустая кампания удалена навсегда"
            if delete_permanently
            else "Кампания удалена из списка, история сохранена",
            tab="campaigns",
        )

    @app.post("/api/master/quiz-campaigns/{campaign_id}/publish-version")
    async def publish_quiz_campaign_version(
        request: Request, campaign_id: int, csrf_token: str = Form(...)
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            row = conn.execute("SELECT * FROM quiz_campaigns WHERE id=?", (campaign_id,)).fetchone()
            if not row:
                return builder_redirect(campaign_id, "Кампания не найдена", error=True)
            try:
                questions = load_db_questions(conn, row["code"])
                if row["campaign_type"] == "daily_414":
                    validate_daily_questions(questions, row["code"])
            except ValueError:
                return builder_redirect(
                    campaign_id,
                    (
                        "Для основного раунда 4:14 нужно ровно 10 "
                        "опубликованных вопросов"
                        if row["campaign_type"] == "daily_414"
                        else "Нельзя опубликовать версию без готовых вопросов"
                    ),
                    error=True,
                )
            new_version = max(1, int(row["current_version"] or 1)) + 1
            conn.execute(
                "UPDATE quiz_attempts SET status='expired', finished_at=CURRENT_TIMESTAMP WHERE campaign_code=? AND status='in_progress'",
                (row["code"],),
            )
            conn.execute(
                "UPDATE quiz_campaigns SET current_version=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (new_version, campaign_id),
            )
            audit(
                conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="publish_version", entity_type="quiz_campaign", entity_id=campaign_id,
                details={"code": row["code"], "old_version": row["current_version"], "new_version": new_version,
                         "questions": len(questions)},
            )
        return builder_redirect(campaign_id, f"Опубликована версия {new_version}. Попытки участников начались заново")

    def builder_redirect(campaign_id: int, message: str, *, error: bool = False) -> RedirectResponse:
        parameter = "error" if error else "ok"
        return RedirectResponse(f"/master/quiz-builder/{campaign_id}?{parameter}={message}", status_code=303)

    @app.get("/master/quiz-builder/{campaign_id:int}", response_class=HTMLResponse)
    async def quiz_builder_page(request: Request, campaign_id: int, ok: str = "", error: str = ""):
        require_master(request)
        with connect(settings.db_path) as conn:
            campaign = conn.execute(
                """
                SELECT qc.*, pt.title AS bonus_title,
                       vcr.title AS final_prize_title
                FROM quiz_campaigns qc
                LEFT JOIN preference_types pt ON pt.code=qc.bonus_preference_code
                LEFT JOIN vault_catalog_rewards vcr
                  ON vcr.id=qc.final_prize_catalog_reward_id
                WHERE qc.id=?
                """,
                (campaign_id,),
            ).fetchone()
            if not campaign:
                raise HTTPException(status_code=404, detail="Кампания не найдена")
            questions = load_builder_questions(conn, campaign["code"])
            sections = conn.execute(
                "SELECT * FROM quiz_sections WHERE campaign_code=? ORDER BY position, id",
                (campaign["code"],),
            ).fetchall()
        main_questions = [
            question for question in questions
            if question["game_round"] == "main"
        ]
        final_questions = [
            question for question in questions
            if question["game_round"] == "final"
        ]
        active_questions = [
            question for question in questions
            if question["is_active"]
            and (
                campaign["campaign_type"] != "daily_414"
                or question["game_round"] == "main"
            )
        ]
        max_score = sum(
            question["points"] for question in active_questions
            if (
                question["type"] == "text" and question["accepted_text_answers"]
            ) or (
                question["type"] != "text"
                and any(option["correct"] and option["is_active"] for option in question["options"])
            )
        )
        max_correct = sum(
            1 for question in active_questions
            if (
                question["type"] == "text" and question["accepted_text_answers"]
            ) or (
                question["type"] != "text"
                and any(option["correct"] and option["is_active"] for option in question["options"])
            )
        )
        return templates.TemplateResponse(
            request,
            "quiz_builder.html",
            context(
                request, campaign=campaign, questions=questions, max_score=max_score,
                max_correct=max_correct, published_count=len(active_questions),
                hidden_count=sum(not question["is_active"] for question in questions),
                main_questions=main_questions,
                final_questions=final_questions,
                active_main_count=sum(question["is_active"] for question in main_questions),
                active_final_count=sum(question["is_active"] for question in final_questions),
                sections=sections,
                ok=ok, error=error,
            ),
        )

    def normalize_complete_question(payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title", "")).strip()
        question_type = str(payload.get("question_type", "single_choice"))
        visual_type = str(payload.get("visual_type", "standard"))
        game_round = str(payload.get("game_round", "main"))
        if len(title) < 2 or len(title) > 300 or question_type not in {"single_choice", "multi_choice", "text"}:
            raise ValueError("Проверьте текст и тип вопроса")
        if visual_type not in {"standard", "rebus", "photo"}:
            raise ValueError("Проверьте визуальный формат вопроса")
        if game_round not in {"main", "final"}:
            raise ValueError("Проверьте этап игры")
        image_path = str(payload.get("image_path", "")).strip()[:500] or None
        try:
            section_id = int(payload.get("section_id") or 0) or None
        except (TypeError, ValueError) as exc:
            raise ValueError("Проверьте блок вопроса") from exc
        if visual_type in {"rebus", "photo"} and not image_path:
            raise ValueError("Для ребуса или фотовопроса добавьте изображение")
        try:
            points = int(payload.get("points", 1))
            time_limit_seconds = int(payload.get("time_limit_seconds", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("Баллы и таймер должны быть целыми числами") from exc
        if points < 0 or points > 1000:
            raise ValueError("Баллы должны быть от 0 до 1000")
        if time_limit_seconds < 0 or time_limit_seconds > 600:
            raise ValueError("Таймер должен быть от 0 до 600 секунд")
        options: list[dict[str, Any]] = []
        accepted_text_answers: list[str] = []
        if question_type != "text":
            raw_options = payload.get("options")
            if not isinstance(raw_options, list):
                raise ValueError("Добавьте варианты ответов")
            seen_option_ids: set[int] = set()
            for raw_option in raw_options:
                if not isinstance(raw_option, dict):
                    raise ValueError("Проверьте варианты ответов")
                option_text = str(raw_option.get("text", "")).strip()
                if not option_text:
                    continue
                if len(option_text) > 300:
                    raise ValueError("Вариант ответа не должен быть длиннее 300 символов")
                raw_option_id = raw_option.get("option_id")
                try:
                    option_id = int(raw_option_id) if raw_option_id else None
                except (TypeError, ValueError) as exc:
                    raise ValueError("Проверьте варианты ответов") from exc
                if option_id is not None:
                    if option_id in seen_option_ids:
                        raise ValueError("Один вариант ответа передан несколько раз")
                    seen_option_ids.add(option_id)
                options.append({
                    "db_id": option_id,
                    "text": option_text,
                    "is_correct": bool(raw_option.get("is_correct")),
                })
            if len(options) < 2:
                raise ValueError("Добавьте минимум два варианта ответа")
            if len({option["text"].casefold() for option in options}) != len(options):
                raise ValueError("Варианты ответов не должны повторяться")
            correct_count = sum(int(option["is_correct"]) for option in options)
            if question_type == "single_choice" and correct_count != 1:
                raise ValueError("Отметьте один правильный вариант")
            if question_type == "multi_choice" and correct_count < 1:
                raise ValueError("Отметьте хотя бы один правильный вариант")
        else:
            raw_accepted = payload.get("accepted_text_answers", [])
            if isinstance(raw_accepted, str):
                raw_accepted = raw_accepted.splitlines()
            if not isinstance(raw_accepted, list):
                raise ValueError("Проверьте правильные текстовые ответы")
            for raw_answer in raw_accepted:
                answer = str(raw_answer).strip()
                if not answer:
                    continue
                if len(answer) > 300:
                    raise ValueError("Правильный текстовый ответ не должен быть длиннее 300 символов")
                accepted_text_answers.append(answer)
            normalized_answers = [normalize_text_answer(answer) for answer in accepted_text_answers]
            if len(set(normalized_answers)) != len(normalized_answers):
                raise ValueError("Одинаковые варианты текстового ответа достаточно указать один раз")
            if points > 0 and not accepted_text_answers:
                raise ValueError("Для начисления баллов укажите правильный текстовый ответ")
        return {
            "title": title,
            "question_type": question_type,
            "visual_type": visual_type,
            "game_round": game_round,
            "image_path": image_path,
            "section_id": section_id,
            "required": bool(payload.get("required", True)),
            "points": points,
            "time_limit_seconds": time_limit_seconds or None,
            "placeholder": str(payload.get("placeholder", "")).strip()[:200] or None,
            "options": options,
            "accepted_text_answers": accepted_text_answers,
            "publish": True,
        }

    def insert_complete_question(
        conn: sqlite3.Connection,
        *,
        campaign_code: str,
        question: dict[str, Any],
        admin_id: int,
    ) -> int:
        position = int(conn.execute(
            """
            SELECT COALESCE(MAX(position), 0) + 10
            FROM quiz_questions WHERE campaign_code=? AND game_round=?
            """,
            (campaign_code, question["game_round"]),
        ).fetchone()[0])
        cursor = conn.execute(
            """
            INSERT INTO quiz_questions(
                campaign_code, code, type, title, visual_type, image_path, section_id,
                placeholder, accepted_text_answers_json, game_round,
                required, points, time_limit_seconds,
                position, is_active, created_by_admin_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_code, f"q_{uuid.uuid4().hex[:10]}", question["question_type"], question["title"],
                question["visual_type"], question["image_path"], question["section_id"],
                question["placeholder"],
                json.dumps(question["accepted_text_answers"], ensure_ascii=False),
                question["game_round"],
                int(question["required"]), question["points"],
                question["time_limit_seconds"], position, int(question["publish"]), admin_id,
            ),
        )
        question_id = int(cursor.lastrowid)
        for index, option in enumerate(question["options"], start=1):
            conn.execute(
                """
                INSERT INTO quiz_options(question_id, code, text, is_correct, position)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    question_id, f"o_{uuid.uuid4().hex[:10]}", option["text"],
                    int(option["is_correct"]), index * 10,
                ),
            )
        return question_id

    @app.post("/api/master/quiz-campaigns/{campaign_id}/questions/create-complete")
    async def create_complete_quiz_question(request: Request, campaign_id: int):
        require_master(request, api=True)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="Некорректные данные")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Некорректные данные")
        check_csrf(request, str(payload.get("csrf_token", "")))
        try:
            question = normalize_complete_question(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        with transaction(settings.db_path) as conn:
            campaign = conn.execute(
                "SELECT code, campaign_type FROM quiz_campaigns WHERE id=?",
                (campaign_id,),
            ).fetchone()
            if not campaign:
                raise HTTPException(status_code=404, detail="Кампания не найдена")
            if campaign["campaign_type"] != "daily_414":
                question["game_round"] = "main"
            if question["section_id"] and not conn.execute(
                "SELECT 1 FROM quiz_sections WHERE id=? AND campaign_code=?",
                (question["section_id"], campaign["code"]),
            ).fetchone():
                raise HTTPException(status_code=422, detail="Выбранный блок не найден")
            question_id = insert_complete_question(
                conn, campaign_code=campaign["code"], question=question,
                admin_id=request.session["admin_id"],
            )
            audit(
                conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="create_complete", entity_type="quiz_question", entity_id=question_id,
                details={"campaign": campaign["code"], "title": question["title"], "options": len(question["options"]), "published": question["publish"]},
            )
        with connect(settings.db_path) as conn:
            saved_options = load_builder_questions(conn, campaign["code"])
        saved_question = next(
            item for item in saved_options if item["db_id"] == question_id
        )
        return {
            "ok": True,
            "question_id": question_id,
            "message": "Вопрос сохранён",
            "options": saved_question["options"],
        }

    @app.post("/api/master/quiz-questions/{question_id}/update-complete")
    async def update_complete_quiz_question(request: Request, question_id: int):
        require_master(request, api=True)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="Некорректные данные")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Некорректные данные")
        check_csrf(request, str(payload.get("csrf_token", "")))
        try:
            question = normalize_complete_question(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        with transaction(settings.db_path) as conn:
            row = conn.execute(
                """
                SELECT qq.*, qc.id AS campaign_id, qc.campaign_type FROM quiz_questions qq
                JOIN quiz_campaigns qc ON qc.code=qq.campaign_code WHERE qq.id=?
                """,
                (question_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Вопрос не найден")
            if row["campaign_type"] != "daily_414":
                question["game_round"] = "main"
            existing_options = {
                int(option["id"]): option
                for option in conn.execute(
                    """
                    SELECT * FROM quiz_options
                    WHERE question_id=?
                    ORDER BY position, id
                    """,
                    (question_id,),
                ).fetchall()
            }
            supplied_option_ids = {
                int(option["db_id"])
                for option in question["options"]
                if option["db_id"] is not None
            }
            if not supplied_option_ids.issubset(existing_options):
                raise HTTPException(
                    status_code=422,
                    detail="Один из вариантов ответа относится к другому вопросу",
                )
            conn.execute(
                """
                UPDATE quiz_questions SET type=?, title=?, placeholder=?, required=?, points=?,
                    visual_type=?, image_path=?, section_id=?, accepted_text_answers_json=?,
                    game_round=?, time_limit_seconds=NULL, is_active=1,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?
                """,
                (
                    question["question_type"], question["title"], question["placeholder"],
                    int(question["required"]), question["points"], question["visual_type"],
                    question["image_path"], question["section_id"],
                    json.dumps(question["accepted_text_answers"], ensure_ascii=False),
                    question["game_round"],
                    question_id,
                ),
            )
            kept_option_ids: set[int] = set()
            for index, option in enumerate(question["options"], start=1):
                if option["db_id"] is not None:
                    option_id = int(option["db_id"])
                    conn.execute(
                        """
                        UPDATE quiz_options
                        SET text=?, is_correct=?, position=?, is_active=1,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND question_id=?
                        """,
                        (
                            option["text"], int(option["is_correct"]), index * 10,
                            option_id, question_id,
                        ),
                    )
                else:
                    option_id = int(
                        conn.execute(
                            """
                            INSERT INTO quiz_options(
                                question_id, code, text, is_correct, position
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                question_id, f"o_{uuid.uuid4().hex[:10]}",
                                option["text"], int(option["is_correct"]), index * 10,
                            ),
                        ).lastrowid
                    )
                kept_option_ids.add(option_id)
            for option_id in existing_options:
                if option_id not in kept_option_ids:
                    conn.execute(
                        "DELETE FROM quiz_options WHERE id=? AND question_id=?",
                        (option_id, question_id),
                    )
            audit(
                conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="update_complete", entity_type="quiz_question", entity_id=question_id,
                details={"title": question["title"], "type": question["question_type"], "options": len(question["options"])},
            )
            saved_options = conn.execute(
                """
                SELECT id, code, text, is_correct, position, is_active
                FROM quiz_options WHERE question_id=?
                ORDER BY position, id
                """,
                (question_id,),
            ).fetchall()
        return {
            "ok": True,
            "question_id": question_id,
            "message": "Изменения сохранены",
            "options": [
                {
                    "db_id": int(option["id"]),
                    "id": option["code"],
                    "text": option["text"],
                    "correct": bool(option["is_correct"]),
                    "position": int(option["position"]),
                    "is_active": bool(option["is_active"]),
                }
                for option in saved_options
            ],
        }

    @app.post("/api/master/quiz-campaigns/{campaign_id}/media")
    async def upload_quiz_media(
        request: Request,
        campaign_id: int,
        image: UploadFile = File(...),
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        with connect(settings.db_path) as conn:
            campaign = conn.execute("SELECT code FROM quiz_campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not campaign:
            raise HTTPException(status_code=404, detail="Кампания не найдена")
        allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
        suffix = allowed.get(str(image.content_type or "").lower())
        if not suffix:
            raise HTTPException(status_code=422, detail="Загрузите JPG, PNG или WebP")
        content = await image.read(8 * 1024 * 1024 + 1)
        if not content or len(content) > 8 * 1024 * 1024:
            raise HTTPException(status_code=422, detail="Размер изображения должен быть не больше 8 МБ")
        campaign_dir = quiz_media_dir / campaign["code"]
        campaign_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex}{suffix}"
        (campaign_dir / filename).write_bytes(content)
        return {"ok": True, "path": f"/quiz-media/{campaign['code']}/{filename}"}

    @app.post("/api/master/quiz-campaigns/{campaign_id}/sections")
    async def create_quiz_section(
        request: Request,
        campaign_id: int,
        title: str = Form(...),
        theme: str = Form("theory"),
        position: int = Form(100),
        background_image: UploadFile | None = File(None),
        csrf_token: str = Form(...),
    ):
        require_master(request)
        check_csrf(request, csrf_token)
        title = title.strip()[:100]
        if len(title) < 2 or theme not in {"theory", "rebus", "photo", "custom"}:
            return builder_redirect(campaign_id, "Проверьте название и фон блока", error=True)
        image_path = None
        with transaction(settings.db_path) as conn:
            campaign = conn.execute("SELECT code FROM quiz_campaigns WHERE id=?", (campaign_id,)).fetchone()
            if not campaign:
                raise HTTPException(status_code=404, detail="Кампания не найдена")
            if background_image and background_image.filename:
                allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
                suffix = allowed.get(str(background_image.content_type or "").lower())
                content = await background_image.read(8 * 1024 * 1024 + 1)
                if not suffix or not content or len(content) > 8 * 1024 * 1024:
                    return builder_redirect(campaign_id, "Фон: JPG, PNG или WebP до 8 МБ", error=True)
                campaign_dir = quiz_media_dir / campaign["code"]
                campaign_dir.mkdir(parents=True, exist_ok=True)
                filename = f"{uuid.uuid4().hex}{suffix}"
                (campaign_dir / filename).write_bytes(content)
                image_path = f"/quiz-media/{campaign['code']}/{filename}"
            conn.execute(
                "INSERT INTO quiz_sections(campaign_code,title,theme,background_image,position) VALUES (?,?,?,?,?)",
                (campaign["code"], title, theme, image_path, max(0, min(position, 9999))),
            )
        return builder_redirect(campaign_id, f"Блок «{title}» создан")

    @app.post("/api/master/quiz-sections/{section_id}/update")
    async def update_quiz_section(
        request: Request,
        section_id: int,
        title: str = Form(...),
        theme: str = Form("theory"),
        position: int = Form(100),
        csrf_token: str = Form(...),
    ):
        require_master(request)
        check_csrf(request, csrf_token)
        title = title.strip()[:100]
        with transaction(settings.db_path) as conn:
            section = conn.execute(
                "SELECT qs.*, qc.id AS campaign_id FROM quiz_sections qs JOIN quiz_campaigns qc ON qc.code=qs.campaign_code WHERE qs.id=?",
                (section_id,),
            ).fetchone()
            if not section:
                raise HTTPException(status_code=404, detail="Блок не найден")
            if len(title) < 2 or theme not in {"theory", "rebus", "photo", "custom"}:
                return builder_redirect(section["campaign_id"], "Проверьте название и фон блока", error=True)
            conn.execute(
                "UPDATE quiz_sections SET title=?, theme=?, position=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (title, theme, max(0, min(position, 9999)), section_id),
            )
        return builder_redirect(section["campaign_id"], f"Блок «{title}» обновлён")

    @app.post("/api/master/quiz-sections/{section_id}/delete")
    async def delete_quiz_section(request: Request, section_id: int, csrf_token: str = Form(...)):
        require_master(request)
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            section = conn.execute(
                "SELECT qs.*, qc.id AS campaign_id FROM quiz_sections qs JOIN quiz_campaigns qc ON qc.code=qs.campaign_code WHERE qs.id=?",
                (section_id,),
            ).fetchone()
            if not section:
                raise HTTPException(status_code=404, detail="Блок не найден")
            conn.execute("UPDATE quiz_questions SET section_id=NULL WHERE section_id=?", (section_id,))
            conn.execute("DELETE FROM quiz_sections WHERE id=?", (section_id,))
        return builder_redirect(section["campaign_id"], "Блок удалён; вопросы сохранены без блока")

    @app.post("/api/master/quiz-campaigns/{campaign_id}/questions/bulk-create")
    async def bulk_create_quiz_questions(request: Request, campaign_id: int):
        require_master(request, api=True)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="Некорректные данные")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Некорректные данные")
        check_csrf(request, str(payload.get("csrf_token", "")))
        try:
            parsed = parse_quick_questions(str(payload.get("text", "")))
            points = int(payload.get("points", 1))
            time_limit_seconds = int(payload.get("time_limit_seconds", 0))
            if points < 0 or points > 1000 or time_limit_seconds < 0 or time_limit_seconds > 600:
                raise ValueError("Проверьте баллы и таймер")
            questions = [normalize_complete_question({
                **item,
                "points": points,
                "time_limit_seconds": time_limit_seconds,
                "game_round": payload.get("game_round", "main"),
                "required": True,
                "publish": True,
            }) for item in parsed]
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        with transaction(settings.db_path) as conn:
            campaign = conn.execute(
                "SELECT code, campaign_type FROM quiz_campaigns WHERE id=?",
                (campaign_id,),
            ).fetchone()
            if not campaign:
                raise HTTPException(status_code=404, detail="Кампания не найдена")
            if campaign["campaign_type"] != "daily_414":
                for question in questions:
                    question["game_round"] = "main"
            question_ids = [
                insert_complete_question(
                    conn, campaign_code=campaign["code"], question=question,
                    admin_id=request.session["admin_id"],
                )
                for question in questions
            ]
            audit(
                conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="bulk_create", entity_type="quiz_campaign", entity_id=campaign_id,
                details={"campaign": campaign["code"], "questions": len(question_ids), "published": True},
            )
        return {"ok": True, "created": len(question_ids), "message": f"Добавлено вопросов: {len(question_ids)}"}

    @app.post("/api/master/quiz-campaigns/{campaign_id}/questions/create")
    async def create_quiz_question(
        request: Request,
        campaign_id: int,
        title: str = Form(...),
        question_type: str = Form(...),
        required: int = Form(0),
        points: int = Form(1),
        time_limit_seconds: int = Form(0),
        position: int = Form(100),
        placeholder: str = Form(""),
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        title = title.strip()
        if len(title) < 2 or len(title) > 300 or question_type not in {"single_choice", "multi_choice", "text"}:
            return builder_redirect(campaign_id, "Проверьте вопрос и его тип", error=True)
        if points < 0 or points > 1000:
            return builder_redirect(campaign_id, "Баллы должны быть от 0 до 1000", error=True)
        if time_limit_seconds < 0 or time_limit_seconds > 600:
            return builder_redirect(campaign_id, "Таймер должен быть от 0 до 600 секунд", error=True)
        position = max(0, min(position, 9999))
        with transaction(settings.db_path) as conn:
            campaign = conn.execute("SELECT code FROM quiz_campaigns WHERE id = ?", (campaign_id,)).fetchone()
            if not campaign:
                return builder_redirect(campaign_id, "Кампания не найдена", error=True)
            code = f"q_{uuid.uuid4().hex[:10]}"
            cursor = conn.execute(
                """
                INSERT INTO quiz_questions(
                    campaign_code, code, type, title, placeholder, required, points, time_limit_seconds,
                    position, is_active, created_by_admin_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    campaign["code"], code, question_type, title, placeholder.strip()[:200] or None,
                    int(bool(required)), points if question_type != "text" else 0,
                    time_limit_seconds or None, position,
                    request.session["admin_id"],
                ),
            )
            audit(
                conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="create", entity_type="quiz_question", entity_id=int(cursor.lastrowid),
                details={"campaign": campaign["code"], "code": code, "type": question_type, "title": title},
            )
        return builder_redirect(campaign_id, "Черновик вопроса создан")

    @app.post("/api/master/quiz-questions/{question_id}/update")
    async def update_quiz_question(
        request: Request,
        question_id: int,
        title: str = Form(...),
        required: int = Form(0),
        points: int = Form(1),
        time_limit_seconds: int = Form(0),
        position: int = Form(100),
        placeholder: str = Form(""),
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        title = title.strip()
        with transaction(settings.db_path) as conn:
            row = conn.execute(
                "SELECT qq.*, qc.id AS campaign_id FROM quiz_questions qq JOIN quiz_campaigns qc ON qc.code=qq.campaign_code WHERE qq.id=?",
                (question_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Вопрос не найден")
            campaign_id = int(row["campaign_id"])
            if len(title) < 2 or len(title) > 300 or points < 0 or points > 1000 or time_limit_seconds < 0 or time_limit_seconds > 600:
                return builder_redirect(campaign_id, "Проверьте текст и баллы", error=True)
            conn.execute(
                """
                UPDATE quiz_questions SET title=?, placeholder=?, required=?, points=?, time_limit_seconds=?, position=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    title, placeholder.strip()[:200] or None, int(bool(required)),
                    points if row["type"] != "text" else 0, time_limit_seconds or None,
                    max(0, min(position, 9999)), question_id,
                ),
            )
            audit(
                conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="update", entity_type="quiz_question", entity_id=question_id,
                details={"title": title, "points": points, "required": bool(required), "timer": time_limit_seconds},
            )
        return builder_redirect(campaign_id, "Вопрос обновлён")

    @app.post("/api/master/quiz-questions/{question_id}/toggle")
    async def toggle_quiz_question(request: Request, question_id: int, csrf_token: str = Form(...)):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            row = conn.execute(
                "SELECT qq.*, qc.id AS campaign_id FROM quiz_questions qq JOIN quiz_campaigns qc ON qc.code=qq.campaign_code WHERE qq.id=?",
                (question_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Вопрос не найден")
            campaign_id = int(row["campaign_id"])
            new_state = 0 if row["is_active"] else 1
            if new_state and row["type"] != "text":
                options = conn.execute("SELECT is_correct FROM quiz_options WHERE question_id=? AND is_active=1", (question_id,)).fetchall()
                if len(options) < 2:
                    return builder_redirect(campaign_id, "Для публикации добавьте минимум два варианта", error=True)
                correct_count = sum(int(option["is_correct"]) for option in options)
                if row["type"] == "single_choice" and correct_count > 1:
                    return builder_redirect(campaign_id, "У одиночного выбора может быть только один правильный вариант", error=True)
            conn.execute("UPDATE quiz_questions SET is_active=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_state, question_id))
            audit(
                conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="activate" if new_state else "deactivate", entity_type="quiz_question", entity_id=question_id,
                details={"title": row["title"]},
            )
        return builder_redirect(campaign_id, "Вопрос опубликован" if new_state else "Вопрос снят с публикации")

    @app.post("/api/master/quiz-questions/{question_id}/options/create")
    async def create_quiz_option(
        request: Request,
        question_id: int,
        text: str = Form(...),
        is_correct: int = Form(0),
        position: int = Form(100),
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        text = text.strip()
        with transaction(settings.db_path) as conn:
            question = conn.execute(
                "SELECT qq.*, qc.id AS campaign_id FROM quiz_questions qq JOIN quiz_campaigns qc ON qc.code=qq.campaign_code WHERE qq.id=?",
                (question_id,),
            ).fetchone()
            if not question:
                raise HTTPException(status_code=404, detail="Вопрос не найден")
            campaign_id = int(question["campaign_id"])
            if question["type"] == "text":
                return builder_redirect(campaign_id, "Текстовому вопросу варианты не нужны", error=True)
            if len(text) < 1 or len(text) > 300:
                return builder_redirect(campaign_id, "Проверьте текст варианта", error=True)
            if is_correct and question["type"] == "single_choice":
                conn.execute("UPDATE quiz_options SET is_correct=0, updated_at=CURRENT_TIMESTAMP WHERE question_id=?", (question_id,))
            code = f"o_{uuid.uuid4().hex[:10]}"
            cursor = conn.execute(
                "INSERT INTO quiz_options(question_id, code, text, is_correct, position) VALUES (?, ?, ?, ?, ?)",
                (question_id, code, text, int(bool(is_correct)), max(0, min(position, 9999))),
            )
            audit(
                conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="create", entity_type="quiz_option", entity_id=int(cursor.lastrowid),
                details={"question_id": question_id, "text": text, "correct": bool(is_correct)},
            )
        return builder_redirect(campaign_id, "Вариант добавлен")

    @app.post("/api/master/quiz-options/{option_id}/update")
    async def update_quiz_option(
        request: Request,
        option_id: int,
        text: str = Form(...),
        is_correct: int = Form(0),
        position: int = Form(100),
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        text = text.strip()
        with transaction(settings.db_path) as conn:
            row = conn.execute(
                """
                SELECT qo.*, qq.type AS question_type, qc.id AS campaign_id
                FROM quiz_options qo JOIN quiz_questions qq ON qq.id=qo.question_id
                JOIN quiz_campaigns qc ON qc.code=qq.campaign_code WHERE qo.id=?
                """,
                (option_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Вариант не найден")
            campaign_id = int(row["campaign_id"])
            if len(text) < 1 or len(text) > 300:
                return builder_redirect(campaign_id, "Проверьте текст варианта", error=True)
            if is_correct and row["question_type"] == "single_choice":
                conn.execute("UPDATE quiz_options SET is_correct=0, updated_at=CURRENT_TIMESTAMP WHERE question_id=?", (row["question_id"],))
            conn.execute(
                "UPDATE quiz_options SET text=?, is_correct=?, position=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (text, int(bool(is_correct)), max(0, min(position, 9999)), option_id),
            )
            audit(
                conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="update", entity_type="quiz_option", entity_id=option_id,
                details={"text": text, "correct": bool(is_correct)},
            )
        return builder_redirect(campaign_id, "Вариант обновлён")

    @app.post("/api/master/quiz-options/{option_id}/toggle")
    async def toggle_quiz_option(request: Request, option_id: int, csrf_token: str = Form(...)):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            row = conn.execute(
                """
                SELECT qo.*, qq.is_active AS question_active, qc.id AS campaign_id
                FROM quiz_options qo JOIN quiz_questions qq ON qq.id=qo.question_id
                JOIN quiz_campaigns qc ON qc.code=qq.campaign_code WHERE qo.id=?
                """,
                (option_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Вариант не найден")
            campaign_id = int(row["campaign_id"])
            new_state = 0 if row["is_active"] else 1
            if not new_state and row["question_active"]:
                active_count = conn.execute("SELECT COUNT(*) FROM quiz_options WHERE question_id=? AND is_active=1", (row["question_id"],)).fetchone()[0]
                if active_count <= 2:
                    return builder_redirect(campaign_id, "У опубликованного вопроса должно остаться минимум два варианта", error=True)
            conn.execute("UPDATE quiz_options SET is_active=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_state, option_id))
            audit(
                conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="activate" if new_state else "deactivate", entity_type="quiz_option", entity_id=option_id,
                details={"text": row["text"], "question_id": row["question_id"]},
            )
        return builder_redirect(campaign_id, "Вариант включён" if new_state else "Вариант отключён")

    @app.post("/api/master/quiz-questions/{question_id}/duplicate")
    async def duplicate_quiz_question(request: Request, question_id: int, csrf_token: str = Form(...)):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            row = conn.execute(
                "SELECT qq.*, qc.id AS campaign_id FROM quiz_questions qq JOIN quiz_campaigns qc ON qc.code=qq.campaign_code WHERE qq.id=?",
                (question_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Вопрос не найден")
            title = f"{row['title'][:290]} — копия"
            position = int(conn.execute(
                """
                SELECT COALESCE(MAX(position), 0) + 10
                FROM quiz_questions
                WHERE campaign_code=? AND game_round=?
                """,
                (row["campaign_code"], row["game_round"]),
            ).fetchone()[0])
            cursor = conn.execute(
                """
                INSERT INTO quiz_questions(
                    campaign_code, code, type, title, visual_type, image_path, section_id,
                    placeholder, accepted_text_answers_json, game_round,
                    required, points, time_limit_seconds,
                    position, is_active, created_by_admin_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    row["campaign_code"], f"q_{uuid.uuid4().hex[:10]}", row["type"], title,
                    row["visual_type"], row["image_path"], row["section_id"],
                    row["placeholder"], row["accepted_text_answers_json"],
                    row["game_round"], row["required"],
                    row["points"], row["time_limit_seconds"],
                    position, request.session["admin_id"],
                ),
            )
            new_question_id = int(cursor.lastrowid)
            options = conn.execute(
                "SELECT * FROM quiz_options WHERE question_id=? ORDER BY position, id", (question_id,),
            ).fetchall()
            for option in options:
                conn.execute(
                    """
                    INSERT INTO quiz_options(question_id, code, text, is_correct, position, is_active)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_question_id, f"o_{uuid.uuid4().hex[:10]}", option["text"],
                        option["is_correct"], option["position"], option["is_active"],
                    ),
                )
            audit(
                conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="duplicate", entity_type="quiz_question", entity_id=new_question_id,
                details={"source_question_id": question_id, "title": title},
            )
        return builder_redirect(int(row["campaign_id"]), "Копия вопроса создана")

    @app.post("/api/master/quiz-questions/{question_id}/move")
    async def move_quiz_question(
        request: Request,
        question_id: int,
        direction: str = Form(...),
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        if direction not in {"up", "down"}:
            raise HTTPException(status_code=422, detail="Некорректное направление")
        with transaction(settings.db_path) as conn:
            row = conn.execute(
                "SELECT qq.*, qc.id AS campaign_id FROM quiz_questions qq JOIN quiz_campaigns qc ON qc.code=qq.campaign_code WHERE qq.id=?",
                (question_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Вопрос не найден")
            ordered = [item["id"] for item in conn.execute(
                """
                SELECT id FROM quiz_questions
                WHERE campaign_code=? AND game_round=?
                ORDER BY position, id
                """,
                (row["campaign_code"], row["game_round"]),
            ).fetchall()]
            index = ordered.index(question_id)
            target = index - 1 if direction == "up" else index + 1
            if 0 <= target < len(ordered):
                ordered[index], ordered[target] = ordered[target], ordered[index]
                conn.executemany(
                    "UPDATE quiz_questions SET position=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    [((item_index + 1) * 10, item_id) for item_index, item_id in enumerate(ordered)],
                )
                audit(
                    conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                    action="move", entity_type="quiz_question", entity_id=question_id,
                    details={"direction": direction, "new_position": target + 1},
                )
        return builder_redirect(int(row["campaign_id"]), "Порядок вопросов изменён")

    @app.post("/api/master/quiz-options/{option_id}/move")
    async def move_quiz_option(
        request: Request,
        option_id: int,
        direction: str = Form(...),
        csrf_token: str = Form(...),
    ):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        if direction not in {"up", "down"}:
            raise HTTPException(status_code=422, detail="Некорректное направление")
        with transaction(settings.db_path) as conn:
            row = conn.execute(
                """
                SELECT qo.*, qc.id AS campaign_id FROM quiz_options qo
                JOIN quiz_questions qq ON qq.id=qo.question_id
                JOIN quiz_campaigns qc ON qc.code=qq.campaign_code WHERE qo.id=?
                """,
                (option_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Вариант не найден")
            ordered = [item["id"] for item in conn.execute(
                "SELECT id FROM quiz_options WHERE question_id=? ORDER BY position, id",
                (row["question_id"],),
            ).fetchall()]
            index = ordered.index(option_id)
            target = index - 1 if direction == "up" else index + 1
            if 0 <= target < len(ordered):
                ordered[index], ordered[target] = ordered[target], ordered[index]
                conn.executemany(
                    "UPDATE quiz_options SET position=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    [((item_index + 1) * 10, item_id) for item_index, item_id in enumerate(ordered)],
                )
        return builder_redirect(int(row["campaign_id"]), "Порядок вариантов изменён")

    @app.post("/api/master/quiz-questions/{question_id}/delete")
    async def delete_quiz_question(request: Request, question_id: int, csrf_token: str = Form(...)):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            row = conn.execute(
                "SELECT qq.*, qc.id AS campaign_id FROM quiz_questions qq JOIN quiz_campaigns qc ON qc.code=qq.campaign_code WHERE qq.id=?",
                (question_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Вопрос не найден")
            audit(
                conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="delete", entity_type="quiz_question", entity_id=question_id,
                details={"campaign": row["campaign_code"], "title": row["title"]},
            )
            conn.execute("DELETE FROM quiz_questions WHERE id=?", (question_id,))
        return builder_redirect(int(row["campaign_id"]), "Вопрос удалён")

    def search_clients(q: str, limit: int = 50):
        query = q.strip()
        digits = "".join(character for character in query if character.isdigit())
        like = f"%{query}%"
        phone_like = f"%{digits[-10:]}%" if digits else like
        with connect(settings.db_path) as conn:
            return conn.execute(
                """
                SELECT c.*,
                    COALESCE(MAX(CASE WHEN pt.code='free_entry' THEN cp.balance_int END), 0) AS free_entry,
                    COALESCE(MAX(CASE WHEN pt.code='free_reentry' THEN cp.balance_int END), 0) AS free_reentry,
                    COALESCE(MAX(CASE WHEN pt.code='free_addon' THEN cp.balance_int END), 0) AS free_addon
                FROM clients c
                LEFT JOIN client_preferences cp ON cp.client_id = c.id
                LEFT JOIN preference_types pt ON pt.id = cp.preference_type_id
                WHERE ? = '' OR c.phone_local LIKE ? OR COALESCE(c.phone_raw,'') LIKE ?
                    OR COALESCE(c.nickname,'') LIKE ? OR COALESCE(c.username,'') LIKE ?
                    OR COALESCE(c.first_name,'') LIKE ? OR COALESCE(c.app_user_id,'') LIKE ?
                    OR COALESCE(c.telegram_id,'') LIKE ?
                GROUP BY c.id ORDER BY c.updated_at DESC LIMIT ?
                """,
                (query, phone_like, like, like, like, like, like, like, limit),
            ).fetchall()

    @app.get("/clients", response_class=HTMLResponse)
    async def clients_page(request: Request, q: str = ""):
        require_auth(request)
        clients = search_clients(q) if q else []
        return templates.TemplateResponse(request, "clients.html", context(request, clients=clients, q=q))

    @app.get("/api/clients/search")
    async def clients_api(request: Request, q: str = ""):
        require_auth(request, api=True)
        rows = search_clients(q, limit=25)
        return [{key: row[key] for key in row.keys()} for row in rows]

    def load_client(client_id: int):
        with connect(settings.db_path) as conn:
            client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
            if not client:
                raise HTTPException(status_code=404, detail="Клиент не найден")
            ensure_preferences(conn, client_id)
            prefs = conn.execute(
                """
                SELECT cp.*, pt.code, pt.title, pt.kind
                FROM client_preferences cp JOIN preference_types pt ON pt.id = cp.preference_type_id
                WHERE cp.client_id = ? AND pt.is_active = 1 ORDER BY pt.id
                """,
                (client_id,),
            ).fetchall()
            logs = conn.execute(
                """
                SELECT pl.*, pt.title, pt.kind FROM preference_log pl
                JOIN preference_types pt ON pt.id = pl.preference_type_id
                WHERE pl.client_id = ? ORDER BY pl.id DESC LIMIT 100
                """,
                (client_id,),
            ).fetchall()
            quiz_participation = conn.execute(
                """
                SELECT qps.*, qc.title, qc.max_attempts,
                       EXISTS(SELECT 1 FROM quiz_attempts qa WHERE qa.client_id=qps.client_id AND qa.campaign_code=qps.campaign_code AND qa.status='in_progress') AS has_active_attempt
                FROM quiz_participation_summary qps
                LEFT JOIN quiz_campaigns qc ON qc.code=qps.campaign_code
                WHERE qps.client_id=? ORDER BY qps.updated_at DESC
                """,
                (client_id,),
            ).fetchall()
        return client, prefs, logs, quiz_participation

    @app.get("/clients/{client_id:int}", response_class=HTMLResponse)
    async def client_detail(request: Request, client_id: int, ok: str = "", error: str = ""):
        require_auth(request)
        client, prefs, logs, quiz_participation = load_client(client_id)
        return templates.TemplateResponse(
            request,
            "client_detail.html",
            context(request, client=client, preferences=prefs, logs=logs, quiz_participation=quiz_participation, ok=ok, error=error),
        )

    @app.post("/api/clients/{client_id:int}/quiz/{campaign}/extra-attempt")
    async def client_extra_quiz_attempt(request: Request, client_id: int, campaign: str, csrf_token: str = Form(...)):
        require_auth(request, api=True)
        check_csrf(request, csrf_token)
        campaign = normalize_campaign(campaign)
        with transaction(settings.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM quiz_participation_summary WHERE client_id=? AND campaign_code=?",
                (client_id, campaign),
            ).fetchone()
            if not row:
                return RedirectResponse(f"/clients/{client_id}?error={quote('Участие в кампании не найдено')}", status_code=303)
            conn.execute(
                "UPDATE quiz_participation_summary SET attempts_used=MAX(0, attempts_used-1), updated_at=CURRENT_TIMESTAMP WHERE client_id=? AND campaign_code=?",
                (client_id, campaign),
            )
            audit(
                conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="extra_attempt", entity_type="quiz_participation", entity_id=client_id,
                details={"campaign": campaign, "previous_attempts_used": row["attempts_used"]},
            )
        return RedirectResponse(f"/clients/{client_id}?ok={quote('Дополнительная попытка предоставлена')}", status_code=303)

    @app.post("/api/clients/{client_id:int}/quiz/{campaign}/reset-attempts")
    async def client_reset_quiz_attempts(request: Request, client_id: int, campaign: str, csrf_token: str = Form(...)):
        require_master(request, api=True)
        check_csrf(request, csrf_token)
        campaign = normalize_campaign(campaign)
        with transaction(settings.db_path) as conn:
            conn.execute(
                "UPDATE quiz_attempts SET status='expired', finished_at=CURRENT_TIMESTAMP WHERE client_id=? AND campaign_code=? AND status='in_progress'",
                (client_id, campaign),
            )
            conn.execute(
                "UPDATE quiz_participation_summary SET attempts_used=0, updated_at=CURRENT_TIMESTAMP WHERE client_id=? AND campaign_code=?",
                (client_id, campaign),
            )
            audit(
                conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="reset_attempts", entity_type="quiz_participation", entity_id=client_id,
                details={"campaign": campaign},
            )
        return RedirectResponse(f"/clients/{client_id}?ok={quote('Счётчик попыток сброшен')}", status_code=303)

    @app.post("/api/clients/{client_id:int}/quiz/{campaign}/finish-active")
    async def client_finish_active_quiz(request: Request, client_id: int, campaign: str, csrf_token: str = Form(...)):
        require_auth(request, api=True)
        check_csrf(request, csrf_token)
        campaign = normalize_campaign(campaign)
        with transaction(settings.db_path) as conn:
            changed = conn.execute(
                "UPDATE quiz_attempts SET status='expired', finished_at=CURRENT_TIMESTAMP WHERE client_id=? AND campaign_code=? AND status='in_progress'",
                (client_id, campaign),
            ).rowcount
            audit(
                conn, admin_id=request.session["admin_id"], admin_name=request.session["admin_name"],
                action="finish_active_attempt", entity_type="quiz_participation", entity_id=client_id,
                details={"campaign": campaign, "changed": changed},
            )
        message = "Активная попытка завершена" if changed else "Активная попытка не найдена"
        return RedirectResponse(f"/clients/{client_id}?ok={quote(message)}", status_code=303)

    @app.get("/api/clients/{client_id}/qr")
    async def client_qr(request: Request, client_id: int):
        require_auth(request, api=True)
        with connect(settings.db_path) as conn:
            row = conn.execute("SELECT phone_local FROM clients WHERE id = ?", (client_id,)).fetchone()
        if not row or not row["phone_local"]:
            raise HTTPException(status_code=404, detail="qr_phone_unavailable")
        image = qrcode.make(row["phone_local"], border=3, box_size=12)
        output = io.BytesIO()
        image.save(output, format="PNG")
        return Response(output.getvalue(), media_type="image/png", headers={"Cache-Control": "private, no-store"})

    @app.post("/api/preferences/add")
    async def preference_add(
        request: Request,
        client_id: int = Form(...),
        code: str = Form(...),
        amount: int = Form(...),
        reason: str = Form(...),
        comment: str = Form(""),
        csrf_token: str = Form(...),
    ):
        require_auth(request, api=True)
        check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                ensure_preferences(conn, client_id)
                change_counter(conn, client_id=client_id, code=code, delta=abs(amount), reason=reason, comment=comment, admin_name=request.session["admin_name"])
        except ValueError as exc:
            return RedirectResponse(f"/clients/{client_id}?error={exc}", status_code=303)
        return RedirectResponse(f"/clients/{client_id}?ok=Начисление+сохранено", status_code=303)

    @app.post("/api/preferences/spend")
    async def preference_spend(
        request: Request,
        client_id: int = Form(...),
        code: str = Form(...),
        amount: int = Form(...),
        reason: str = Form(...),
        comment: str = Form(""),
        csrf_token: str = Form(...),
    ):
        require_auth(request, api=True)
        check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                ensure_preferences(conn, client_id)
                change_counter(conn, client_id=client_id, code=code, delta=-abs(amount), reason=reason, comment=comment, admin_name=request.session["admin_name"])
        except ValueError as exc:
            return RedirectResponse(f"/clients/{client_id}?error={exc}", status_code=303)
        return RedirectResponse(f"/clients/{client_id}?ok=Списание+сохранено", status_code=303)

    @app.post("/api/preferences/set-discount")
    async def preference_discount(
        request: Request,
        client_id: int = Form(...),
        code: str = Form(...),
        percent: float = Form(...),
        reason: str = Form(...),
        comment: str = Form(""),
        csrf_token: str = Form(...),
    ):
        require_auth(request, api=True)
        check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                ensure_preferences(conn, client_id)
                set_percent(conn, client_id=client_id, code=code, percent=percent, reason=reason, comment=comment, admin_name=request.session["admin_name"])
        except ValueError as exc:
            return RedirectResponse(f"/clients/{client_id}?error={exc}", status_code=303)
        return RedirectResponse(f"/clients/{client_id}?ok=Скидка+сохранена", status_code=303)

    @app.post("/api/clients/{client_id}/comment")
    async def client_comment(request: Request, client_id: int, comment: str = Form(""), csrf_token: str = Form(...)):
        require_auth(request, api=True)
        check_csrf(request, csrf_token)
        with transaction(settings.db_path) as conn:
            conn.execute("UPDATE clients SET comment = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (comment.strip(), client_id))
        return RedirectResponse(f"/clients/{client_id}?ok=Комментарий+сохранён", status_code=303)

    @app.get("/clients/import", response_class=HTMLResponse)
    async def import_page(request: Request):
        require_auth(request)
        return templates.TemplateResponse(request, "import.html", context(request, preview=None, result=None, fields=FIELD_LABELS))

    @app.post("/clients/import", response_class=HTMLResponse)
    async def import_preview(request: Request, upload: UploadFile = File(...), csrf_token: str = Form(...)):
        require_auth(request)
        check_csrf(request, csrf_token)
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in {".csv", ".xlsx"}:
            raise HTTPException(status_code=400, detail="Поддерживаются только CSV и XLSX")
        content = await upload.read(settings.max_upload_mb * 1024 * 1024 + 1)
        if len(content) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Файл слишком большой")
        upload_dir = BASE_DIR / "data" / "uploads"
        saved_name = f"{uuid.uuid4().hex}{suffix}"
        path = upload_dir / saved_name
        path.write_bytes(content)
        try:
            headers, rows = read_tabular(path)
        except Exception:
            path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Не удалось прочитать файл")
        preview = {
            "token": saved_name,
            "filename": Path(upload.filename or "import").name,
            "headers": headers,
            "rows": rows[:10],
            "total": len(rows),
            "mapping": detect_mapping(headers),
        }
        return templates.TemplateResponse(request, "import.html", context(request, preview=preview, result=None, fields=FIELD_LABELS))

    @app.post("/api/import/clients", response_class=HTMLResponse)
    async def import_commit(request: Request):
        require_auth(request, api=True)
        form = await request.form()
        check_csrf(request, str(form.get("csrf_token", "")))
        token = Path(str(form.get("token", ""))).name
        filename = Path(str(form.get("filename", "import"))).name
        path = BASE_DIR / "data" / "uploads" / token
        if not path.exists() or path.suffix.lower() not in {".csv", ".xlsx"}:
            raise HTTPException(status_code=400, detail="Файл предпросмотра не найден")
        mapping = {field: str(form.get(f"map_{field}", "")) for field in FIELD_LABELS}
        if not mapping.get("phone_raw") and not mapping.get("app_user_id"):
            raise HTTPException(status_code=400, detail="Укажите колонку Phone или ID")
        headers, rows = read_tabular(path)
        if any(column and column not in headers for column in mapping.values()):
            raise HTTPException(status_code=400, detail="Некорректное сопоставление колонок")
        with transaction(settings.db_path) as conn:
            stats = import_rows(conn, rows, mapping)
            conn.execute(
                """
                INSERT INTO import_batches(filename, total_rows, inserted_count, updated_count,
                    skipped_count, phone_error_count, duplicate_count, admin_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (filename, stats["total"], stats["inserted"], stats["updated"], stats["skipped"], stats["phone_errors"], stats["duplicates"], request.session["admin_name"]),
            )
        path.unlink(missing_ok=True)
        return templates.TemplateResponse(request, "import.html", context(request, preview=None, result=stats, fields=FIELD_LABELS))

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(
        request: Request,
        q: str = "",
        preference: str = "",
        operation: str = "",
        admin: str = "",
        date_from: str = "",
        date_to: str = "",
    ):
        require_auth(request)
        clauses = ["1=1"]
        params: list[Any] = []
        if q:
            clauses.append("(c.first_name LIKE ? OR c.nickname LIKE ? OR c.phone_local LIKE ?)")
            params.extend([f"%{q}%"] * 3)
        if preference:
            clauses.append("pt.code = ?")
            params.append(preference)
        if operation:
            clauses.append("pl.operation_type = ?")
            params.append(operation)
        if admin:
            clauses.append("pl.admin_name LIKE ?")
            params.append(f"%{admin}%")
        if date_from:
            clauses.append("date(pl.created_at) >= date(?)")
            params.append(date_from)
        if date_to:
            clauses.append("date(pl.created_at) <= date(?)")
            params.append(date_to)
        with connect(settings.db_path) as conn:
            logs = conn.execute(
                f"""
                SELECT pl.*, c.first_name, c.nickname, c.phone_local, pt.title, pt.code, pt.kind
                FROM preference_log pl JOIN clients c ON c.id = pl.client_id
                JOIN preference_types pt ON pt.id = pl.preference_type_id
                WHERE {' AND '.join(clauses)} ORDER BY pl.id DESC LIMIT 500
                """,
                params,
            ).fetchall()
            preference_types = conn.execute(
                "SELECT code, title FROM preference_types ORDER BY position, id"
            ).fetchall()
        filters = {"q": q, "preference": preference, "operation": operation, "admin": admin, "date_from": date_from, "date_to": date_to}
        return templates.TemplateResponse(request, "logs.html", context(request, logs=logs, filters=filters, preference_types=preference_types))

    def csv_response(filename: str, headers: list[str], rows):
        def safe_cell(value: Any) -> Any:
            if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
                return "'" + value
            return value

        output = io.StringIO()
        output.write("\ufeff")
        writer = csv.writer(output, delimiter=";")
        writer.writerow(headers)
        writer.writerows([safe_cell(value) for value in row] for row in rows)
        return StreamingResponse(iter([output.getvalue()]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/api/quiz/results.csv")
    async def export_quiz_results(
        request: Request,
        campaign: str = "", phone: str = "", username: str = "", bonus: str = "",
        date_from: str = "", date_to: str = "",
    ):
        require_auth(request, api=True)
        where, params = quiz_result_filters(campaign, phone, username, bonus, date_from, date_to)
        rows = load_quiz_results(where, params, limit=100_000)
        headers = [
            "submission_id", "created_at", "campaign", "client_id", "name", "username",
            "nickname", "phone_local", "answers", "score", "max_score", "correct_count",
            "max_correct_count", "passed", "bonus_granted", "bonus_pending",
            "bonus_type", "completion_time_ms", "main_prize_eligible",
            "jackcoin_awarded", "streak_days", "duplicate", "new_or_existing",
            "quiz_referrer_id", "source",
        ]
        values = (
            (
                row["id"], row["created_at"], row["campaign_code"], row["client_id"], row["name"],
                row["username"], row["nickname"], row["phone_local"], row["answers_json"], row["score"],
                row["max_score"], row["correct_count"], row["max_correct_count"], row["passed"],
                row["bonus_granted"], row["bonus_pending"], row["bonus_type"],
                row["completion_time_ms"], row["main_prize_eligible"],
                row["jackcoin_awarded"], row["streak_days"], row["is_duplicate"],
                "new" if row["is_new_client"] else "existing",
                row["quiz_referrer_id"], row["source"],
            )
            for row in rows
        )
        return csv_response("quiz_results.csv", headers, values)

    @app.get("/api/export/clients.csv")
    async def export_clients(request: Request):
        require_auth(request, api=True)
        with connect(settings.db_path) as conn:
            rows = conn.execute("SELECT app_user_id, telegram_id, referrer_app_user_id, nickname, username, first_name, phone_raw, phone_local, source, comment, created_at, updated_at FROM clients ORDER BY id").fetchall()
        headers = ["ID", "Telegram ID", "Referrer ID", "Nickname", "Username", "First name", "Phone", "Phone local", "Source", "Comment", "Created at", "Updated at"]
        return csv_response("clients.csv", headers, ([row[key] for key in row.keys()] for row in rows))

    @app.get("/api/export/preference-log.csv")
    async def export_logs(request: Request):
        require_auth(request, api=True)
        with connect(settings.db_path) as conn:
            rows = conn.execute(
                """
                SELECT pl.created_at, c.first_name, c.nickname, c.phone_local, pt.title,
                    pl.operation_type, pl.delta_int, pl.old_balance_int, pl.new_balance_int,
                    pl.old_percent_value, pl.new_percent_value, pl.reason, pl.comment, pl.admin_name
                FROM preference_log pl JOIN clients c ON c.id=pl.client_id
                JOIN preference_types pt ON pt.id=pl.preference_type_id ORDER BY pl.id
                """
            ).fetchall()
        headers = ["Date", "First name", "Nickname", "Phone", "Preference", "Operation", "Delta", "Old balance", "New balance", "Old percent", "New percent", "Reason", "Comment", "Admin"]
        return csv_response("preference_log.csv", headers, ([row[key] for key in row.keys()] for row in rows))

    return app


app = create_app()
