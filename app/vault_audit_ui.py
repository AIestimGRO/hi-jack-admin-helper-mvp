from __future__ import annotations

import inspect
import json
from datetime import date, datetime, timedelta, timezone
from math import ceil
from typing import Any, Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates

from app.admin_access_control import ACCESS_MASTER, _role_from_request
from app.config import BASE_DIR
from app.db import connect


ACTIVATION_ASSET = "/static/js/vault-activation-policy.js"
HISTORY_ASSET_VERSION = "20261001-economy-history-v1"
HISTORY_PAGE_SIZE = 50

_CARD_ACTIONS = {
    "issued": "Карта выдана администратором",
    "purchase_issued": "Карта куплена за JACKCOIN",
    "final_prize_issued": "Карта выдана как главный приз",
    "activated": "Карта активирована",
    "redeemed": "Карта погашена",
    "activation_timeout_redeemed": "15 минут истекли — карта использована автоматически",
    "activation_expired": "Истёк старый код активации",
    "expired": "Истёк срок действия карты",
    "cancelled": "Карта отменена",
}

_OPERATION_FILTERS = (
    ("jc_credit", "Начисление JC"),
    ("jc_debit", "Списание JC"),
    ("card_issued", "Выдача / покупка карты"),
    ("card_activated", "Активация карты"),
    ("card_redeemed", "Погашение карты"),
    ("card_timeout", "Автосгорание через 15 минут"),
    ("card_cancelled", "Отмена карты"),
    ("card_expired", "Истечение карты / кода"),
)

_SOURCE_LABELS = {
    "admin": "Администратор",
    "purchase": "Hi, Store",
    "final_prize": "Главный приз JACKSIDE",
    "daily_414": "JACKSIDE",
    "vault_reward": "Hi, Store",
    "quiz_error_review": "Разбор ошибок",
    "referral": "Реферальная программа",
    "quiz": "Квиз",
}

_TEMPLATES = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


def _route(app: FastAPI, path: str, method: str = "GET") -> APIRoute | None:
    return next(
        (
            route
            for route in app.routes
            if isinstance(route, APIRoute)
            and route.path == path
            and method in (route.methods or set())
        ),
        None,
    )


def _replace_html_response(response: Any, body: str) -> Any:
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in {"content-length", "content-type"}
    }
    return HTMLResponse(
        body,
        status_code=int(getattr(response, "status_code", 200) or 200),
        headers=headers,
    )


def _wrap_html_route(
    app: FastAPI,
    *,
    path: str,
    marker: str,
    transform: Callable[[str, dict[str, Any]], str],
) -> None:
    route = _route(app, path)
    if route is None or getattr(route, marker, False):
        return
    original_endpoint = route.dependant.call

    async def wrapper(**kwargs: Any):
        result = original_endpoint(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        if not hasattr(result, "body"):
            return result
        content_type = str(result.headers.get("content-type") or "")
        if "text/html" not in content_type:
            return result
        source = bytes(result.body).decode("utf-8")
        updated = transform(source, kwargs)
        if updated == source:
            return result
        return _replace_html_response(result, updated)

    route.endpoint = wrapper
    route.dependant.call = wrapper
    setattr(route, marker, True)


def _format_time(value: Any, timezone_name: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "—"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.astimezone(ZoneInfo(timezone_name)).strftime("%d.%m.%Y %H:%M:%S")


def _date_boundary(value: str, timezone_name: str, *, next_day: bool = False) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed_date = date.fromisoformat(raw)
    except ValueError:
        return None
    local = datetime.combine(parsed_date, datetime.min.time(), tzinfo=ZoneInfo(timezone_name))
    if next_day:
        local += timedelta(days=1)
    return local.astimezone(timezone.utc).isoformat(timespec="seconds")


def _positive_int(value: Any) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = int(raw)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _card_detail(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(payload, dict):
        return raw
    for key in ("reason", "comment", "message"):
        text = str(payload.get(key) or "").strip()
        if text:
            return text
    return ""


def _source_label(value: Any) -> str:
    raw = str(value or "").strip()
    return _SOURCE_LABELS.get(raw, raw or "—")


def _operation_label(kind: str, code: str, amount: int) -> str:
    if kind == "jackcoin":
        return "Начисление JC" if amount > 0 else "Списание JC"
    return _CARD_ACTIONS.get(code, code or "Операция с картой")


def _entry_value(row: Any) -> str:
    if str(row["kind"]) == "jackcoin":
        amount = int(row["amount"] or 0)
        return f"{'+' if amount > 0 else ''}{amount} JC"
    title = str(row["item_title"] or "JACK CARD")
    code = str(row["card_code"] or "").strip()
    return f"{title} · код {code}" if code else title


def query_economy_history(
    db_path: str,
    *,
    timezone_name: str,
    filters: dict[str, str] | None = None,
    page: int = 1,
    page_size: int = HISTORY_PAGE_SIZE,
) -> dict[str, Any]:
    filters = {key: str(value or "").strip() for key, value in (filters or {}).items()}
    page = max(1, int(page or 1))
    page_size = min(200, max(10, int(page_size or HISTORY_PAGE_SIZE)))

    cte = """
    WITH entries AS (
        SELECT
            'card' AS kind,
            e.id AS source_row_id,
            e.created_at AS created_at,
            e.client_id AS client_id,
            COALESCE(
                NULLIF(c.first_name,''),
                NULLIF(c.nickname,''),
                CASE WHEN NULLIF(c.username,'') IS NOT NULL THEN '@' || LTRIM(c.username,'@') END,
                'Клиент #' || COALESCE(CAST(e.client_id AS TEXT),'?')
            ) AS client_name,
            COALESCE(c.username,'') AS username,
            COALESCE(c.phone_local,'') AS phone_local,
            COALESCE(c.phone_raw,'') AS phone_raw,
            e.action AS operation_code,
            CASE
                WHEN e.action IN ('issued','purchase_issued','final_prize_issued') THEN 'card_issued'
                WHEN e.action='activated' THEN 'card_activated'
                WHEN e.action='redeemed' THEN 'card_redeemed'
                WHEN e.action='activation_timeout_redeemed' THEN 'card_timeout'
                WHEN e.action='cancelled' THEN 'card_cancelled'
                WHEN e.action IN ('expired','activation_expired') THEN 'card_expired'
                ELSE 'card_other'
            END AS operation_group,
            0 AS amount,
            COALESCE(vcr.title,'JACK CARD') AS item_title,
            COALESCE(e.code,'') AS card_code,
            COALESCE(vmr.source_type,'') AS source_type,
            CASE
                WHEN e.admin_id IS NOT NULL
                  OR (TRIM(COALESCE(e.admin_name,''))<>'' AND LOWER(e.admin_name)<>'system')
                    THEN 'admin'
                WHEN e.action IN ('activated','purchase_issued') THEN 'user'
                ELSE 'system'
            END AS actor_type,
            e.admin_id AS actor_admin_id,
            CASE
                WHEN e.admin_id IS NOT NULL
                  OR (TRIM(COALESCE(e.admin_name,''))<>'' AND LOWER(e.admin_name)<>'system')
                    THEN COALESCE(NULLIF(e.admin_name,''),'Администратор')
                WHEN e.action IN ('activated','purchase_issued') THEN 'Пользователь'
                ELSE 'Система'
            END AS actor_name,
            COALESCE(e.details,'') AS detail_raw
        FROM vault_reward_events e
        LEFT JOIN vault_member_rewards vmr ON vmr.id=e.member_reward_id
        LEFT JOIN vault_catalog_rewards vcr ON vcr.id=e.catalog_reward_id
        LEFT JOIN clients c ON c.id=e.client_id

        UNION ALL

        SELECT
            'jackcoin' AS kind,
            jl.id AS source_row_id,
            jl.created_at AS created_at,
            jl.client_id AS client_id,
            COALESCE(
                NULLIF(c.first_name,''),
                NULLIF(c.nickname,''),
                CASE WHEN NULLIF(c.username,'') IS NOT NULL THEN '@' || LTRIM(c.username,'@') END,
                'Клиент #' || CAST(jl.client_id AS TEXT)
            ) AS client_name,
            COALESCE(c.username,'') AS username,
            COALESCE(c.phone_local,'') AS phone_local,
            COALESCE(c.phone_raw,'') AS phone_raw,
            jl.operation_type AS operation_code,
            CASE WHEN jl.amount>0 THEN 'jc_credit' ELSE 'jc_debit' END AS operation_group,
            jl.amount AS amount,
            '' AS item_title,
            '' AS card_code,
            COALESCE(jl.source_type,'') AS source_type,
            CASE
                WHEN jl.created_by_admin_id IS NOT NULL THEN 'admin'
                WHEN jl.source_type IN ('vault_reward','quiz_error_review') THEN 'user'
                ELSE 'system'
            END AS actor_type,
            jl.created_by_admin_id AS actor_admin_id,
            CASE
                WHEN jl.created_by_admin_id IS NOT NULL THEN COALESCE(NULLIF(a.display_name,''),'Администратор')
                WHEN jl.source_type IN ('vault_reward','quiz_error_review') THEN 'Пользователь'
                ELSE 'Система'
            END AS actor_name,
            COALESCE(jl.comment,'') AS detail_raw
        FROM jackcoin_ledger jl
        LEFT JOIN clients c ON c.id=jl.client_id
        LEFT JOIN admins a ON a.id=jl.created_by_admin_id
    )
    """

    where: list[str] = ["1=1"]
    params: list[Any] = []

    date_from = _date_boundary(filters.get("date_from", ""), timezone_name)
    date_to = _date_boundary(filters.get("date_to", ""), timezone_name, next_day=True)
    if date_from:
        where.append("datetime(created_at) >= datetime(?)")
        params.append(date_from)
    if date_to:
        where.append("datetime(created_at) < datetime(?)")
        params.append(date_to)

    query = filters.get("client", "")
    if query:
        pattern = f"%{query}%"
        compact = f"%{''.join(ch for ch in query if ch.isdigit())}%"
        where.append(
            "(client_name LIKE ? OR username LIKE ? OR phone_local LIKE ? OR phone_raw LIKE ? "
            "OR CAST(client_id AS TEXT) LIKE ?)"
        )
        params.extend((pattern, pattern, compact, pattern, pattern))

    kind = filters.get("kind", "")
    if kind in {"jackcoin", "card"}:
        where.append("kind=?")
        params.append(kind)

    operation = filters.get("operation", "")
    if operation:
        where.append("operation_group=?")
        params.append(operation)

    source = filters.get("source", "")
    if source:
        where.append("source_type=?")
        params.append(source)

    actor = filters.get("actor", "")
    if actor == "__user__":
        where.append("actor_type='user'")
    elif actor == "__system__":
        where.append("actor_type='system'")
    elif actor == "__admin__":
        where.append("actor_type='admin'")
    elif actor:
        where.append("actor_name=?")
        params.append(actor)

    code = filters.get("card_code", "")
    if code:
        where.append("card_code LIKE ?")
        params.append(f"%{code}%")

    jc_min = _positive_int(filters.get("jc_min"))
    jc_max = _positive_int(filters.get("jc_max"))
    if jc_min is not None:
        where.append("kind='jackcoin' AND ABS(amount)>=?")
        params.append(jc_min)
    if jc_max is not None:
        where.append("kind='jackcoin' AND ABS(amount)<=?")
        params.append(jc_max)

    where_sql = " AND ".join(where)
    offset = (page - 1) * page_size

    with connect(db_path) as conn:
        total = int(
            conn.execute(
                cte + f"SELECT COUNT(*) AS total FROM entries WHERE {where_sql}",
                tuple(params),
            ).fetchone()["total"]
        )
        rows = conn.execute(
            cte
            + f"""
              SELECT * FROM entries
              WHERE {where_sql}
              ORDER BY datetime(created_at) DESC, source_row_id DESC, kind DESC
              LIMIT ? OFFSET ?
            """,
            tuple([*params, page_size, offset]),
        ).fetchall()
        source_rows = conn.execute(
            """
            SELECT source_type FROM jackcoin_ledger WHERE TRIM(COALESCE(source_type,''))<>''
            UNION
            SELECT source_type FROM vault_member_rewards WHERE TRIM(COALESCE(source_type,''))<>''
            ORDER BY source_type
            """
        ).fetchall()
        actor_rows = conn.execute(
            """
            SELECT display_name AS actor_name FROM admins WHERE TRIM(COALESCE(display_name,''))<>''
            UNION
            SELECT admin_name AS actor_name FROM vault_reward_events
            WHERE TRIM(COALESCE(admin_name,''))<>'' AND LOWER(admin_name)<>'system'
            ORDER BY actor_name
            """
        ).fetchall()

    entries: list[dict[str, Any]] = []
    for row in rows:
        kind_value = str(row["kind"])
        amount = int(row["amount"] or 0)
        detail = _card_detail(row["detail_raw"]) if kind_value == "card" else str(row["detail_raw"] or "")
        entries.append(
            {
                "created_at": _format_time(row["created_at"], timezone_name),
                "kind": kind_value,
                "kind_label": "JACKCOIN" if kind_value == "jackcoin" else "JACK CARD",
                "client": str(row["client_name"] or "—"),
                "client_id": row["client_id"],
                "phone": str(row["phone_local"] or row["phone_raw"] or ""),
                "operation": _operation_label(kind_value, str(row["operation_code"] or ""), amount),
                "operation_group": str(row["operation_group"] or ""),
                "value": _entry_value(row),
                "source": _source_label(row["source_type"]),
                "source_code": str(row["source_type"] or ""),
                "actor": str(row["actor_name"] or "—"),
                "actor_type": str(row["actor_type"] or ""),
                "detail": detail,
                "card_code": str(row["card_code"] or ""),
            }
        )

    pages = max(1, ceil(total / page_size))
    if page > pages:
        page = pages

    sources = [
        {"value": str(row["source_type"]), "label": _source_label(row["source_type"])}
        for row in source_rows
    ]
    actors = [str(row["actor_name"]) for row in actor_rows if str(row["actor_name"] or "").strip()]

    return {
        "entries": entries,
        "total": total,
        "page": page,
        "pages": pages,
        "page_size": page_size,
        "sources": sources,
        "actors": actors,
        "operation_filters": _OPERATION_FILTERS,
    }


def _page_url(request: Request, page: int) -> str:
    params = dict(request.query_params)
    params["page"] = str(max(1, page))
    return f"{request.url.path}?{urlencode(params)}"


def install_vault_audit_ui(app: FastAPI) -> FastAPI:
    if getattr(app.state, "vault_audit_ui_installed", False):
        return app
    app.state.vault_audit_ui_installed = True
    settings = app.state.settings

    def account_transform(source: str, kwargs: dict[str, Any]) -> str:
        del kwargs
        if ACTIVATION_ASSET in source or "</body>" not in source:
            return source
        return source.replace(
            "</body>",
            f'<script data-vault-activation-policy src="{ACTIVATION_ASSET}" defer></script></body>',
            1,
        )

    _wrap_html_route(
        app,
        path="/account",
        marker="_vault_activation_policy_asset",
        transform=account_transform,
    )

    @app.get("/master/economy-history", response_class=HTMLResponse)
    async def economy_history(request: Request):
        if _role_from_request(request) != ACCESS_MASTER:
            raise HTTPException(status_code=403, detail="Доступ только для мастер-администратора")

        filters = {
            "date_from": request.query_params.get("date_from", ""),
            "date_to": request.query_params.get("date_to", ""),
            "client": request.query_params.get("client", ""),
            "kind": request.query_params.get("kind", ""),
            "operation": request.query_params.get("operation", ""),
            "source": request.query_params.get("source", ""),
            "actor": request.query_params.get("actor", ""),
            "card_code": request.query_params.get("card_code", ""),
            "jc_min": request.query_params.get("jc_min", ""),
            "jc_max": request.query_params.get("jc_max", ""),
        }
        try:
            page = max(1, int(request.query_params.get("page", "1")))
        except ValueError:
            page = 1

        result = query_economy_history(
            settings.db_path,
            timezone_name=settings.timezone_name,
            filters=filters,
            page=page,
        )
        current_page = int(result["page"])
        return _TEMPLATES.TemplateResponse(
            request,
            "economy_history.html",
            {
                "request": request,
                "csrf_token": str(request.session.get("csrf") or ""),
                "admin_name": str(request.session.get("admin_name") or settings.admin_name),
                "admin_role": str(request.session.get("admin_role") or "master_admin"),
                "asset_version": HISTORY_ASSET_VERSION,
                "filters": filters,
                "entries": result["entries"],
                "total": result["total"],
                "page": current_page,
                "pages": result["pages"],
                "sources": result["sources"],
                "actors": result["actors"],
                "operation_filters": result["operation_filters"],
                "prev_url": _page_url(request, current_page - 1) if current_page > 1 else "",
                "next_url": _page_url(request, current_page + 1) if current_page < int(result["pages"]) else "",
                "timezone_name": settings.timezone_name,
            },
        )

    return app


__all__ = ["install_vault_audit_ui", "query_economy_history"]
