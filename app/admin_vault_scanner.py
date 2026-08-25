from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse

from app.db import connect
from app.product_shell import _check_csrf, _require_master


_VAULT_CAMERA_POLICY = "camera=(self), microphone=(), geolocation=()"
_CAMERA_PATHS = frozenset({"/admin/vault", "/master/clients"})
_CARD_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_CLIENT_PATH_RE = re.compile(r"^/clients/(?P<client_id>[1-9][0-9]*)/?$")


def _client_name(row) -> str:
    first_name = str(row["first_name"] or "").strip()
    nickname = str(row["nickname"] or "").strip()
    username = str(row["username"] or "").strip().lstrip("@")
    if first_name:
        return first_name
    if nickname:
        return nickname
    if username:
        return f"@{username}"
    return f"Клиент #{int(row['id'])}"


def _client_id_from_scan(raw_value: str) -> int | None:
    raw = str(raw_value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    match = _CLIENT_PATH_RE.fullmatch(parsed.path or "")
    return int(match.group("client_id")) if match else None


def _client_phone_from_scan(raw_value: str) -> str | None:
    raw = str(raw_value or "").strip()
    if not raw or "/" in raw or "?" in raw:
        return None
    digits = re.sub(r"\D+", "", raw)
    if len(digits) == 11 and digits[:1] in {"7", "8"}:
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def _card_code_from_scan(raw_value: str) -> str | None:
    raw = str(raw_value or "").strip()
    if not raw:
        return None

    if "/" in raw or "?" in raw:
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return None
        if (parsed.path or "").rstrip("/") != "/admin/vault":
            return None
        values = parse_qs(parsed.query, keep_blank_values=False).get("code", [])
        candidate = str(values[0] if values else "").strip()
    else:
        candidate = raw

    if _client_phone_from_scan(candidate):
        return None
    return candidate.upper() if _CARD_VALUE_RE.fullmatch(candidate) else None


def _client_payload(row) -> dict[str, object]:
    client_id = int(row["id"])
    return {
        "kind": "client",
        "client_id": client_id,
        "name": _client_name(row),
        "url": f"/clients/{client_id}",
    }


def install_admin_vault_scanner(app: FastAPI) -> FastAPI:
    if getattr(app.state, "admin_vault_scanner_installed", False):
        return app
    app.state.admin_vault_scanner_installed = True
    settings = app.state.settings

    @app.middleware("http")
    async def admin_vault_camera_policy(request: Request, call_next):
        response = await call_next(request)
        if request.url.path in _CAMERA_PATHS:
            response.headers["Permissions-Policy"] = _VAULT_CAMERA_POLICY
        return response

    @app.post("/api/master/qr/resolve", response_class=JSONResponse)
    async def resolve_admin_qr(
        request: Request,
        raw_value: str = Form(...),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        raw = str(raw_value or "").strip()
        if not raw or len(raw) > 2_048:
            return JSONResponse({"error": "qr_not_recognized"}, status_code=404)

        scanned_client_id = _client_id_from_scan(raw)
        card_code = _card_code_from_scan(raw)
        phone_local = _client_phone_from_scan(raw)

        with connect(settings.db_path) as conn:
            if scanned_client_id is not None:
                client = conn.execute(
                    """
                    SELECT id,first_name,nickname,username
                    FROM clients
                    WHERE id=? AND IFNULL(client_status,'')<>'deleted'
                    """,
                    (scanned_client_id,),
                ).fetchone()
                if client:
                    return JSONResponse(_client_payload(client))

            if card_code:
                reward = conn.execute(
                    """
                    SELECT vmr.id,vmr.client_id,vmr.code,vmr.activation_code,
                           vmr.status,vmr.activated_at,vmr.activation_expires_at,
                           vcr.title,c.first_name,c.nickname,c.username
                    FROM vault_member_rewards vmr
                    JOIN vault_catalog_rewards vcr ON vcr.id=vmr.catalog_reward_id
                    LEFT JOIN clients c ON c.id=vmr.client_id
                    WHERE UPPER(vmr.code)=? OR UPPER(IFNULL(vmr.activation_code,''))=?
                    ORDER BY CASE WHEN vmr.status='active' THEN 0 ELSE 1 END,
                             vmr.id DESC
                    LIMIT 1
                    """,
                    (card_code, card_code),
                ).fetchone()
                if reward:
                    client_label = (
                        str(reward["first_name"] or "").strip()
                        or str(reward["nickname"] or "").strip()
                        or (
                            f"@{str(reward['username']).lstrip('@')}"
                            if reward["username"]
                            else f"Клиент #{int(reward['client_id'])}"
                        )
                    )
                    redeemable = bool(
                        reward["status"] == "active"
                        and reward["activated_at"]
                        and reward["activation_code"]
                    )
                    return JSONResponse(
                        {
                            "kind": "card",
                            "member_reward_id": int(reward["id"]),
                            "client_id": int(reward["client_id"]),
                            "client_name": client_label,
                            "title": str(reward["title"] or "JACK CARD"),
                            "status": str(reward["status"] or ""),
                            "redeem_code": card_code,
                            "redeemable": redeemable,
                            "activation_expires_at": reward["activation_expires_at"],
                        }
                    )

            if phone_local:
                clients = conn.execute(
                    """
                    SELECT id,first_name,nickname,username
                    FROM clients
                    WHERE phone_local=? AND IFNULL(client_status,'')<>'deleted'
                    ORDER BY id DESC
                    LIMIT 3
                    """,
                    (phone_local,),
                ).fetchall()
                if len(clients) == 1:
                    return JSONResponse(_client_payload(clients[0]))
                if len(clients) > 1:
                    return JSONResponse(
                        {
                            "kind": "client_search",
                            "phone_local": phone_local,
                            "url": f"/master/clients?q={phone_local}",
                            "matches": len(clients),
                        }
                    )

        return JSONResponse({"error": "qr_not_recognized"}, status_code=404)

    return app


__all__ = [
    "_card_code_from_scan",
    "_client_id_from_scan",
    "_client_phone_from_scan",
    "install_admin_vault_scanner",
]
