from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from app.admin_access_control import (
    ACCESS_BARTENDER,
    ACCESS_QUIZ_MANAGER,
    effective_access_role,
)
from app.db import connect


_SCOPED_ENTRY_PATHS = frozenset({"/", "/master", "/master/"})


def scoped_admin_landing(access_role: str) -> str | None:
    if access_role == ACCESS_QUIZ_MANAGER:
        return "/staff/quizzes"
    if access_role == ACCESS_BARTENDER:
        return "/clients"
    return None


def install_staff_admin_entry(app: FastAPI) -> FastAPI:
    if getattr(app.state, "staff_admin_entry_installed", False):
        return app
    app.state.staff_admin_entry_installed = True
    settings = app.state.settings

    @app.middleware("http")
    async def scoped_staff_entry_middleware(request: Request, call_next):
        if (
            request.method != "GET"
            or request.url.path not in _SCOPED_ENTRY_PATHS
            or not request.session.get("authenticated")
        ):
            return await call_next(request)

        admin_id = request.session.get("admin_id")
        if not admin_id:
            return await call_next(request)

        with connect(settings.db_path) as conn:
            admin = conn.execute(
                "SELECT role,is_active FROM admins WHERE id=?",
                (int(admin_id),),
            ).fetchone()
            if not admin or not int(admin["is_active"] or 0):
                return await call_next(request)
            access_role = effective_access_role(
                conn,
                admin_id=int(admin_id),
                base_role=str(admin["role"]),
            )

        landing = scoped_admin_landing(access_role)
        if landing:
            return RedirectResponse(landing, status_code=303)
        return await call_next(request)

    return app


__all__ = ["install_staff_admin_entry", "scoped_admin_landing"]
