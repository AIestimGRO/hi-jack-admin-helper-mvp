from __future__ import annotations

from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.routing import APIRoute

from app.db import connect
from app.product_shell import _current_member
from app.services.member_accounts import verify_password


_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


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


def install_launch_security_hardening(app: FastAPI) -> FastAPI:
    if getattr(app.state, "launch_security_hardening_installed", False):
        return app
    app.state.launch_security_hardening_installed = True
    _install_email_change_reauth(app)

    @app.middleware("http")
    async def launch_security_middleware(request: Request, call_next):
        # The legacy deep probe performs a database write. A public caller must
        # never be able to turn a health endpoint into an SQLite write loop.
        if request.url.path == "/health/deep":
            return JSONResponse({"error": "not_found"}, status_code=404)

        if request.session.get("authenticated") and not _admin_session_valid(request):
            return _invalid_admin_session_response(request)

        response = await call_next(request)

        location = response.headers.get("location")
        safe_location = _sanitize_redirect_location(location)
        if location and safe_location != location:
            response.headers["location"] = safe_location or "/account"

        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    return app


__all__ = [
    "install_launch_security_hardening",
]
