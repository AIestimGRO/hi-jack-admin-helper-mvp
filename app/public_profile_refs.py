from __future__ import annotations

import base64
import hashlib
import hmac
import sqlite3


PUBLIC_PROFILE_REF_BYTES = 12


def public_profile_ref(secret_key: str, client_id: int) -> str:
    digest = hmac.new(
        str(secret_key).encode("utf-8"),
        f"public-profile:{int(client_id)}".encode("utf-8"),
        hashlib.sha256,
    ).digest()[:PUBLIC_PROFILE_REF_BYTES]
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def resolve_public_profile_ref(
    conn: sqlite3.Connection,
    *,
    secret_key: str,
    profile_ref: str,
) -> int | None:
    candidate = str(profile_ref or "").strip()
    if not candidate:
        return None
    rows = conn.execute(
        """
        SELECT c.id
        FROM member_accounts ma
        JOIN clients c ON c.id=ma.client_id
        WHERE ma.is_active=1 AND IFNULL(c.client_status,'')<>'deleted'
        ORDER BY c.id
        """
    ).fetchall()
    for row in rows:
        client_id = int(row["id"])
        if hmac.compare_digest(public_profile_ref(secret_key, client_id), candidate):
            return client_id
    return None


__all__ = ["public_profile_ref", "resolve_public_profile_ref"]
