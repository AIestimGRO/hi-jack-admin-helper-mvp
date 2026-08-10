from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.db import connect
from app.product_shell import _current_member


def _as_title(row: Any, *, state: str, temporary: bool = False) -> dict[str, Any]:
    data = dict(row)
    return {
        "kind": "title",
        "state": state,
        "definition_id": int(data["definition_id"] if "definition_id" in data else data["id"]),
        "member_title_id": int(data["member_title_id"]) if data.get("member_title_id") else None,
        "selected": bool(data.get("selected")),
        "temporary": bool(temporary),
        "name": str(data.get("name") or "Звание"),
        "description": str(data.get("description") or ""),
        "icon": str(data.get("icon") or "★"),
        "icon_path": str(data.get("icon_path") or ""),
        "awarded_at": str(data.get("awarded_at") or ""),
        "expires_at": str(data.get("expires_at") or ""),
    }


def _as_achievement(row: Any, *, state: str) -> dict[str, Any]:
    data = dict(row)
    return {
        "kind": "achievement",
        "state": state,
        "definition_id": int(data["definition_id"] if "definition_id" in data else data["id"]),
        "member_title_id": None,
        "selected": False,
        "temporary": False,
        "name": str(data.get("name") or "Достижение"),
        "description": str(data.get("description") or ""),
        "icon": str(data.get("icon") or "◆"),
        "icon_path": str(data.get("icon_path") or ""),
        "awarded_at": str(data.get("awarded_at") or ""),
        "expires_at": "",
    }


def title_collection_payload(conn, *, client_id: int) -> dict[str, Any]:
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    permanent_rows = conn.execute(
        """
        SELECT td.id AS definition_id,mt.id AS member_title_id,mt.selected,mt.awarded_at,
               NULL AS expires_at,td.name,td.description,td.icon,td.icon_path,td.priority
        FROM member_titles mt
        JOIN title_definitions td ON td.id=mt.title_definition_id
        WHERE mt.client_id=? AND mt.temporary_period_id IS NULL AND td.is_enabled=1
        ORDER BY mt.selected DESC,td.priority DESC,mt.awarded_at DESC,mt.id DESC
        """,
        (client_id,),
    ).fetchall()
    temporary_rows = conn.execute(
        """
        SELECT td.id AS definition_id,mt.id AS member_title_id,0 AS selected,mt.awarded_at,
               mt.expires_at,td.name,td.description,td.icon,td.icon_path,td.priority
        FROM member_titles mt
        JOIN title_definitions td ON td.id=mt.title_definition_id
        WHERE mt.client_id=? AND mt.temporary_period_id IS NOT NULL AND td.is_enabled=1
          AND mt.expires_at>?
        ORDER BY td.priority DESC,mt.awarded_at DESC,mt.id DESC
        """,
        (client_id, now_iso),
    ).fetchall()
    achievement_rows = conn.execute(
        """
        SELECT ad.id AS definition_id,ma.awarded_at,ad.name,ad.description,ad.icon,ad.icon_path
        FROM member_achievements ma
        JOIN achievement_definitions ad ON ad.id=ma.achievement_id
        WHERE ma.client_id=? AND ad.is_enabled=1
        ORDER BY ma.awarded_at DESC,ma.id DESC
        """,
        (client_id,),
    ).fetchall()

    active_titles = [
        *[_as_title(row, state="active", temporary=False) for row in permanent_rows],
        *[_as_title(row, state="active", temporary=True) for row in temporary_rows],
    ]
    active_achievements = [_as_achievement(row, state="active") for row in achievement_rows]

    active_title_ids = {item["definition_id"] for item in active_titles}
    active_achievement_ids = {item["definition_id"] for item in active_achievements}

    locked_titles = []
    for row in conn.execute(
        "SELECT id,name,description,icon,icon_path,title_type FROM title_definitions WHERE is_enabled=1 ORDER BY priority DESC,id",
    ).fetchall():
        if int(row["id"]) in active_title_ids:
            continue
        locked_titles.append(
            _as_title(row, state="locked", temporary=str(row["title_type"]) == "temporary")
        )

    locked_achievements = []
    for row in conn.execute(
        "SELECT id,name,description,icon,icon_path FROM achievement_definitions WHERE is_enabled=1 ORDER BY position,id",
    ).fetchall():
        if int(row["id"]) in active_achievement_ids:
            continue
        locked_achievements.append(_as_achievement(row, state="locked"))

    items = [*active_titles, *active_achievements, *locked_titles, *locked_achievements]
    return {
        "items": items,
        "active_count": len(active_titles) + len(active_achievements),
        "total_count": len(items),
    }


def install_profile_experience(app: FastAPI) -> FastAPI:
    if getattr(app.state, "profile_experience_installed", False):
        return app
    app.state.profile_experience_installed = True
    settings = app.state.settings

    @app.get("/api/account/title-collection")
    async def account_title_collection(request: Request):
        member = _current_member(request, required=True)
        with connect(settings.db_path) as conn:
            payload = title_collection_payload(conn, client_id=int(member["client_id"]))
        return JSONResponse(payload)

    return app


__all__ = ["install_profile_experience", "title_collection_payload"]
