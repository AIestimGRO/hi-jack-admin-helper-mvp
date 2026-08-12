from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI

from app import main_impl
from app.services import jackside_analytics


_ORIGINAL_GET_OR_REFRESH = jackside_analytics.get_or_refresh_snapshot


def get_current_jackside_snapshot(
    conn,
    *,
    as_of=None,
    timezone_name: str = "Europe/Moscow",
    force: bool = False,
    refresh: Callable[..., dict[str, Any]] = jackside_analytics.refresh_jackside_analytics,
) -> dict[str, Any]:
    payload = jackside_analytics.load_jackside_analytics_snapshot(conn)
    # A rating page should never wait for the periodic five-minute refresh after
    # a completed game, final result, account/rating transfer or other source
    # mutation. source_version is bumped transactionally by DB triggers.
    if force or not payload or jackside_analytics.analytics_sources_changed(conn):
        return refresh(conn, as_of=as_of, timezone_name=timezone_name)
    return _ORIGINAL_GET_OR_REFRESH(
        conn,
        as_of=as_of,
        timezone_name=timezone_name,
        force=False,
        refresh=refresh,
    )


def install_jackside_rating_freshness(app: FastAPI) -> FastAPI:
    if getattr(app.state, "jackside_rating_freshness_installed", False):
        return app
    app.state.jackside_rating_freshness_installed = True
    # main_impl imported the function directly, so patch both the service module
    # and main_impl's module global used by existing handlers/middleware.
    jackside_analytics.get_or_refresh_snapshot = get_current_jackside_snapshot
    main_impl.get_or_refresh_snapshot = get_current_jackside_snapshot
    return app


__all__ = [
    "get_current_jackside_snapshot",
    "install_jackside_rating_freshness",
]
