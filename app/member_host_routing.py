from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse


MEMBER_TO_ADMIN_HOST = {
    "club-v2.hijackpoker.ru": "quiz-v2.hijackpoker.ru",
    "club.hijackpoker.ru": "quiz.hijackpoker.ru",
}
ADMIN_TO_MEMBER_HOST = {admin: member for member, admin in MEMBER_TO_ADMIN_HOST.items()}
MEMBER_HOSTS = frozenset(MEMBER_TO_ADMIN_HOST)
ADMIN_HOSTS = frozenset(ADMIN_TO_MEMBER_HOST)


def member_host_redirect_target(hostname: str | None, path: str) -> str | None:
    host = str(hostname or "").strip().lower().rstrip(".")

    admin_host = MEMBER_TO_ADMIN_HOST.get(host)
    if admin_host:
        if path in {"/", "/login"}:
            return "/account/login"
        if path == "/logout" or path == "/master" or path.startswith("/master/"):
            return f"https://{admin_host}{path}"
        return None

    member_host = ADMIN_TO_MEMBER_HOST.get(host)
    if member_host:
        if (
            path == "/account"
            or path.startswith("/account/")
            or path == "/players"
            or path.startswith("/players/")
            or path.startswith("/legal/")
        ):
            return f"https://{member_host}{path}"

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
    "ADMIN_TO_MEMBER_HOST",
    "MEMBER_HOSTS",
    "MEMBER_TO_ADMIN_HOST",
    "install_member_host_routing",
    "member_host_redirect_target",
]
