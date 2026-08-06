#!/usr/bin/env python3
"""Repair JACKSIDE issue schedules in quiz_campaigns without serving a request."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import transaction
from app.services.jackside_issues import sync_campaign_schedule_from_issue


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize jackside_issues schedules into quiz_campaigns."
    )
    parser.add_argument("db_path", type=Path, help="Path to the SQLite database")
    parser.add_argument(
        "--timezone",
        default="Europe/Moscow",
        help="Club IANA timezone (default: Europe/Moscow)",
    )
    args = parser.parse_args()

    repaired = 0
    with transaction(args.db_path) as conn:
        issues = conn.execute(
            """
            SELECT * FROM jackside_issues
            WHERE campaign_code IS NOT NULL AND campaign_code != ''
            ORDER BY id
            """
        ).fetchall()
        for issue in issues:
            sync_campaign_schedule_from_issue(
                conn, issue, timezone_name=args.timezone
            )
            repaired += 1

    print(f"Repaired {repaired} JACKSIDE campaign schedules.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
