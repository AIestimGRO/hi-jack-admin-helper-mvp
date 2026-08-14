from __future__ import annotations

import sqlite3
from typing import Any

from app.services import hijack_rating as _hijack_rating


_ORIGINAL_REFERRAL_TREE = _hijack_rating.referral_tree
_APPLIED = False


def referral_tree_without_deleted(
    conn: sqlite3.Connection,
    *,
    root_client_id: int,
    max_depth: int = _hijack_rating.MAX_REFERRAL_TREE_DEPTH,
) -> dict[str, Any]:
    """Return the referral tree without deleted/anonymized member accounts.

    Historical referral rows remain untouched. A deleted node is not re-parented, so
    descendants below that deleted account are also hidden from the member-facing tree.
    """
    payload = _ORIGINAL_REFERRAL_TREE(
        conn,
        root_client_id=root_client_id,
        max_depth=max_depth,
    )
    root = payload.get("root")
    if not root:
        return payload

    deleted_rows = conn.execute(
        "SELECT id FROM clients WHERE COALESCE(client_status, '')='deleted'"
    ).fetchall()
    deleted_ids = {int(row["id"]) for row in deleted_rows}
    if int(root.get("client_id") or 0) in deleted_ids:
        return {
            "root": None,
            "direct": 0,
            "total": 0,
            "max_depth": 0,
            "truncated": False,
        }

    total = 0
    deepest = 0

    def prune(nodes: list[dict[str, Any]], depth: int) -> list[dict[str, Any]]:
        nonlocal total, deepest
        visible: list[dict[str, Any]] = []
        for item in nodes or []:
            client_id = int(item.get("client_id") or 0)
            if client_id in deleted_ids:
                continue
            node = dict(item)
            node["depth"] = depth
            node["children"] = prune(list(item.get("children") or []), depth + 1)
            visible.append(node)
            total += 1
            deepest = max(deepest, depth)
        return visible

    root_payload = dict(root)
    root_payload["children"] = prune(list(root.get("children") or []), 1)
    return {
        "root": root_payload,
        "direct": len(root_payload["children"]),
        "total": total,
        "max_depth": deepest,
        "truncated": bool(payload.get("truncated")),
    }


def apply_referral_tree_visibility_policy() -> None:
    global _APPLIED
    if _APPLIED:
        return
    _hijack_rating.referral_tree = referral_tree_without_deleted
    _APPLIED = True


__all__ = [
    "apply_referral_tree_visibility_policy",
    "referral_tree_without_deleted",
]
