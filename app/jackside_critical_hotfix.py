from __future__ import annotations

import io
import re
import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from PIL import Image

from app.db import transaction
from app.jackside_brand_asset import JACKSIDE_LOGO_WEBP
from app.services.daily_414_final import final_table_needs_reconcile, reconcile_final_table


ASSET_VERSION = "jackside-critical-20260820-5"
_SCRIPT_TAG = (
    '<script src="/static/js/jackside-critical-hotfix.js?'
    f'v={ASSET_VERSION}"></script>'
)
_UI_STYLE_TAG = (
    '<link rel="stylesheet" data-jackside-ui-polish '
    'href="/static/css/jackside-ui-polish.css?v=jackside-ui-20260820-10">'
)
_UI_SCRIPT_TAG = (
    '<script data-jackside-ui-polish '
    'src="/static/js/jackside-ui-polish.js?v=jackside-ui-20260820-10"></script>'
)

_FINAL_OUTCOME_SCRIPT_TAG = (
    '<script data-jackside-final-outcome '
    'src="/static/js/jackside-final-outcome-only.js?v=jackside-final-ui-20260820-11"></script>'
)

_MEMBER_SCRIPT_TAG = (
    '<script src="/static/js/jackside-member-critical-hotfix.js?'
    f'v={ASSET_VERSION}"></script>'
)
_ADMIN_SCRIPT_TAG = (
    '<script src="/static/js/jackside-admin-critical-hotfix.js?'
    f'v={ASSET_VERSION}"></script>'
)
_CAMPAIGN_BACKGROUND_RE = re.compile(r'data-campaign-background="[^"]*"')
_OLD_MARK = "/static/img/brand/hi-jack-mark.webp"
_NEW_MARK = "/jackside-brand/logo.webp"
_BRAND_HEAD = (
    '<link rel="icon" type="image/png" href="/jackside-brand/favicon.png">\n'
    '<link rel="apple-touch-icon" href="/jackside-brand/apple-touch-icon.png">\n'
    '<link rel="manifest" href="/jackside.webmanifest">\n'
)


def _png_from_brand(size: int) -> bytes:
    image = Image.open(io.BytesIO(JACKSIDE_LOGO_WEBP)).convert("RGB")
    image = image.resize((size, size), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


JACKSIDE_ICON_512 = _png_from_brand(512)
JACKSIDE_ICON_192 = _png_from_brand(192)
JACKSIDE_APPLE_TOUCH = _png_from_brand(180)
JACKSIDE_FAVICON = _png_from_brand(64)


def refresh_jackside_issue_question_counts(conn: sqlite3.Connection) -> int:
    """Refresh cached JACKSIDE counters from the actual active questions."""
    changed = 0
    rows = conn.execute(
        """
        SELECT id, campaign_code, main_question_count, final_question_count
        FROM jackside_issues
        WHERE COALESCE(campaign_code,'')<>''
        """,
    ).fetchall()
    for row in rows:
        counts = conn.execute(
            """
            SELECT
              SUM(CASE
                    WHEN IFNULL(game_round,'main')='main' AND IFNULL(is_active,1)=1
                    THEN 1 ELSE 0
                  END) AS main_count,
              SUM(CASE
                    WHEN game_round='final' AND IFNULL(is_active,1)=1
                    THEN 1 ELSE 0
                  END) AS final_count
            FROM quiz_questions
            WHERE campaign_code=?
            """,
            (str(row["campaign_code"]),),
        ).fetchone()
        main_count = int(counts["main_count"] or 0)
        final_count = int(counts["final_count"] or 0)
        if (
            main_count == int(row["main_question_count"] or 0)
            and final_count == int(row["final_question_count"] or 0)
        ):
            continue
        conn.execute(
            """
            UPDATE jackside_issues
            SET main_question_count=?, final_question_count=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (main_count, final_count, int(row["id"])),
        )
        changed += 1
    return changed


def reconcile_expired_jackside_final(
    conn: sqlite3.Connection,
    *,
    campaign_code: str,
    now: datetime | None = None,
) -> bool:
    """Resolve a stale JACKSIDE final even if the normal status lookup later 404s."""
    code = str(campaign_code or "").strip()
    if not code.startswith("jackside_"):
        return False
    table = conn.execute(
        """
        SELECT * FROM daily_414_final_tables
        WHERE campaign_code=?
        ORDER BY campaign_version DESC,id DESC
        LIMIT 1
        """,
        (code,),
    ).fetchone()
    if not table or table["status"] in {"completed", "unavailable"}:
        return False
    current = now or datetime.now(timezone.utc)
    if not final_table_needs_reconcile(table, now=current):
        return False
    reconcile_final_table(
        conn,
        final_table_id=int(table["id"]),
        now=current,
    )
    return True


def _brand_html(html: str) -> str:
    branded = html.replace(_OLD_MARK, _NEW_MARK)
    branded = re.sub(
        r'<link rel="icon"[^>]*>\s*',
        "",
        branded,
        flags=re.IGNORECASE,
    )
    branded = re.sub(
        r'<link rel="apple-touch-icon"[^>]*>\s*',
        "",
        branded,
        flags=re.IGNORECASE,
    )
    branded = re.sub(
        r'<link rel="manifest"[^>]*>\s*',
        "",
        branded,
        flags=re.IGNORECASE,
    )
    if "/jackside-brand/favicon.png" not in branded:
        branded = branded.replace("</head>", f"{_BRAND_HEAD}</head>", 1)
    return branded


def rewrite_jackside_quiz_html(html: str) -> str:
    """Keep section artwork section-scoped and load JACKSIDE UX/branding fixes."""
    if 'id="quiz-app"' not in html or 'data-campaign-type="daily_414"' not in html:
        return html
    rewritten = _CAMPAIGN_BACKGROUND_RE.sub(
        'data-campaign-background=""', html, count=1
    )
    rewritten = _brand_html(rewritten)
    if _UI_STYLE_TAG not in rewritten:
        rewritten = rewritten.replace("</head>", f"{_UI_STYLE_TAG}\n</head>", 1)
    if _SCRIPT_TAG not in rewritten:
        rewritten = rewritten.replace("</body>", f"{_SCRIPT_TAG}\n</body>", 1)
    if _FINAL_OUTCOME_SCRIPT_TAG not in rewritten:
        rewritten = rewritten.replace("</body>", f"{_FINAL_OUTCOME_SCRIPT_TAG}\n</body>", 1)
    return rewritten


def rewrite_jackside_member_html(html: str) -> str:
    """Brand JACKSIDE member pages and load lobby/rules action labels."""
    if 'data-account-tab=' not in html or 'JACKSIDE' not in html:
        return html
    rewritten = _brand_html(html)
    if _MEMBER_SCRIPT_TAG not in rewritten:
        rewritten = rewritten.replace("</body>", f"{_MEMBER_SCRIPT_TAG}\n</body>", 1)
    return rewritten


def rewrite_jackside_builder_html(html: str) -> str:
    """Load JACKSIDE-only admin fixes in the quiz builder."""
    if 'data-quiz-builder' not in html or 'data-campaign-type="daily_414"' not in html:
        return html
    if _ADMIN_SCRIPT_TAG not in html:
        return html.replace("</body>", f"{_ADMIN_SCRIPT_TAG}\n</body>", 1)
    return html


async def _single_chunk(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


def install_jackside_critical_hotfix(app: FastAPI) -> FastAPI:
    if getattr(app.state, "jackside_critical_hotfix_installed", False):
        return app
    app.state.jackside_critical_hotfix_installed = True
    settings: Any = app.state.settings

    @app.get("/jackside-brand/logo.webp")
    async def jackside_brand_logo() -> Response:
        return Response(
            content=JACKSIDE_LOGO_WEBP,
            media_type="image/webp",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/jackside-brand/icon-512.png")
    async def jackside_brand_icon_512() -> Response:
        return Response(content=JACKSIDE_ICON_512, media_type="image/png")

    @app.get("/jackside-brand/icon-192.png")
    async def jackside_brand_icon_192() -> Response:
        return Response(content=JACKSIDE_ICON_192, media_type="image/png")

    @app.get("/jackside-brand/apple-touch-icon.png")
    async def jackside_brand_apple_touch() -> Response:
        return Response(content=JACKSIDE_APPLE_TOUCH, media_type="image/png")

    @app.get("/jackside-brand/favicon.png")
    async def jackside_brand_favicon() -> Response:
        return Response(content=JACKSIDE_FAVICON, media_type="image/png")

    @app.get("/jackside.webmanifest")
    async def jackside_manifest() -> JSONResponse:
        return JSONResponse(
            {
                "name": "JACKSIDE by Hi, Jack!",
                "short_name": "JACKSIDE",
                "start_url": "/account",
                "scope": "/",
                "display": "standalone",
                "background_color": "#020807",
                "theme_color": "#07110f",
                "icons": [
                    {
                        "src": "/jackside-brand/icon-192.png",
                        "sizes": "192x192",
                        "type": "image/png",
                    },
                    {
                        "src": "/jackside-brand/icon-512.png",
                        "sizes": "512x512",
                        "type": "image/png",
                    },
                ],
            },
            headers={"Cache-Control": "no-cache"},
        )

    @app.middleware("http")
    async def jackside_critical_hotfix_middleware(request: Request, call_next):
        if request.method == "GET" and request.url.path == "/master/jackside":
            with transaction(settings.db_path) as conn:
                refresh_jackside_issue_question_counts(conn)

        if (
            request.method == "GET"
            and request.url.path == "/api/quiz/final-table/status"
        ):
            campaign = str(request.query_params.get("campaign") or "").strip()
            if campaign.startswith("jackside_"):
                with transaction(settings.db_path) as conn:
                    reconcile_expired_jackside_final(
                        conn,
                        campaign_code=campaign,
                    )

        response = await call_next(request)
        if request.method != "GET":
            return response
        is_quiz = request.url.path == "/quiz"
        is_account = request.url.path == "/account"
        is_builder = request.url.path.startswith("/master/quiz-builder/")
        if not is_quiz and not is_account and not is_builder:
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

        if is_quiz:
            rewritten_html = rewrite_jackside_quiz_html(html)
        elif is_account:
            rewritten_html = rewrite_jackside_member_html(html)
        else:
            rewritten_html = rewrite_jackside_builder_html(html)
        rewritten = rewritten_html.encode("utf-8")
        response.body_iterator = _single_chunk(rewritten)
        response.headers["content-length"] = str(len(rewritten))
        return response

    return app


__all__ = [
    "ASSET_VERSION",
    "install_jackside_critical_hotfix",
    "reconcile_expired_jackside_final",
    "refresh_jackside_issue_question_counts",
    "rewrite_jackside_builder_html",
    "rewrite_jackside_member_html",
    "rewrite_jackside_quiz_html",
]
