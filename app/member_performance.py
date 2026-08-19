from __future__ import annotations

import inspect
import re
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.db import connect
from app.product_shell import _current_member
from app.profile_experience import title_collection_payload
from app.services.member_accounts import jackcoin_balance
from app.services.reward_animations import animation_url as reward_animation_url
from app.services.vault import purchase_token


VAULT_PAGE_SIZE = 6
MEMBER_PERFORMANCE_ASSET = "member-performance-v1"


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = getattr(row, key, default)
    return default if value is None else value


def _display_name(member: Any) -> str:
    nickname = str(_row_value(member, "nickname", "") or "").strip()
    first_name = str(_row_value(member, "first_name", "") or "").strip()
    username = str(_row_value(member, "username", "") or "").strip()
    return nickname or first_name or (f"@{username}" if username else "Игрок")


def _safe_version(value: Any) -> str:
    clean = re.sub(r"[^0-9A-Za-z_-]+", "", str(value or ""))
    return clean[:64] or "1"


def _profile_media(conn: sqlite3.Connection, account_id: int) -> dict[str, str]:
    try:
        row = conn.execute(
            """
            SELECT avatar_path,avatar_kind,updated_at
            FROM member_profile_media
            WHERE account_id=?
            """,
            (int(account_id),),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if not row or not str(row["avatar_path"] or "").strip():
        return {"url": "", "kind": "", "version": ""}
    return {
        "url": "/account/avatar",
        "kind": str(row["avatar_kind"] or "upload"),
        "version": _safe_version(row["updated_at"]),
    }


def _profile_social_links(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT code,title,description,link_type,url,position
            FROM club_social_links
            WHERE is_active=1 AND show_profile=1
            ORDER BY position,id
            LIMIT 6
            """,
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in rows]


def _collection_subtitle(item: dict[str, Any]) -> str:
    if item.get("state") == "locked":
        return "Не открыто · достижение" if item.get("kind") == "achievement" else "Не открыто · звание"
    if item.get("kind") == "achievement":
        return "Достижение получено"
    if item.get("temporary"):
        return "Активное звание"
    if item.get("selected"):
        return "Основное звание"
    return "Звание получено"


def _collection_meta(item: dict[str, Any]) -> str:
    if item.get("state") == "locked":
        return "Ещё не открыто"
    if item.get("kind") == "achievement":
        return "Открыто"
    if item.get("temporary") and item.get("expires_at"):
        return "Активно до " + str(item["expires_at"])[:16].replace("T", " ")
    if item.get("selected"):
        return "Сейчас отображается как основное звание"
    return "Звание получено"


def _decorate_collection(payload: dict[str, Any]) -> dict[str, Any]:
    raw_items = list(payload.get("items") or [])
    indexed = list(enumerate(raw_items))
    indexed.sort(
        key=lambda pair: (
            1 if pair[1].get("state") == "locked" else 0,
            0 if pair[1].get("state") != "locked" and pair[1].get("selected") else 1,
            pair[0],
        )
    )
    items: list[dict[str, Any]] = []
    for _, source in indexed:
        item = dict(source)
        item["subtitle"] = _collection_subtitle(item)
        item["meta"] = _collection_meta(item)
        classes = ["profile-emblem-card"]
        classes.append("is-locked" if item.get("state") == "locked" else "is-active-title")
        if item.get("kind") == "achievement":
            classes.append("is-achievement")
        if item.get("temporary"):
            classes.append("is-temporary")
        if item.get("selected"):
            classes.append("is-selected")
        item["card_class"] = " ".join(classes)
        items.append(item)
    return {
        **payload,
        "items": items,
        "unlocked_count": sum(1 for item in items if item.get("state") != "locked"),
    }


def _render_template_from_response(
    original_response: Any,
    *,
    template_name: str,
    context: dict[str, Any],
    profile_avatar: dict[str, str] | None = None,
) -> HTMLResponse:
    environment = original_response.template.environment
    template = environment.get_template(template_name)
    html = template.render(context)
    if profile_avatar and profile_avatar.get("url"):
        member = context.get("member")
        header_name = str(
            _row_value(member, "nickname", "")
            or _row_value(member, "first_name", "")
            or _row_value(member, "username", "")
            or "HJ"
        )
        fallback = header_name[:1].upper()
        target = (
            '<a class="member-profile-button" href="/account?tab=profile" '
            f'aria-label="Открыть профиль">{fallback}</a>'
        )
        avatar_url = profile_avatar["url"]
        avatar_version = profile_avatar.get("version") or "1"
        sticker_class = " is-sticker" if profile_avatar.get("kind") == "sticker" else ""
        replacement = (
            '<a class="member-profile-button has-product-avatar" href="/account?tab=profile" '
            'aria-label="Открыть профиль">'
            f'<img data-product-avatar="1" class="{sticker_class.strip()}" '
            f'src="{avatar_url}?v={avatar_version}" alt="" loading="eager" decoding="async">'
            "</a>"
        )
        if target in html:
            html = html.replace(target, replacement, 1)
    headers = {
        key: value
        for key, value in original_response.headers.items()
        if key.lower() not in {"content-length", "content-type"}
    }
    return HTMLResponse(
        html,
        status_code=int(getattr(original_response, "status_code", 200) or 200),
        headers=headers,
    )


def _catalog_page(
    db_path: Path,
    *,
    offset: int,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    with connect(db_path) as conn:
        total = int(
            conn.execute(
                "SELECT COUNT(*) FROM vault_catalog_rewards WHERE is_active=1"
            ).fetchone()[0]
        )
        rows = conn.execute(
            """
            SELECT r.*,
                   (
                       SELECT COUNT(*)
                       FROM vault_member_rewards vmr
                       WHERE vmr.catalog_reward_id=r.id
                         AND vmr.status<>'cancelled'
                   ) AS allocated_count
            FROM vault_catalog_rewards r
            WHERE r.is_active=1
            ORDER BY r.position,r.id
            LIMIT ? OFFSET ?
            """,
            (int(limit), int(offset)),
        ).fetchall()
    return [dict(row) for row in rows], total


def _install_account_wrapper(app: FastAPI, templates: Jinja2Templates) -> None:
    settings = app.state.settings
    route = next(
        (
            candidate
            for candidate in app.routes
            if isinstance(candidate, APIRoute)
            and candidate.path == "/account"
            and "GET" in (candidate.methods or set())
        ),
        None,
    )
    if route is None or getattr(route, "_member_performance_wrapped", False):
        return

    original_endpoint = route.dependant.call

    async def account_performance_wrapper(**kwargs: Any):
        result = original_endpoint(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        request = kwargs.get("request")
        tab = str(kwargs.get("tab") or "home")
        if not isinstance(request, Request) or tab not in {"profile", "vault"}:
            return result
        if not hasattr(result, "context") or not hasattr(result, "template"):
            return result

        context = dict(result.context)
        context["performance_render"] = True
        context["member_performance_asset"] = MEMBER_PERFORMANCE_ASSET

        if tab == "profile":
            if str(request.query_params.get("view") or "").strip() == "settings":
                return result
            member = context.get("member")
            if member is None:
                return result
            account_id = int(_row_value(member, "id", 0) or 0)
            client_id = int(_row_value(member, "client_id", 0) or 0)
            with connect(settings.db_path) as conn:
                collection = _decorate_collection(
                    title_collection_payload(conn, client_id=client_id)
                )
                profile_avatar = _profile_media(conn, account_id)
                social_links = _profile_social_links(conn)
            context.update(
                {
                    "profile_display_name": _display_name(member),
                    "profile_collection": collection,
                    "profile_avatar": profile_avatar,
                    "profile_social_links": social_links,
                }
            )
            return _render_template_from_response(
                result,
                template_name="member_profile_fast.html",
                context=context,
                profile_avatar=profile_avatar,
            )

        catalog = list(context.get("vault_catalog") or [])
        context["vault_catalog_total"] = len(catalog)
        context["vault_catalog_initial"] = catalog[:VAULT_PAGE_SIZE]
        context["vault_page_size"] = VAULT_PAGE_SIZE
        return _render_template_from_response(
            result,
            template_name="member_vault_fast.html",
            context=context,
        )

    route.endpoint = account_performance_wrapper
    route.dependant.call = account_performance_wrapper
    setattr(route, "_member_performance_wrapped", True)


def install_member_performance(app: FastAPI) -> FastAPI:
    if getattr(app.state, "member_performance_installed", False):
        return app
    app.state.member_performance_installed = True
    settings = app.state.settings
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
    templates.env.globals["reward_animation_url"] = reward_animation_url

    @app.get("/api/account/vault-catalog-page", response_class=JSONResponse)
    async def account_vault_catalog_page(
        request: Request,
        offset: int = Query(0, ge=0),
        limit: int = Query(VAULT_PAGE_SIZE, ge=1, le=VAULT_PAGE_SIZE),
    ):
        member = _current_member(request, required=True)
        account_id = int(member["id"])
        client_id = int(member["client_id"])
        safe_limit = min(int(limit), VAULT_PAGE_SIZE)
        rows, total = _catalog_page(
            Path(settings.db_path),
            offset=int(offset),
            limit=safe_limit,
        )
        with connect(settings.db_path) as conn:
            balance = jackcoin_balance(conn, client_id)
        tokens = {
            int(row["id"]): purchase_token(
                settings.secret_key,
                account_id=account_id,
                catalog_reward_id=int(row["id"]),
                price_jc=int(row["price_jc"] or 0),
            )
            for row in rows
        }
        partial = templates.get_template("_vault_catalog_cards_fast.html")
        html = partial.render(
            {
                "request": request,
                "rewards": rows,
                "balance": balance,
                "csrf_token": str(request.session.get("csrf") or ""),
                "vault_purchase_tokens": tokens,
                "category_marks": {
                    "drink": "☕",
                    "entry": "♠",
                    "card": "♦",
                    "profile": "★",
                    "protection": "⌁",
                    "club": "HJ",
                },
                "category_labels": {
                    "drink": "Бар",
                    "entry": "Игра",
                    "card": "JACK CARD",
                    "profile": "Профиль",
                    "protection": "Защита серии",
                    "club": "Клуб",
                },
            }
        )
        next_offset = int(offset) + len(rows)
        return JSONResponse(
            {
                "html": html,
                "offset": int(offset),
                "next_offset": next_offset,
                "count": len(rows),
                "total": total,
                "has_more": next_offset < total,
            }
        )

    _install_account_wrapper(app, templates)
    return app


__all__ = ["VAULT_PAGE_SIZE", "install_member_performance"]
