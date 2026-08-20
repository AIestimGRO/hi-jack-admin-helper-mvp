from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.db import connect
from app.services.daily_414_final import final_winner_announcement
from app.services.member_accounts import MEMBER_COOKIE_NAME, authenticated_member


def _issue_jackcoin_breakdown(conn, *, table_id: int, submission, client_id: int) -> dict[str, int]:
    main = int(submission["jackcoin_awarded"] or 0) if submission else 0
    final_correct = int(
        conn.execute(
            """
            SELECT COALESCE(SUM(jl.amount), 0)
            FROM jackcoin_ledger jl
            JOIN daily_414_final_answers a ON CAST(a.id AS TEXT)=jl.source_id
            JOIN daily_414_finalists f ON f.id=a.finalist_id
            WHERE jl.client_id=?
              AND jl.source_type='jackside_final_correct'
              AND a.final_table_id=?
              AND f.client_id=?
            """,
            (client_id, table_id, client_id),
        ).fetchone()[0]
        or 0
    )
    final_win = int(
        conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM jackcoin_ledger
            WHERE client_id=?
              AND source_type='jackside_final_win'
              AND source_id=?
            """,
            (client_id, str(table_id)),
        ).fetchone()[0]
        or 0
    )
    final_prize = int(
        conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM jackcoin_ledger
            WHERE client_id=?
              AND source_type='final_prize'
              AND source_id=?
            """,
            (client_id, str(table_id)),
        ).fetchone()[0]
        or 0
    )
    return {
        "main": main,
        "final_correct": final_correct,
        "final_win": final_win,
        "final_prize": final_prize,
        "total": main + final_correct + final_win + final_prize,
    }


def _final_answer_result(conn, *, table_id: int, finalist) -> tuple[bool | None, int]:
    question_index = finalist["eliminated_question_index"]
    if question_index is None:
        return None, 0
    answer = conn.execute(
        """
        SELECT id, is_correct
        FROM daily_414_final_answers
        WHERE final_table_id=? AND finalist_id=? AND question_index=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (table_id, int(finalist["id"]), int(question_index)),
    ).fetchone()
    if not answer:
        return None, 0
    awarded = int(
        conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM jackcoin_ledger
            WHERE client_id=?
              AND source_type='jackside_final_correct'
              AND source_id=?
            """,
            (int(finalist["client_id"]), str(answer["id"])),
        ).fetchone()[0]
        or 0
    )
    return bool(answer["is_correct"]), awarded


def _payload(settings: Any, request: Request, campaign: str) -> dict[str, Any] | None:
    code = str(campaign or "").strip()
    if not code.startswith("jackside_"):
        return None

    token = str(request.cookies.get(MEMBER_COOKIE_NAME) or "").strip()
    if not token:
        return None

    now = datetime.now(timezone.utc)
    with connect(settings.db_path) as conn:
        member = authenticated_member(
            conn,
            secret_key=settings.secret_key,
            token=token,
            touch=False,
        )
        if not member:
            return None
        client_id = int(member["client_id"])

        table = conn.execute(
            """
            SELECT * FROM daily_414_final_tables
            WHERE campaign_code=?
            ORDER BY campaign_version DESC, id DESC
            LIMIT 1
            """,
            (code,),
        ).fetchone()
        if not table:
            return None

        table_id = int(table["id"])
        finalist = conn.execute(
            """
            SELECT * FROM daily_414_finalists
            WHERE final_table_id=? AND client_id=?
            """,
            (table_id, client_id),
        ).fetchone()
        submission = conn.execute(
            """
            SELECT * FROM quiz_submissions
            WHERE campaign_code=? AND client_id=?
            ORDER BY campaign_version DESC, id DESC
            LIMIT 1
            """,
            (code, client_id),
        ).fetchone()
        jc = _issue_jackcoin_breakdown(
            conn,
            table_id=table_id,
            submission=submission,
            client_id=client_id,
        )

        base = {
            "ok": True,
            "campaign": code,
            "server_now": now.isoformat(timespec="milliseconds"),
            "issue_jackcoin_total": jc["total"],
            "issue_jackcoin_breakdown": jc,
        }

        if table["status"] not in {"completed", "unavailable"}:
            return {**base, "state": "pending", "message": "Подводим итог…"}

        outcome = str(table["outcome"] or "")
        if outcome == "cancelled":
            return {
                **base,
                "state": "cancelled",
                "message": "Финальный стол не состоялся. Результат основной части сохранён.",
            }

        if not finalist:
            return {
                **base,
                "state": "not_qualified",
                "message": "В этот раз вы не вошли в финальный стол.",
            }

        if outcome == "no_winner":
            return {
                **base,
                "state": "no_winner",
                "message": "На последнем вопросе правильного ответа не было. Победителя нет.",
            }

        if str(finalist["status"] or "") == "winner":
            return {
                **base,
                "state": "winner",
                "message": final_winner_announcement(1),
            }

        answer_correct, correct_award = _final_answer_result(
            conn,
            table_id=table_id,
            finalist=finalist,
        )
        if answer_correct is True:
            return {
                **base,
                "state": "correct_not_first",
                "message": (
                    "Ответ верный! "
                    f"За правильный ответ на финальный вопрос вы получаете {correct_award} JC. "
                    "Но, к сожалению, другой участник ответил правильно раньше и стал "
                    "победителем финального стола."
                ),
            }
        if answer_correct is False:
            return {
                **base,
                "state": "eliminated",
                "message": (
                    "Ответ на финальный вопрос неверный. Финальный стол для вас завершён. "
                    "Ниже — итог: сколько JACKCOIN вы получили за выпуск и за что."
                ),
            }

        return {
            **base,
            "state": "eliminated",
            "message": (
                "Финальный стол для вас завершён. "
                "Ниже — итог: сколько JACKCOIN вы получили за выпуск и за что."
            ),
        }


def install_jackside_final_outcome_only(app: FastAPI) -> FastAPI:
    if getattr(app.state, "jackside_final_outcome_only_installed", False):
        return app
    app.state.jackside_final_outcome_only_installed = True
    settings = app.state.settings

    @app.get("/api/jackside/final-outcome")
    async def jackside_final_outcome(request: Request, campaign: str = "") -> JSONResponse:
        payload = _payload(settings, request, campaign)
        if not payload:
            return JSONResponse(
                {"ok": False, "error": "final_outcome_not_found"},
                status_code=404,
            )
        return JSONResponse(payload, headers={"Cache-Control": "private, no-store"})

    return app


__all__ = ["install_jackside_final_outcome_only"]