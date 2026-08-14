from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

from app import profile_experience
from app.db import connect
from app.hijack_rating_paging import hijack_rating_page_payload
from app.prelaunch_data_integrity import calendar_jackside_rating_payload
from app.product_shell import _avatar_record, _public_tournaments
from app.public_profile_refs import public_profile_ref
from app.services.hijack_rating import referral_tree
from app.services.phone import display_phone


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def _format_tournament_datetime(value: Any, timezone_name: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        parsed = parsed.astimezone(ZoneInfo(timezone_name))
    except (KeyError, ValueError):
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%d.%m · %H:%M")


def _social_links(conn: sqlite3.Connection, tab: str) -> list[dict[str, Any]]:
    if not _table_exists(conn, "club_social_links"):
        return []
    rows = conn.execute(
        """
        SELECT code,title,description,link_type,url,show_home,show_profile,position
        FROM club_social_links
        WHERE is_active=1 AND TRIM(COALESCE(url,''))<>''
        ORDER BY position,id
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if tab == "home" and not bool(item.get("show_home")):
            continue
        if tab == "profile" and not bool(item.get("show_profile")):
            continue
        if tab not in {"home", "profile"}:
            continue
        link_type = str(item.get("link_type") or "")
        item["short_label"] = {
            "telegram": "Telegram",
            "miniapp": "Mini App",
            "maps": "Карты",
        }.get(link_type, str(item.get("title") or "Hi, Jack!"))
        result.append(item)
    return result


def _empty_collection() -> SimpleNamespace:
    return SimpleNamespace(items=[], active_count=0, total_count=0)


def _prioritize_collection(payload: dict[str, Any]) -> SimpleNamespace:
    indexed = list(enumerate(list(payload.get("items") or [])))
    indexed.sort(
        key=lambda pair: (
            1 if pair[1].get("state") == "locked" else 0,
            0
            if pair[1].get("state") != "locked" and pair[1].get("selected")
            else 1,
            pair[0],
        )
    )
    return SimpleNamespace(
        items=[item for _, item in indexed],
        active_count=int(payload.get("active_count") or 0),
        total_count=int(payload.get("total_count") or 0),
    )


def _attach_profile_refs(
    rows: list[dict[str, Any]], secret_key: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        client_id = row.get("client_id")
        row["profile_ref"] = (
            public_profile_ref(secret_key, int(client_id)) if client_id is not None else ""
        )
        result.append(row)
    return result


def _member_first_paint_state(
    request: Request,
    member: Any,
    current_tab: str = "home",
    current_rating_section: str = "month",
) -> dict[str, Any]:
    settings = request.app.state.settings
    tab = str(current_tab or "home")
    section = str(current_rating_section or "month")
    account_id = int(member["id"])
    client_id = int(member["client_id"])

    state: dict[str, Any] = {
        "profile": {
            "nickname": str(member["nickname"] or "").strip(),
            "avatar_url": None,
            "avatar_kind": None,
        },
        "tournaments": [],
        "nearest_tournament": None,
        "social_links": [],
        "title_collection": _empty_collection(),
        "referral_tree": None,
        "calendar_rating": None,
        "hijack_rating": None,
        "profile_view": "settings"
        if request.query_params.get("view") == "settings"
        else "main",
        "store_tab": "cards"
        if request.query_params.get("store") == "cards"
        else "market",
        "schedule_tab": "tournaments"
        if request.query_params.get("schedule") == "tournaments"
        else "quizzes",
        "chat_href": "/account/chats?back="
        + quote(
            request.url.path
            + (f"?{request.url.query}" if request.url.query else ""),
            safe="",
        ),
        "security": {
            "email": str(member["email"] or ""),
            "phone": display_phone(member["phone_local"])
            if member["phone_local"]
            else "—",
            "birth_date": "",
        },
    }

    with connect(settings.db_path) as conn:
        if _table_exists(conn, "member_profile_media"):
            avatar = _avatar_record(conn, account_id)
            if avatar and avatar["avatar_path"]:
                state["profile"]["avatar_url"] = "/account/avatar"
                state["profile"]["avatar_kind"] = str(avatar["avatar_kind"] or "photo")

        if _table_exists(conn, "club_tournaments"):
            tournaments = _public_tournaments(conn)
            for item in tournaments:
                item["starts_at_display"] = _format_tournament_datetime(
                    item.get("starts_at"), settings.timezone_name
                )
            state["tournaments"] = tournaments
            state["nearest_tournament"] = tournaments[0] if tournaments else None

        state["social_links"] = _social_links(conn, tab)

        if tab == "profile":
            state["title_collection"] = _prioritize_collection(
                profile_experience.title_collection_payload(conn, client_id=client_id)
            )
            state["referral_tree"] = referral_tree(conn, root_client_id=client_id)
            if _table_exists(conn, "clients"):
                identity = conn.execute(
                    "SELECT birth_date FROM clients WHERE id=?",
                    (client_id,),
                ).fetchone()
                if identity and identity["birth_date"]:
                    state["security"]["birth_date"] = str(identity["birth_date"])

        if tab == "rating" and section in {"month", "all"}:
            period = "year" if section == "all" else "month"
            payload = calendar_jackside_rating_payload(
                conn,
                client_id=client_id,
                period=period,
                timezone_name=settings.timezone_name,
            )
            payload = dict(payload)
            payload["rows"] = _attach_profile_refs(
                list(payload.get("rows") or []), settings.secret_key
            )
            state["calendar_rating"] = payload

        if tab == "rating" and section == "club":
            payload = hijack_rating_page_payload(
                conn,
                client_id=client_id,
                period="global",
                offset=0,
                limit=31,
            )
            payload = dict(payload)
            payload["rows"] = _attach_profile_refs(
                list(payload.get("rows") or []), settings.secret_key
            )
            state["hijack_rating"] = payload

    return state


def _member_profile_ref(request: Request, client_id: Any) -> str:
    if client_id is None:
        return ""
    return public_profile_ref(request.app.state.settings.secret_key, int(client_id))


def _template_environments(app: FastAPI):
    seen: set[int] = set()
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        closure = getattr(endpoint, "__closure__", None) or ()
        for cell in closure:
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if not isinstance(value, Jinja2Templates):
                continue
            env_id = id(value.env)
            if env_id in seen:
                continue
            seen.add(env_id)
            yield value.env


def install_member_first_paint(app: FastAPI) -> FastAPI:
    if getattr(app.state, "member_first_paint_installed", False):
        return app

    environments = list(_template_environments(app))
    if not environments:
        raise RuntimeError("member template environment not found")
    for env in environments:
        env.globals["member_first_paint_state"] = _member_first_paint_state
        env.globals["member_profile_ref"] = _member_profile_ref

    app.state.member_first_paint_installed = True
    return app


__all__ = ["install_member_first_paint"]
