from __future__ import annotations

from fastapi import FastAPI, Request


_VAULT_CAMERA_POLICY = "camera=(self), microphone=(), geolocation=()"


def install_admin_vault_scanner(app: FastAPI) -> FastAPI:
    if getattr(app.state, "admin_vault_scanner_installed", False):
        return app
    app.state.admin_vault_scanner_installed = True

    @app.middleware("http")
    async def admin_vault_camera_policy(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/admin/vault":
            response.headers["Permissions-Policy"] = _VAULT_CAMERA_POLICY
        return response

    return app


__all__ = ["install_admin_vault_scanner"]
