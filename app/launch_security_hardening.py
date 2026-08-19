from __future__ import annotations

import io
import json
import math
import threading
import time
from collections import deque
from datetime import date
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.routing import APIRoute
from PIL import Image, UnidentifiedImageError

from app import prelaunch_experience as experience
from app import prelaunch_profile_sharing as profile_sharing
from app import product_shell
from app.db import connect
from app.product_shell import _current_member
from app.services.member_accounts import verify_password


_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
_PRIVATE_CACHE_PREFIXES = (
    "/account",
    "/master",
    "/admin",
    "/staff",
    "/clients",
    "/login",
    "/api/account",
    "/api/admin",
    "/api/master",
    "/api/staff",
    "/api/clients",
)
_PUBLIC_LEGAL_KEYS = (
    "nickname",
    "avatar",
    "result",
    "place",
    "titles",
    "achievements",
)

# App-side protection is intentionally independent of nginx. Login endpoints get a
# relatively generous burst allowance; endpoints capable of sending email are much
# tighter so one client cannot turn the public portal into an SMTP relay/spam source.
_AUTH_RATE_RULES: dict[tuple[str, str], tuple[int, int]] = {
    ("POST", "/login"): (30, 60),
    ("POST", "/account/login"): (30, 60),
    ("POST", "/account/register/request-code"): (5, 300),
    ("POST", "/account/register/resend-code"): (5, 300),
    ("POST", "/account/forgot-password"): (5, 300),
    ("POST", "/account/security/email/request"): (5, 300),
    ("POST", "/account/security/phone/request"): (5, 300),
    ("POST", "/account/security/delete/request"): (5, 300),
}
_RATE_LIMIT_MAX_BUCKETS = 8192
_RATE_LIMIT_STALE_SECONDS = 600


class _SlidingWindowLimiter:
    def __init__(self) -> None:
        self._events: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        if len(self._events) < _RATE_LIMIT_MAX_BUCKETS:
            return
        stale_before = float(now) - _RATE_LIMIT_STALE_SECONDS
        for key, events in list(self._events.items()):
            if not events or events[-1] <= stale_before:
                self._events.pop(key, None)
        if len(self._events) < _RATE_LIMIT_MAX_BUCKETS:
            return
        overflow = len(self._events) - _RATE_LIMIT_MAX_BUCKETS + 1
        for key in list(self._events)[:overflow]:
            self._events.pop(key, None)

    def check(
        self,
        *,
        bucket: str,
        client_key: str,
        now: float,
        limit: int,
        window_seconds: int,
    ) -> int | None:
        key = (str(bucket), str(client_key))
        cutoff = float(now) - int(window_seconds)
        with self._lock:
            self._prune(float(now))
            events = self._events.setdefault(key, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= int(limit):
                retry_after = events[0] + int(window_seconds) - float(now)
                return max(1, int(math.ceil(retry_after)))
            events.append(float(now))
        return None


def _client_rate_key(request: Request) -> str:
    # Uvicorn trusts proxy headers only from nginx on production, so request.client
    # is the normalized client address without trusting arbitrary X-Forwarded-For.
    return str(request.client.host if request.client else "unknown")


def _rate_limit_response(request: Request, retry_after: int):
    headers = {
        **_SECURITY_HEADERS,
        "Retry-After": str(max(1, int(retry_after))),
        "Cache-Control": "no-store",
    }
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"error": "rate_limited", "retry_after": int(retry_after)},
            status_code=429,
            headers=headers,
        )
    return PlainTextResponse(
        "Слишком много запросов. Попробуйте чуть позже.",
        status_code=429,
        headers=headers,
    )


def _admin_session_valid(request: Request) -> bool:
    if not request.session.get("authenticated"):
        return True
    admin_id = request.session.get("admin_id")
    session_version = request.session.get("admin_session_version")
    if admin_id is None or session_version is None:
        return False
    try:
        expected_admin_id = int(admin_id)
        expected_version = int(session_version)
    except (TypeError, ValueError):
        return False
    with connect(request.app.state.settings.db_path) as conn:
        row = conn.execute(
            """
            SELECT is_active,session_version
            FROM admins
            WHERE id=?
            """,
            (expected_admin_id,),
        ).fetchone()
    return bool(
        row
        and int(row["is_active"] or 0) == 1
        and int(row["session_version"] or 0) == expected_version
    )


def _invalid_admin_session_response(request: Request):
    request.session.clear()
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "authentication_required"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


def _sanitize_redirect_location(value: str | None) -> str | None:
    location = str(value or "").strip()
    if not location:
        return None
    if location.startswith("//"):
        return "/account"
    return location


def _profile_settings_redirect(message: str, *, error: bool = False) -> RedirectResponse:
    query = {"tab": "profile", "view": "settings", "error" if error else "ok": message}
    return RedirectResponse(f"/account?{urlencode(query)}", status_code=303)


def _is_adult_birth_date(value: str, *, today: date | None = None) -> bool:
    try:
        born = date.fromisoformat(str(value or "").strip())
    except ValueError:
        return False
    current = today or date.today()
    years = current.year - born.year
    if (current.month, current.day) < (born.month, born.day):
        years -= 1
    return years >= 18


def _safe_open_image(data: bytes) -> Image.Image:
    """Reject oversized raster dimensions before Pillow fully decodes pixels."""
    try:
        image = Image.open(io.BytesIO(data))
        if image.width < 32 or image.height < 32:
            raise ValueError("Изображение слишком маленькое")
        if image.width > 8192 or image.height > 8192:
            raise ValueError("Изображение слишком большое")
        image.load()
        return image
    except ValueError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
        raise ValueError("Файл не является поддерживаемым изображением") from exc


def _install_safe_image_decoder() -> None:
    product_shell._open_image = _safe_open_image  # noqa: SLF001


def _privacy_safe_rating_categories(conn, account_id: int) -> dict[str, bool]:
    """Visibility may narrow legal consent, never expand it."""
    consent = conn.execute(
        """
        SELECT s.categories_json,s.granted,s.document_version,
               d.version AS active_version
        FROM member_public_rating_consent_state s
        LEFT JOIN legal_reference_documents d
          ON d.code='public-rating-consent' AND d.is_active=1
        WHERE s.account_id=?
        """,
        (int(account_id),),
    ).fetchone()
    if not consent or not int(consent["granted"] or 0):
        return {}
    if not consent["active_version"] or str(consent["document_version"]) != str(
        consent["active_version"]
    ):
        return {}
    try:
        granted = json.loads(str(consent["categories_json"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(granted, dict):
        return {}

    visibility = profile_sharing._profile_visibility(conn, int(account_id))  # noqa: SLF001
    result = {
        key: bool(granted.get(key)) and bool(visibility.get(key))
        for key in _PUBLIC_LEGAL_KEYS
    }
    participation_granted = bool(granted.get("participation_stats"))
    game_stats = participation_granted and bool(visibility.get("game_stats"))
    game_history = participation_granted and bool(visibility.get("game_history"))
    result["participation_stats"] = bool(game_stats or game_history)
    result["game_stats"] = game_stats
    result["game_history"] = game_history
    return result if any(result.values()) else {}


def _install_public_profile_consent_guard() -> None:
    experience._rating_categories = _privacy_safe_rating_categories  # noqa: SLF001


def _install_email_change_reauth(app: FastAPI) -> None:
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path != "/account/security/email/request":
            continue
        if "POST" not in (route.methods or set()):
            continue
        if getattr(route, "_launch_email_reauth_wrapped", False):
            return

        original = route.endpoint

        async def email_change_reauth_wrapper(
            request: Request,
            new_email: str,
            csrf_token: str,
        ):
            form = await request.form()
            current_password = str(form.get("current_password") or "")
            member = _current_member(request, required=True)
            if not current_password or not verify_password(
                current_password,
                str(member["password_hash"] or ""),
            ):
                return _profile_settings_redirect(
                    "Введите текущий пароль для смены почты",
                    error=True,
                )
            return await original(
                request=request,
                new_email=new_email,
                csrf_token=csrf_token,
            )

        route.endpoint = email_change_reauth_wrapper
        route.dependant.call = email_change_reauth_wrapper
        setattr(route, "_launch_email_reauth_wrapped", True)
        return


def _install_birthday_adult_gate(app: FastAPI) -> None:
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path != "/account/birthday":
            continue
        if "POST" not in (route.methods or set()):
            continue
        if getattr(route, "_launch_adult_gate_wrapped", False):
            return

        original = route.endpoint

        async def birthday_adult_wrapper(
            request: Request,
            birth_date: str,
            csrf_token: str,
        ):
            if not _is_adult_birth_date(birth_date):
                return RedirectResponse(
                    "/account/birthday?"
                    + urlencode({"error": "Регистрация и использование сервиса доступны с 18 лет"}),
                    status_code=303,
                )
            return await original(
                request=request,
                birth_date=birth_date,
                csrf_token=csrf_token,
            )

        route.endpoint = birthday_adult_wrapper
        route.dependant.call = birthday_adult_wrapper
        setattr(route, "_launch_adult_gate_wrapped", True)
        return


def install_launch_security_hardening(app: FastAPI) -> FastAPI:
    if getattr(app.state, "launch_security_hardening_installed", False):
        return app
    app.state.launch_security_hardening_installed = True
    app.state.launch_auth_rate_limiter = _SlidingWindowLimiter()
    _install_email_change_reauth(app)
    _install_birthday_adult_gate(app)
    _install_safe_image_decoder()
    _install_public_profile_consent_guard()

    @app.middleware("http")
    async def launch_security_middleware(request: Request, call_next):
        # The legacy deep probe performs a database write. A public caller must
        # never be able to turn a health endpoint into an SQLite write loop.
        if request.url.path == "/health/deep":
            return JSONResponse({"error": "not_found"}, status_code=404)

        rule = _AUTH_RATE_RULES.get((request.method.upper(), request.url.path))
        if rule:
            limit, window_seconds = rule
            retry_after = request.app.state.launch_auth_rate_limiter.check(
                bucket=f"{request.method.upper()}:{request.url.path}",
                client_key=_client_rate_key(request),
                now=time.monotonic(),
                limit=limit,
                window_seconds=window_seconds,
            )
            if retry_after is not None:
                return _rate_limit_response(request, retry_after)

        if request.session.get("authenticated") and not _admin_session_valid(request):
            return _invalid_admin_session_response(request)

        response = await call_next(request)

        location = response.headers.get("location")
        safe_location = _sanitize_redirect_location(location)
        if location and safe_location != location:
            response.headers["location"] = safe_location or "/account"

        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path.startswith(_PRIVATE_CACHE_PREFIXES):
            response.headers["Cache-Control"] = "no-store"
        return response

    return app


__all__ = [
    "install_launch_security_hardening",
]
