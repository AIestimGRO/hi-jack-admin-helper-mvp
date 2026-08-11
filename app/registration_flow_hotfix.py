from __future__ import annotations

from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.db import connect
from app.legal_registration import _is_adult, _move_latest_route_before_existing
from app.member_account_management import normalize_birth_date
from app.product_shell import _check_csrf
from app.services.member_accounts import active_legal_documents


_ALLOWED_DOCUMENTS = ("privacy", "rewards")


def _registration_redirect(message: str = "", *, error: bool = False) -> RedirectResponse:
    path = "/account/register"
    if message:
        key = "error" if error else "ok"
        path = f"{path}?{urlencode({key: message})}"
    return RedirectResponse(path, status_code=303)


def _registration_flow(request: Request) -> dict:
    flow = request.session.get("member_registration_flow")
    if not isinstance(flow, dict):
        flow = {"accepted": {}}
    accepted = flow.get("accepted")
    if not isinstance(accepted, dict):
        accepted = {}
    flow["accepted"] = accepted
    request.session["member_registration_flow"] = flow
    return flow


def install_registration_flow_hotfix(app: FastAPI) -> FastAPI:
    if getattr(app.state, "registration_flow_hotfix_installed", False):
        return app
    app.state.registration_flow_hotfix_installed = True
    settings = app.state.settings

    @app.post("/account/register/consent")
    async def member_register_consent_idempotent(
        request: Request,
        document_code: str = Form(...),
        accepted: bool = Form(False),
        csrf_token: str = Form(...),
    ):
        _check_csrf(request, csrf_token)
        if document_code not in _ALLOWED_DOCUMENTS or not accepted:
            return _registration_redirect("Consent is required", error=True)

        flow = _registration_flow(request)
        accepted_map = flow["accepted"]

        # A repeated browser submit must never destroy already accepted steps.
        if document_code in accepted_map:
            return _registration_redirect()

        expected = "privacy" if "privacy" not in accepted_map else "rewards"
        if document_code != expected:
            return _registration_redirect("Complete the previous consent step", error=True)

        with connect(settings.db_path) as conn:
            document = active_legal_documents(conn).get(document_code)
        if not document:
            return _registration_redirect("Legal document is unavailable", error=True)

        accepted_map[document_code] = {
            "version": str(document["version"]),
            "accepted_at": None,
        }
        flow["accepted"] = accepted_map
        request.session["member_registration_flow"] = flow
        return _registration_redirect()

    _move_latest_route_before_existing(app, "/account/register/consent", "POST")

    @app.post("/api/account/register/legal-extra")
    async def registration_legal_extra_persistent(
        request: Request,
        birth_date: str = Form(...),
        marketing: bool = Form(False),
        csrf_token: str = Form(...),
    ):
        _check_csrf(request, csrf_token)
        try:
            normalized = normalize_birth_date(birth_date)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)

        if not normalized or not _is_adult(normalized):
            return JSONResponse(
                {"ok": False, "error": "Регистрация доступна с 18 лет"},
                status_code=409,
            )

        # Keep all profile-side registration state in the same signed session.
        # The server-side request-code gate therefore does not depend on JS timing.
        request.session["member_registration_birth_date"] = normalized
        request.session["member_registration_marketing"] = bool(marketing)
        return JSONResponse({"ok": True})

    _move_latest_route_before_existing(
        app, "/api/account/register/legal-extra", "POST"
    )
    return app


__all__ = ["install_registration_flow_hotfix"]
