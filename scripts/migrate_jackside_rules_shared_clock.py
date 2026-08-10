#!/usr/bin/env python3
"""Activate the shared-clock JACKSIDE rules without overwriting custom rules."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import transaction
from app.services.jackside_copy import (
    DEFAULT_RULES_CONTENT,
    DEFAULT_RULES_TITLE,
    DEFAULT_RULES_VERSION,
)

LEGACY_BUILTIN_VERSION = "1.0"
LEGACY_BUILTIN_MARKER = "10 вопросов, 4:14, одна попытка, финал на вылет."


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Activate the versioned JACKSIDE shared-clock rules. "
            "Only the original built-in 1.0 rules are migrated automatically."
        )
    )
    parser.add_argument("db_path", type=Path, help="Path to the SQLite database")
    args = parser.parse_args()

    with transaction(args.db_path) as conn:
        active = conn.execute(
            """
            SELECT * FROM jackside_rules_versions
            WHERE is_active=1 ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        if active and str(active["version"]) == DEFAULT_RULES_VERSION:
            print(f"JACKSIDE rules {DEFAULT_RULES_VERSION} are already active.")
            return 0
        if active:
            is_builtin_legacy = (
                str(active["version"]) == LEGACY_BUILTIN_VERSION
                and str(active["title"]) == DEFAULT_RULES_TITLE
                and LEGACY_BUILTIN_MARKER in str(active["content"] or "")
            )
            if not is_builtin_legacy:
                print(
                    "Refusing to replace a custom active JACKSIDE rules version: "
                    f"{active['version']}. Publish the new rules manually after review."
                )
                return 2

        conn.execute("UPDATE jackside_rules_versions SET is_active=0 WHERE is_active=1")
        existing = conn.execute(
            "SELECT id FROM jackside_rules_versions WHERE version=?",
            (DEFAULT_RULES_VERSION,),
        ).fetchone()
        if existing:
            rules_id = int(existing["id"])
            conn.execute(
                """
                UPDATE jackside_rules_versions
                SET title=?, content=?, is_active=1,
                    published_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (DEFAULT_RULES_TITLE, DEFAULT_RULES_CONTENT, rules_id),
            )
        else:
            rules_id = int(
                conn.execute(
                    """
                    INSERT INTO jackside_rules_versions(
                        version, title, content, is_active
                    ) VALUES (?, ?, ?, 1)
                    """,
                    (
                        DEFAULT_RULES_VERSION,
                        DEFAULT_RULES_TITLE,
                        DEFAULT_RULES_CONTENT,
                    ),
                ).lastrowid
            )

        updated_issues = conn.execute(
            """
            UPDATE jackside_issues
            SET rules_version_id=?, rules_version=?, updated_at=CURRENT_TIMESTAMP
            WHERE rules_version=?
              AND status IN ('draft', 'scheduled', 'lobby')
            """,
            (rules_id, DEFAULT_RULES_VERSION, LEGACY_BUILTIN_VERSION),
        ).rowcount

    print(
        f"Activated JACKSIDE rules {DEFAULT_RULES_VERSION}; "
        f"updated {updated_issues} not-yet-started issue(s)."
    )
    print("Existing members must accept the new active rules version before play.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
