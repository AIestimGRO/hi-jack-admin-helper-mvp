from __future__ import annotations

import io

import qrcode
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from app.product_shell import _current_member
from app.services.phone import normalize_phone


def member_identity_payload(phone_local: str | None) -> str:
    normalized = normalize_phone(phone_local)
    if not normalized:
        raise ValueError("member_phone_unavailable")
    return normalized


def install_member_identity_qr(app: FastAPI) -> FastAPI:
    if getattr(app.state, "member_identity_qr_installed", False):
        return app
    app.state.member_identity_qr_installed = True

    @app.get("/account/identity/qr.png")
    async def member_identity_qr(request: Request):
        member = _current_member(request, required=True)
        try:
            payload = member_identity_payload(str(member["phone_local"] or ""))
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail="Для клубной карты нужен номер телефона",
            ) from exc

        image = qrcode.make(payload)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return Response(
            buffer.getvalue(),
            media_type="image/png",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return app


__all__ = ["install_member_identity_qr", "member_identity_payload"]
