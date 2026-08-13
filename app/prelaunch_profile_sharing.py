from __future__ import annotations

import json
import sqlite3
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import prelaunch_experience as experience
from app.config import BASE_DIR
from app.db import connect, transaction
from app.product_shell import _check_csrf, _csrf_token, _current_member


PROFILE_SHARING_CATEGORY = "participation_stats"
PROFILE_HISTORY_CATEGORY = "game_history"
PROFILE_VISIBILITY_DEFAULTS: dict[str, bool] = {
    "nickname": True,
    "avatar": True,
    "result": True,
    "place": True,
    "titles": True,
    "achievements": True,
    "game_stats": True,
    "game_history": True,
}
_LEGACY_LEGAL_KEYS = (
    "nickname",
    "avatar",
    "result",
    "place",
    "titles",
    "achievements",
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


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

        CREATE TABLE IF NOT EXISTS member_profile_visibility (
            account_id INTEGER NOT NULL REFERENCES member_accounts(id) ON DELETE CASCADE,
            category TEXT NOT NULL,
            is_visible INTEGER NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(account_id, category)
        );
        CREATE INDEX IF NOT EXISTS ix_member_profile_visibility_account
            ON member_profile_visibility(account_id, category);

        CREATE TABLE IF NOT EXISTS member_profile_visibility_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER REFERENCES member_accounts(id) ON DELETE SET NULL,
            category TEXT NOT NULL,
            is_visible INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_member_profile_visibility_events_account
            ON member_profile_visibility_events(account_id, created_at DESC, id DESC);
        """
    )


def _json_object(value: object) -> dict[str, object]:
    try:
        payload = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _legacy_visibility(conn: sqlite3.Connection, account_id: int) -> dict[str, bool]:
    """Read old explicit choices without rewriting legal consent history."""
    visibility = dict(PROFILE_VISIBILITY_DEFAULTS)

    if _table_exists(conn, "member_public_rating_consent_state"):
        legal = conn.execute(
            """
            SELECT categories_json FROM member_public_rating_consent_state
            WHERE account_id=?
            """,
            (account_id,),
        ).fetchone()
        if legal:
            categories = _json_object(legal["categories_json"])
            for key in _LEGACY_LEGAL_KEYS:
                # A stored false for one of the categories that was actually
                # presented in the old public-rating UI is an explicit opt-out.
                if key in categories and categories[key] is False:
                    visibility[key] = False

    sharing = conn.execute(
        "SELECT share_game_profile FROM member_profile_sharing WHERE account_id=?",
        (account_id,),
    ).fetchone()
    if sharing is not None:
        shared = bool(int(sharing["share_game_profile"] or 0))
        visibility["game_stats"] = shared
        visibility["game_history"] = shared

    return visibility


def _profile_visibility(conn: sqlite3.Connection, account_id: int) -> dict[str, bool]:
    visibility = _legacy_visibility(conn, account_id)
    for row in conn.execute(
        """
        SELECT category,is_visible FROM member_profile_visibility
        WHERE account_id=?
        """,
        (account_id,),
    ).fetchall():
        category = str(row["category"] or "")
        if category in visibility:
            visibility[category] = bool(int(row["is_visible"] or 0))
    return visibility


def _rating_categories_with_registered_profile(
    conn: sqlite3.Connection, account_id: int
) -> dict[str, bool]:
    visibility = _profile_visibility(conn, account_id)
    # prelaunch_experience historically used participation_stats as the fetch
    # switch for both aggregate stats and history. Keep it enabled internally
    # when either public category is visible, then let templates use the two
    # explicit keys below to decide what is actually rendered.
    fetch_participation = bool(
        visibility["game_stats"] or visibility["game_history"]
    )
    return {
        "nickname": visibility["nickname"],
        "avatar": visibility["avatar"],
        "result": visibility["result"],
        "place": visibility["place"],
        "achievements": visibility["achievements"],
        "titles": visibility["titles"],
        PROFILE_SHARING_CATEGORY: fetch_participation,
        "game_stats": visibility["game_stats"],
        PROFILE_HISTORY_CATEGORY: visibility["game_history"],
    }


def _profile_visibility_entry_html() -> str:
    return """
<section class="member-card account-panel profile-visibility-entry" aria-labelledby="profile-visibility-heading">
  <div class="app-section-head">
    <div><p class="member-eyebrow">Приватность</p><h2 id="profile-visibility-heading">Видимость профиля</h2></div>
    <a class="profile-action" href="/account/profile-sharing">Настроить</a>
  </div>
  <p class="member-muted">Игровой профиль открыт участникам клуба по умолчанию. Можно скрыть отдельные категории в один шаг.</p>
</section>
""".strip()


def _inject_profile_visibility_entry(html: str) -> str:
    if "profile-visibility-entry" in html:
        return html
    marker = '<section class="member-card account-panel profile-panel">'
    if marker not in html:
        return html
    return html.replace(marker, f"{_profile_visibility_entry_html()}\n\n{marker}", 1)


def install_prelaunch_profile_sharing(app: FastAPI) -> FastAPI:
    if getattr(app.state, "prelaunch_profile_sharing_installed", False):
        return app
    app.state.prelaunch_profile_sharing_installed = True
    settings = app.state.settings
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

    with transaction(settings.db_path) as conn:
        ensure_profile_sharing_schema(conn)

    experience._rating_categories = _rating_categories_with_registered_profile

    @app.middleware("http")
    async def profile_visibility_entrypoint(request: Request, call_next):
        response = await call_next(request)
        if not (
            request.method.upper() == "GET"
            and request.url.path == "/account"
            and request.query_params.get("tab") == "profile"
            and response.status_code == 200
            and "text/html" in str(response.headers.get("content-type") or "")
        ):
            return response

        chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, bytes) else bytes(chunk))
        body = b"".join(chunks).decode("utf-8")
        rendered = _inject_profile_visibility_entry(body)
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return HTMLResponse(
            rendered,
            status_code=response.status_code,
            headers=headers,
            background=response.background,
        )

    @app.get("/account/profile-sharing", response_class=HTMLResponse)
    async def member_profile_sharing_page(
        request: Request, ok: str = "", error: str = ""
    ):
        member = _current_member(request, required=True)
        with connect(settings.db_path) as conn:
            visibility = _profile_visibility(conn, int(member["id"]))
        return templates.TemplateResponse(
            request,
            "member_profile_sharing.html",
            {
                "request": request,
                "member": member,
                "current_tab": "profile",
                "csrf_token": _csrf_token(request),
                "visibility": visibility,
                "ok": ok,
                "error": error,
                "asset_version": "prelaunch-v2",
            },
        )

    @app.post("/account/profile-sharing")
    async def member_profile_sharing_update(
        request: Request,
        nickname: bool = Form(False),
        avatar: bool = Form(False),
        result: bool = Form(False),
        place: bool = Form(False),
        titles: bool = Form(False),
        achievements: bool = Form(False),
        game_stats: bool = Form(False),
        game_history: bool = Form(False),
        csrf_token: str = Form(...),
    ):
        member = _current_member(request, required=True)
        _check_csrf(request, csrf_token)
        account_id = int(member["id"])
        requested = {
            "nickname": bool(nickname),
            "avatar": bool(avatar),
            "result": bool(result),
            "place": bool(place),
            "titles": bool(titles),
            "achievements": bool(achievements),
            "game_stats": bool(game_stats),
            "game_history": bool(game_history),
        }
        with transaction(settings.db_path) as conn:
            before = _profile_visibility(conn, account_id)
            for category, visible in requested.items():
                conn.execute(
                    """
                    INSERT INTO member_profile_visibility(account_id,category,is_visible)
                    VALUES (?,?,?)
                    ON CONFLICT(account_id,category) DO UPDATE SET
                        is_visible=excluded.is_visible,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (account_id, category, int(visible)),
                )
                if before.get(category) != visible:
                    conn.execute(
                        """
                        INSERT INTO member_profile_visibility_events(
                            account_id,category,is_visible
                        ) VALUES (?,?,?)
                        """,
                        (account_id, category, int(visible)),
                    )

            # Keep the legacy coarse preference synchronized for compatibility.
            # The new per-category table remains authoritative on subsequent reads.
            legacy_granted = bool(game_stats and game_history)
            legacy = conn.execute(
                "SELECT share_game_profile FROM member_profile_sharing WHERE account_id=?",
                (account_id,),
            ).fetchone()
            legacy_before = (
                bool(int(legacy["share_game_profile"] or 0)) if legacy else None
            )
            conn.execute(
                """
                INSERT INTO member_profile_sharing(account_id,share_game_profile)
                VALUES (?,?)
                ON CONFLICT(account_id) DO UPDATE SET
                    share_game_profile=excluded.share_game_profile,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (account_id, int(legacy_granted)),
            )
            if legacy_before is None or legacy_before != legacy_granted:
                conn.execute(
                    """
                    INSERT INTO member_profile_sharing_events(account_id,granted)
                    VALUES (?,?)
                    """,
                    (account_id, int(legacy_granted)),
                )

        query = urlencode({"ok": "Видимость профиля сохранена"})
        return RedirectResponse(f"/account/profile-sharing?{query}", status_code=303)

    return app


__all__ = [
    "PROFILE_HISTORY_CATEGORY",
    "PROFILE_SHARING_CATEGORY",
    "PROFILE_VISIBILITY_DEFAULTS",
    "_inject_profile_visibility_entry",
    "_profile_visibility",
    "_rating_categories_with_registered_profile",
    "ensure_profile_sharing_schema",
    "install_prelaunch_profile_sharing",
]
