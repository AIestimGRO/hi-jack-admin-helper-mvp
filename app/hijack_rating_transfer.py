from __future__ import annotations

from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse

from app.db import transaction
from app.product_shell import _check_csrf, _require_master
from app.services.auth import audit
from app.services.hijack_rating import ensure_hijack_rating_schema


def _redirect(message: str, *, error: bool = False) -> RedirectResponse:
    key = "error" if error else "ok"
    return RedirectResponse(
        f"/master/hijack-rating?{urlencode({key: message})}", status_code=303
    )


def transfer_hijack_rating_owner(
    conn,
    *,
    source_client_id: int,
    target_client_id: int,
) -> dict[str, int]:
    ensure_hijack_rating_schema(conn)
    if int(source_client_id) == int(target_client_id):
        raise ValueError("Старый и новый Client ID должны отличаться")

    source = conn.execute(
        "SELECT id,client_status FROM clients WHERE id=?",
        (int(source_client_id),),
    ).fetchone()
    if not source:
        raise ValueError("Старый профиль не найден")

    target = conn.execute(
        """
        SELECT c.id,c.client_status,ma.id AS account_id
        FROM clients c
        LEFT JOIN member_accounts ma ON ma.client_id=c.id
        WHERE c.id=?
        """,
        (int(target_client_id),),
    ).fetchone()
    if not target:
        raise ValueError("Новый профиль не найден")
    if str(target["client_status"] or "existing") == "deleted":
        raise ValueError("Нельзя перенести рейтинг в удалённый профиль")
    if not target["account_id"]:
        raise ValueError("У нового профиля должен быть зарегистрированный личный кабинет")

    tournament_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM hi_jack_rating_entries WHERE client_id=?",
            (int(source_client_id),),
        ).fetchone()[0]
    )

    baseline_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hi_jack_rating_baseline_entries'"
    ).fetchone()
    baseline_count = 0
    if baseline_exists:
        baseline_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM hi_jack_rating_baseline_entries WHERE client_id=?",
                (int(source_client_id),),
            ).fetchone()[0]
        )

    if tournament_count == 0 and baseline_count == 0:
        raise ValueError("У старого профиля нет записей рейтинга HI JACK для переноса")

    overlap = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT src.import_id)
            FROM hi_jack_rating_entries src
            WHERE src.client_id=?
              AND EXISTS (
                  SELECT 1 FROM hi_jack_rating_entries dst
                  WHERE dst.client_id=? AND dst.import_id=src.import_id
              )
            """,
            (int(source_client_id), int(target_client_id)),
        ).fetchone()[0]
    )
    if overlap:
        raise ValueError(
            f"Перенос остановлен: у нового профиля уже есть данные в {overlap} тех же турнирах"
        )

    if baseline_exists and baseline_count:
        target_baseline = int(
            conn.execute(
                "SELECT COUNT(*) FROM hi_jack_rating_baseline_entries WHERE client_id=?",
                (int(target_client_id),),
            ).fetchone()[0]
        )
        if target_baseline:
            raise ValueError(
                "Перенос остановлен: у нового профиля уже есть строка исходного общего рейтинга"
            )

    conn.execute(
        "UPDATE hi_jack_rating_entries SET client_id=? WHERE client_id=?",
        (int(target_client_id), int(source_client_id)),
    )
    if baseline_exists:
        conn.execute(
            "UPDATE hi_jack_rating_baseline_entries SET client_id=? WHERE client_id=?",
            (int(target_client_id), int(source_client_id)),
        )

    return {
        "tournament_rows": tournament_count,
        "baseline_rows": baseline_count,
        "total_rows": tournament_count + baseline_count,
    }


def install_hijack_rating_transfer(app: FastAPI) -> FastAPI:
    if getattr(app.state, "hijack_rating_transfer_installed", False):
        return app
    app.state.hijack_rating_transfer_installed = True
    settings = app.state.settings

    @app.post("/api/master/hijack-rating/transfer-owner")
    async def master_transfer_hijack_rating_owner(
        request: Request,
        source_client_id: int = Form(...),
        target_client_id: int = Form(...),
        confirmation: str = Form(...),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        if " ".join(str(confirmation or "").split()).upper() != "ПЕРЕНЕСТИ РЕЙТИНГ":
            return _redirect(
                "Для переноса введите ПЕРЕНЕСТИ РЕЙТИНГ", error=True
            )
        try:
            with transaction(settings.db_path) as conn:
                result = transfer_hijack_rating_owner(
                    conn,
                    source_client_id=int(source_client_id),
                    target_client_id=int(target_client_id),
                )
                audit(
                    conn,
                    admin_id=int(request.session.get("admin_id")),
                    admin_name=str(request.session.get("admin_name") or "master"),
                    action="hijack_rating_owner_transferred",
                    entity_type="client",
                    entity_id=int(target_client_id),
                    details={
                        "source_client_id": int(source_client_id),
                        "target_client_id": int(target_client_id),
                        **result,
                    },
                )
        except ValueError as exc:
            return _redirect(str(exc), error=True)

        return _redirect(
            f"Рейтинг HI JACK перенесён: {result['total_rows']} строк, "
            f"Client ID {int(source_client_id)} → {int(target_client_id)}"
        )

    return app


__all__ = [
    "install_hijack_rating_transfer",
    "transfer_hijack_rating_owner",
]
