from __future__ import annotations

import re
import sqlite3
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request

from app.db import transaction


ASSET_VERSION = "jackside-critical-20260819-2"
_SCRIPT_TAG = (
    '<script src="/static/js/jackside-critical-hotfix.js?'
    f'v={ASSET_VERSION}"></script>'
)
_ADMIN_SCRIPT_TAG = (
    '<script src="/static/js/jackside-admin-critical-hotfix.js?'
    f'v={ASSET_VERSION}"></script>'
)
_CAMPAIGN_BACKGROUND_RE = re.compile(r'data-campaign-background="[^"]*"')


def refresh_jackside_issue_question_counts(conn: sqlite3.Connection) -> int:
    """Refresh cached JACKSIDE counters from the actual active questions."""
    changed = 0
    rows = conn.execute(
        """
        SELECT id, campaign_code, main_question_count, final_question_count
        FROM jackside_issues
        WHERE COALESCE(campaign_code,'')<>''
        """
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


def rewrite_jackside_quiz_html(html: str) -> str:
    """Keep section artwork section-scoped and load the final-flow watchdog."""
    if 'id="quiz-app"' not in html or 'data-campaign-type="daily_414"' not in html:
        return html
    rewritten = _CAMPAIGN_BACKGROUND_RE.sub(
        'data-campaign-background=""', html, count=1
    )
    if _SCRIPT_TAG not in rewritten:
        rewritten = rewritten.replace("</body>", f"{_SCRIPT_TAG}\n</body>", 1)
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

    @app.middleware("http")
    async def jackside_critical_hotfix_middleware(request: Request, call_next):
        if request.method == "GET" and request.url.path == "/master/jackside":
            with transaction(settings.db_path) as conn:
                refresh_jackside_issue_question_counts(conn)

        response = await call_next(request)
        if request.method != "GET":
            return response
        is_quiz = request.url.path == "/quiz"
        is_builder = request.url.path.startswith("/master/quiz-builder/")
        if not is_quiz and not is_builder:
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

        rewritten_html = (
            rewrite_jackside_quiz_html(html)
            if is_quiz
            else rewrite_jackside_builder_html(html)
        )
        rewritten = rewritten_html.encode("utf-8")
        response.body_iterator = _single_chunk(rewritten)
        response.headers["content-length"] = str(len(rewritten))
        return response

    return app


__all__ = [
    "ASSET_VERSION",
    "install_jackside_critical_hotfix",
    "refresh_jackside_issue_question_counts",
    "rewrite_jackside_builder_html",
    "rewrite_jackside_quiz_html",
]
