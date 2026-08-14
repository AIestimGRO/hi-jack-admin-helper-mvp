from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse


MEMBER_HOSTS = frozenset({"club-v2.hijackpoker.ru", "club.hijackpoker.ru"})
ADMIN_HOSTS = frozenset({"quiz-v2.hijackpoker.ru"})
MEMBER_CANONICAL_HOST = "club-v2.hijackpoker.ru"
ADMIN_CANONICAL_HOST = "quiz-v2.hijackpoker.ru"


def member_host_redirect_target(hostname: str | None, path: str) -> str | None:
    host = str(hostname or "").strip().lower().rstrip(".")

    if host in MEMBER_HOSTS:
        if path in {"/", "/login"}:
            return "/account/login"
        if path == "/logout" or path == "/master" or path.startswith("/master/"):
            return f"https://{ADMIN_CANONICAL_HOST}{path}"
        return None

    if host in ADMIN_HOSTS:
        if (
            path == "/account"
            or path.startswith("/account/")
            or path == "/players"
            or path.startswith("/players/")
            or path.startswith("/legal/")
        ):
            return f"https://{MEMBER_CANONICAL_HOST}{path}"

    return None


def _with_query(target: str, query: str) -> str:
    value = str(query or "")
    if not value:
        return target
    separator = "&" if "?" in target else "?"
    return f"{target}{separator}{value}"


def install_member_host_routing(app: FastAPI) -> FastAPI:
    if getattr(app.state, "member_host_routing_installed", False):
        return app
    app.state.member_host_routing_installed = True

    @app.middleware("http")
    async def member_host_routing_middleware(request: Request, call_next):
        if request.method in {"GET", "HEAD"}:
            target = member_host_redirect_target(request.url.hostname, request.url.path)
            if target:
                return RedirectResponse(
                    _with_query(target, request.url.query),
                    status_code=303,
                )
        return await call_next(request)

    return app


__all__ = [
    "ADMIN_HOSTS",
    "MEMBER_HOSTS",
    "install_member_host_routing",
    "member_host_redirect_target",
]
