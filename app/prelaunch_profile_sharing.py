from __future__ import annotations

import sqlite3

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import prelaunch_experience as experience
from app.config import BASE_DIR
from app.db import connect, transaction
from app.product_shell import _check_csrf, _current_member


PROFILE_SHARING_CATEGORY = "participation_stats"
_ORIGINAL_RATING_CATEGORIES = experience._rating_categories


def ensure_profile_sharing_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS member_profile_sharing (
            account_id INTEGER PRIMARY KEY REFERENCES member_accounts(id) ON DELETE CASCADE,
            share_game_profile INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS member_profile_sharing_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER REFERENCES member_accounts(id) ON DELETE SET NULL,
            granted INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_member_profile_sharing_events_account
            ON member_profile_sharing_events(account_id, created_at DESC);
        """
    )


def _sharing_enabled(conn: sqlite3.Connection, account_id: int) -> bool:
    row = conn.execute(
        "SELECT share_game_profile FROM member_profile_sharing WHERE account_id=?",
        (account_id,),
    ).fetchone()
    return bool(row and int(row["share_game_profile"] or 0))


def _rating_categories_with_registered_profile(
    conn: sqlite3.Connection, account_id: int
) -> dict[str, bool]:
    categories = dict(_ORIGINAL_RATING_CATEGORIES(conn, account_id))
    ensure_profile_sharing_schema(conn)
    if _sharing_enabled(conn, account_id):
        categories[PROFILE_SHARING_CATEGORY] = True
    return categories


def install_prelaunch_profile_sharing(app: FastAPI) -> FastAPI:
    if getattr(app.state, "prelaunch_profile_sharing_installed", False):
        return app
    app.state.prelaunch_profile_sharing_installed = True
    settings = app.state.settings
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

    with transaction(settings.db_path) as conn:
        ensure_profile_sharing_schema(conn)

    experience._rating_categories = _rating_categories_with_registered_profile

    @app.get("/account/profile-sharing", response_class=HTMLResponse)
    async def member_profile_sharing_page(
        request: Request, ok: str = "", error: str = ""
    ):
        member = _current_member(request, required=True)
        with connect(settings.db_path) as conn:
            ensure_profile_sharing_schema(conn)
            granted = _sharing_enabled(conn, int(member["id"]))
        return templates.TemplateResponse(
            request,
            "member_profile_sharing.html",
            {
                "request": request,
                "member": member,
                "current_tab": "profile",
                "csrf_token": request.session.get("csrf", ""),
                "granted": granted,
                "ok": ok,
                "error": error,
                "asset_version": "prelaunch-v1",
            },
        )

    @app.post("/account/profile-sharing")
    async def member_profile_sharing_update(
        request: Request,
        share_game_profile: bool = Form(False),
        csrf_token: str = Form(...),
    ):
        member = _current_member(request, required=True)
        _check_csrf(request, csrf_token)
        granted = bool(share_game_profile)
        with transaction(settings.db_path) as conn:
            ensure_profile_sharing_schema(conn)
            conn.execute(
                """
                INSERT INTO member_profile_sharing(account_id, share_game_profile)
                VALUES (?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    share_game_profile=excluded.share_game_profile,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (int(member["id"]), int(granted)),
            )
            conn.execute(
                """
                INSERT INTO member_profile_sharing_events(account_id, granted)
                VALUES (?, ?)
                """,
                (int(member["id"]), int(granted)),
            )
        message = (
            "Игровой профиль открыт для зарегистрированных участников"
            if granted
            else "Расширенная игровая статистика скрыта от других участников"
        )
        return RedirectResponse(
            f"/account/profile-sharing?ok={message.replace(' ', '+')}", status_code=303
        )

    return app


__all__ = [
    "PROFILE_SHARING_CATEGORY",
    "ensure_profile_sharing_schema",
    "install_prelaunch_profile_sharing",
]
