from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.db import transaction
from app.services.daily_414_final import (
    final_cancelled_message,
    final_eliminated_message,
    final_table_needs_reconcile,
    final_winner_announcement,
    list_final_winners,
    reconcile_final_table,
)
from app.services.member_accounts import (
    MEMBER_COOKIE_NAME,
    authenticated_member,
)


ASSET_VERSION = "jackside-final-recovery-20260820-1"
_STYLE_TAG = (
    '<link rel="stylesheet" data-jackside-final-recovery '
    'href="/static/css/jackside-final-recovery.css?'
    f'v={ASSET_VERSION}">'
)


def _persisted_final_payload(
    settings: Any,
    request: Request,
    campaign: str,
) -> dict[str, Any] | None:
    code = str(campaign or "").strip()
    if not code.startswith("jackside_"):
        return None

    token = str(request.cookies.get(MEMBER_COOKIE_NAME) or "").strip()
    if not token:
        return None

    now = datetime.now(timezone.utc)
    with transaction(settings.db_path) as conn:
        member = authenticated_member(
            conn,
            secret_key=settings.secret_key,
            token=token,
            touch=False,
        )
        if not member:
            return None

        table = conn.execute(
            """
            SELECT * FROM daily_414_final_tables
            WHERE campaign_code=?
            ORDER BY campaign_version DESC,id DESC
            LIMIT 1
            """,
            (code,),
        ).fetchone()
        if not table:
            return None

        if table["status"] not in {"completed", "unavailable"} and final_table_needs_reconcile(
            table,
            now=now,
        ):
            table = reconcile_final_table(
                conn,
                final_table_id=int(table["id"]),
                now=now,
            )

        finalist = conn.execute(
            """
            SELECT * FROM daily_414_finalists
            WHERE final_table_id=? AND client_id=?
            """,
            (int(table["id"]), int(member["client_id"])),
        ).fetchone()
        submission = conn.execute(
            """
            SELECT * FROM quiz_submissions
            WHERE campaign_code=? AND client_id=?
            ORDER BY campaign_version DESC,id DESC
            LIMIT 1
            """,
            (code, int(member["client_id"])),
        ).fetchone()

        base: dict[str, Any] = {
            "ok": True,
            "campaign": code,
            "campaign_version": int(table["campaign_version"] or 1),
            "server_now": now.isoformat(timespec="milliseconds"),
            "status": str(table["status"] or ""),
            "outcome": str(table["outcome"] or "") or None,
        }

        if table["status"] not in {"completed", "unavailable"}:
            return {
                **base,
                "state": "pending",
                "message": "Подводим итог…",
            }

        if str(table["outcome"] or "") == "cancelled":
            return {
                **base,
                "state": "cancelled",
                "message": final_cancelled_message(
                    jackcoin_awarded=int(submission["jackcoin_awarded"] or 0)
                    if submission
                    else 0
                ),
            }

        if not finalist:
            return {
                **base,
                "state": "not_qualified",
                "message": "В этот раз результат не вошёл в финальный стол.",
            }

        if finalist["status"] == "winner":
            winners = list_final_winners(conn, final_table_id=int(table["id"]))
            winner_ids = [int(row["client_id"]) for row in winners]
            announcement = final_winner_announcement(len(winner_ids))
            total = int(table["winner_jackcoin_awarded"] or 0)
            my_share = 0
            if total:
                if len(winner_ids) > 1:
                    row = conn.execute(
                        """
                        SELECT amount FROM jackcoin_ledger
                        WHERE source_type='final_prize' AND source_id=? AND client_id=?
                        ORDER BY id DESC LIMIT 1
                        """,
                        (str(table["id"]), int(member["client_id"])),
                    ).fetchone()
                    my_share = int(row["amount"] or 0) if row else 0
                else:
                    my_share = total
            message = announcement
            if my_share:
                message += f" {my_share} JACKCOIN уже начислены на баланс."
            return {
                **base,
                "state": "winner",
                "winner_count": len(winner_ids),
                "winners": winner_ids,
                "reward_jackcoin": my_share or total,
                "reward_jackcoin_total": total,
                "message": message,
            }

        if finalist["status"] == "eliminated":
            return {
                **base,
                "state": "eliminated",
                "message": (
                    "Финальный стол завершён без победителя: никто не ответил верно."
                    if table["outcome"] == "no_winner"
                    else final_eliminated_message()
                ),
            }

        return {
            **base,
            "state": "completed",
            "message": (
                "Финальный стол завершён без победителя. Главный приз не выдаётся."
                if table["outcome"] == "no_winner"
                else "Финальный стол завершён. Результат сохранён."
            ),
        }


async def _single_chunk(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


def install_jackside_final_recovery(app: FastAPI) -> FastAPI:
    if getattr(app.state, "jackside_final_recovery_installed", False):
        return app
    app.state.jackside_final_recovery_installed = True
    settings: Any = app.state.settings

    @app.get("/api/jackside/final-result")
    async def jackside_persisted_final_result(
        request: Request,
        campaign: str = "",
    ) -> JSONResponse:
        payload = _persisted_final_payload(settings, request, campaign)
        if not payload:
            return JSONResponse(
                {"ok": False, "error": "final_result_not_found"},
                status_code=404,
            )
        return JSONResponse(payload)

    @app.middleware("http")
    async def jackside_final_recovery_middleware(request: Request, call_next):
        response = await call_next(request)
        if request.method != "GET" or request.url.path != "/quiz":
            return response
        content_type = str(response.headers.get("content-type") or "").lower()
        if "text/html" not in content_type:
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        try:
            html = body.decode("utf-8")
        except UnicodeDecodeError:
            response.body_iterator = _single_chunk(body)
            return response
        if 'data-campaign-type="daily_414"' not in html:
            response.body_iterator = _single_chunk(body)
            return response
        if "data-jackside-final-recovery" not in html:
            html = html.replace("</head>", f"{_STYLE_TAG}\n</head>", 1)
        rewritten = html.encode("utf-8")
        response.body_iterator = _single_chunk(rewritten)
        response.headers["content-length"] = str(len(rewritten))
        return response

    return app


__all__ = [
    "ASSET_VERSION",
    "install_jackside_final_recovery",
]
