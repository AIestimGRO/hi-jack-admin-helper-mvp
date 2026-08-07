from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from app.services.jackside_analytics import build_jackside_analytics


def jackside_leaderboard(
    conn: sqlite3.Connection,
    *,
    as_of: datetime | str | None = None,
) -> list[dict[str, Any]]:
    """Compatibility wrapper for the rolling JACKSIDE month rating."""
    return list(build_jackside_analytics(conn, as_of=as_of)["month"])
