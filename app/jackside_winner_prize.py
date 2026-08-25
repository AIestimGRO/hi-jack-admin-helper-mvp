from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse

from app.db import connect, transaction
from app.product_shell import _check_csrf, _require_master
from app.services import jackside_issues as issue_service


_EDITABLE_STATUSES = frozenset({"draft", "scheduled", "lobby"})


def _prize_editable(
    issue: sqlite3.Row | dict[str, Any], *, now: datetime | None = None
) -> bool:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    start = issue_service._parse_dt(
        str(issue["starts_at"]) if issue["starts_at"] else None
    )
    return bool(
        start
        and start > current
        and str(issue["status"] or "") in _EDITABLE_STATUSES
    )


def winner_prize_payload(
    conn: sqlite3.Connection,
    *,
    issue_id: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    issue = issue_service.get_issue(conn, int(issue_id))
    if not issue:
        raise ValueError("issue_not_found")
    selected_id = issue["final_prize_catalog_reward_id"]
    selected = None
    if selected_id:
        selected = conn.execute(
            "SELECT id,title,is_active FROM vault_catalog_rewards WHERE id=?",
            (int(selected_id),),
        ).fetchone()
    rewards = conn.execute(
        """
        SELECT id,title,validity_days,is_active
        FROM vault_catalog_rewards
        WHERE is_active=1 OR id=?
        ORDER BY position,id
        """,
        (int(selected_id or 0),),
    ).fetchall()
    return {
        "id": int(issue["id"]),
        "title": str(issue["title"] or "JACKSIDE"),
        "status": str(issue["status"] or ""),
        "editable": _prize_editable(issue, now=now),
        "final_prize_type": str(issue["final_prize_type"] or "none"),
        "final_prize_catalog_reward_id": int(selected_id) if selected_id else None,
        "final_prize_jackcoin_amount": int(issue["final_prize_jackcoin_amount"] or 0),
        "final_prize_title": str(selected["title"]) if selected else None,
        "rewards": [
            {
                "id": int(row["id"]),
                "title": str(row["title"]),
                "validity_days": int(row["validity_days"] or 0),
                "is_active": bool(row["is_active"]),
            }
            for row in rewards
        ],
    }


def set_future_winner_card_prize(
    conn: sqlite3.Connection,
    *,
    issue_id: int,
    catalog_reward_id: int,
    now: datetime | None = None,
) -> sqlite3.Row:
    issue = issue_service.get_issue(conn, int(issue_id))
    if not issue:
        raise ValueError("issue_not_found")
    if not _prize_editable(issue, now=now):
        raise ValueError("issue_prize_locked")

    selected_id = int(catalog_reward_id)
    if selected_id == -1:
        return issue
    if selected_id < -1:
        raise ValueError("invalid_card_prize")

    prize_type = "none"
    catalog_id: int | None = None
    if selected_id > 0:
        reward = conn.execute(
            """
            SELECT id FROM vault_catalog_rewards
            WHERE id=? AND is_active=1
            """,
            (selected_id,),
        ).fetchone()
        if not reward:
            raise ValueError("invalid_card_prize")
        prize_type = "reward_card"
        catalog_id = selected_id

    conn.execute(
        """
        UPDATE jackside_issues
        SET final_prize_type=?, final_prize_catalog_reward_id=?,
            final_prize_jackcoin_amount=0, updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (prize_type, catalog_id, int(issue_id)),
    )

    campaign_code = str(issue["campaign_code"] or "")
    if campaign_code:
        conn.execute(
            """
            UPDATE quiz_campaigns
            SET final_prize_type=?, final_prize_catalog_reward_id=?,
                final_prize_jackcoin_amount=0, updated_at=CURRENT_TIMESTAMP
            WHERE code=?
            """,
            (prize_type, catalog_id, campaign_code),
        )
        conn.execute(
            """
            UPDATE daily_414_final_tables
            SET prize_type=?, prize_catalog_reward_id=?, prize_jackcoin_amount=0,
                updated_at=CURRENT_TIMESTAMP
            WHERE campaign_code=? AND status IN ('waiting','unavailable')
            """,
            (prize_type, catalog_id, campaign_code),
        )

    return issue_service.get_issue(conn, int(issue_id))


def _error_message(code: str) -> str:
    return {
        "issue_not_found": "Выпуск JACKSIDE не найден",
        "issue_prize_locked": "Выпуск уже стартовал — дополнительный приз зафиксирован",
        "invalid_card_prize": "Выбранная JACK CARD недоступна. Проверьте THE VAULT",
    }.get(code, code)


def install_jackside_winner_prize(app: FastAPI) -> FastAPI:
    if getattr(app.state, "jackside_winner_prize_installed", False):
        return app
    app.state.jackside_winner_prize_installed = True
    settings = app.state.settings

    @app.get("/api/master/jackside/issues/{issue_id}/winner-prize")
    async def get_winner_prize(request: Request, issue_id: int) -> JSONResponse:
        _require_master(request)
        try:
            with connect(settings.db_path) as conn:
                payload = winner_prize_payload(conn, issue_id=int(issue_id))
        except ValueError as exc:
            code = str(exc)
            return JSONResponse(
                {"ok": False, "error": code, "message": _error_message(code)},
                status_code=404 if code == "issue_not_found" else 422,
            )
        return JSONResponse(
            {
                "ok": True,
                "issue": payload,
                "csrf_token": str(request.session.get("csrf") or ""),
            },
            headers={"Cache-Control": "private, no-store"},
        )

    @app.post("/api/master/jackside/issues/{issue_id}/winner-prize")
    async def save_winner_prize(
        request: Request,
        issue_id: int,
        catalog_reward_id: int = Form(0),
        csrf_token: str = Form(...),
    ) -> JSONResponse:
        _require_master(request)
        _check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                updated = set_future_winner_card_prize(
                    conn,
                    issue_id=int(issue_id),
                    catalog_reward_id=int(catalog_reward_id),
                )
                selected = None
                if updated["final_prize_catalog_reward_id"]:
                    selected = conn.execute(
                        "SELECT title FROM vault_catalog_rewards WHERE id=?",
                        (int(updated["final_prize_catalog_reward_id"]),),
                    ).fetchone()
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_audit_log'"
                ).fetchone():
                    conn.execute(
                        """
                        INSERT INTO admin_audit_log(
                            admin_id,admin_name,action,entity_type,entity_id,details
                        ) VALUES (?,?,?,?,?,?)
                        """,
                        (
                            request.session.get("admin_id"),
                            str(request.session.get("admin_name") or "Master"),
                            "set_jackside_winner_prize",
                            "jackside_issue",
                            int(issue_id),
                            json.dumps(
                                {
                                    "final_prize_type": str(
                                        updated["final_prize_type"] or "none"
                                    ),
                                    "catalog_reward_id": updated[
                                        "final_prize_catalog_reward_id"
                                    ],
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        ),
                    )
        except ValueError as exc:
            code = str(exc)
            return JSONResponse(
                {"ok": False, "error": code, "message": _error_message(code)},
                status_code=404 if code == "issue_not_found" else 422,
            )

        return JSONResponse(
            {
                "ok": True,
                "issue_id": int(issue_id),
                "final_prize_type": str(updated["final_prize_type"] or "none"),
                "catalog_reward_id": (
                    int(updated["final_prize_catalog_reward_id"])
                    if updated["final_prize_catalog_reward_id"]
                    else None
                ),
                "reward_title": str(selected["title"]) if selected else None,
            },
            headers={"Cache-Control": "private, no-store"},
        )

    return app


__all__ = [
    "install_jackside_winner_prize",
    "set_future_winner_card_prize",
    "winner_prize_payload",
]
