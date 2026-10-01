from __future__ import annotations

import html
import inspect
import json
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute

from app.db import connect


ACTIVATION_ASSET = "/static/js/vault-activation-policy.js"

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
    transform: Callable[[str], str],
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
        updated = transform(source)
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


def _sort_time(value: Any) -> datetime:
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _client_name(row: Any) -> str:
    return str(
        row["first_name"]
        or row["nickname"]
        or (f"@{str(row['username']).lstrip('@')}" if row["username"] else "")
        or f"Клиент #{row['client_id']}"
    )


def _card_actor(row: Any) -> str:
    admin_name = str(row["admin_name"] or "").strip()
    if admin_name and admin_name.lower() != "system":
        return admin_name
    action = str(row["action"] or "")
    if action in {"activated", "purchase_issued"}:
        return "Пользователь"
    return "Система"


def _jc_actor(row: Any) -> str:
    admin_name = str(row["actor_admin_name"] or "").strip()
    if admin_name:
        return admin_name
    source = str(row["source_type"] or "")
    if source in {"vault_reward", "quiz_error_review"}:
        return "Пользователь"
    return "Система"


def _render_audit(app: FastAPI) -> str:
    settings = app.state.settings
    with connect(settings.db_path) as conn:
        card_rows = conn.execute(
            """
            SELECT e.*, vcr.title AS reward_title,
                   c.first_name, c.nickname, c.username
            FROM vault_reward_events e
            LEFT JOIN vault_catalog_rewards vcr ON vcr.id=e.catalog_reward_id
            LEFT JOIN clients c ON c.id=e.client_id
            ORDER BY e.id DESC
            LIMIT 200
            """
        ).fetchall()
        jc_rows = conn.execute(
            """
            SELECT jl.*, c.first_name, c.nickname, c.username,
                   a.display_name AS actor_admin_name
            FROM jackcoin_ledger jl
            LEFT JOIN clients c ON c.id=jl.client_id
            LEFT JOIN admins a ON a.id=jl.created_by_admin_id
            ORDER BY jl.id DESC
            LIMIT 200
            """
        ).fetchall()

    entries: list[dict[str, Any]] = []
    for row in card_rows:
        action = str(row["action"] or "")
        details = {}
        try:
            details = json.loads(str(row["details"] or "{}"))
        except json.JSONDecodeError:
            details = {}
        entries.append(
            {
                "created_at": row["created_at"],
                "kind": "JACK CARD",
                "client": _client_name(row),
                "operation": _CARD_ACTIONS.get(action, action or "Операция с картой"),
                "value": str(row["reward_title"] or "JACK CARD") + f" · {row['code']}",
                "actor": _card_actor(row),
                "detail": str(details.get("reason") or ""),
            }
        )
    for row in jc_rows:
        amount = int(row["amount"] or 0)
        entries.append(
            {
                "created_at": row["created_at"],
                "kind": "JACKCOIN",
                "client": _client_name(row),
                "operation": "Начисление JC" if amount > 0 else "Списание JC",
                "value": f"{'+' if amount > 0 else ''}{amount} JC",
                "actor": _jc_actor(row),
                "detail": str(row["comment"] or row["source_type"] or ""),
            }
        )

    entries.sort(key=lambda item: _sort_time(item["created_at"]), reverse=True)
    entries = entries[:300]
    rows = []
    for entry in entries:
        detail = html.escape(entry["detail"])
        operation = html.escape(entry["operation"])
        if detail:
            operation += f"<small>{detail}</small>"
        rows.append(
            "<tr>"
            f"<td data-label=\"Время\">{html.escape(_format_time(entry['created_at'], settings.timezone_name))}</td>"
            f"<td data-label=\"Тип\"><strong>{html.escape(entry['kind'])}</strong></td>"
            f"<td data-label=\"Клиент\">{html.escape(entry['client'])}</td>"
            f"<td data-label=\"Операция\">{operation}</td>"
            f"<td data-label=\"Изменение\">{html.escape(entry['value'])}</td>"
            f"<td data-label=\"Кто\">{html.escape(entry['actor'])}</td>"
            "</tr>"
        )
    body = "".join(rows) or '<tr><td colspan="6" class="empty">Операций пока нет</td></tr>'
    return f"""
<style>
  .vault-events {{ display:none !important; }}
  .vault-unified-audit small {{ display:block; margin-top:4px; opacity:.72; }}
  .vault-unified-audit .audit-note {{ margin:0 0 14px; opacity:.72; }}
</style>
<details class="card vault-unified-audit" open>
  <summary>Полная история JACK CARDS и JACKCOIN</summary>
  <p class="audit-note">Время показано по часовому поясу клуба. Для ручных операций отображается конкретный администратор; для автоматических — «Система», для действий из личного кабинета — «Пользователь».</p>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Время</th><th>Тип</th><th>Клиент</th><th>Операция</th><th>Изменение</th><th>Кто</th></tr></thead>
      <tbody>{body}</tbody>
    </table>
  </div>
</details>
"""


def install_vault_audit_ui(app: FastAPI) -> FastAPI:
    if getattr(app.state, "vault_audit_ui_installed", False):
        return app
    app.state.vault_audit_ui_installed = True

    def account_transform(source: str) -> str:
        if ACTIVATION_ASSET in source or "</body>" not in source:
            return source
        return source.replace(
            "</body>",
            f'<script data-vault-activation-policy src="{ACTIVATION_ASSET}" defer></script></body>',
            1,
        )

    def vault_transform(source: str) -> str:
        if "vault-unified-audit" in source or "</main>" not in source:
            return source
        return source.replace("</main>", _render_audit(app) + "</main>", 1)

    _wrap_html_route(
        app,
        path="/account",
        marker="_vault_activation_policy_asset",
        transform=account_transform,
    )
    _wrap_html_route(
        app,
        path="/admin/vault",
        marker="_vault_unified_audit_ui",
        transform=vault_transform,
    )
    return app


__all__ = ["install_vault_audit_ui"]
