from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse


MEMBER_HOSTS = frozenset({"club-v2.hijackpoker.ru", "club.hijackpoker.ru"})


def member_host_redirect_target(hostname: str | None, path: str) -> str | None:
    host = str(hostname or "").strip().lower().rstrip(".")
    if host not in MEMBER_HOSTS:
        return None
    if path in {"/", "/login"}:
        return "/account/login"
    return None


def install_member_host_routing(app: FastAPI) -> FastAPI:
    if getattr(app.state, "member_host_routing_installed", False):
        return app
    app.state.member_host_routing_installed = True

    @app.middleware("http")
    async def member_host_routing_middleware(request: Request, call_next):
        if request.method in {"GET", "HEAD"}:
            target = member_host_redirect_target(request.url.hostname, request.url.path)
            if target:
                return RedirectResponse(target, status_code=303)
        return await call_next(request)

    return app


__all__ = [
    "MEMBER_HOSTS",
    "install_member_host_routing",
    "member_host_redirect_target",
]
