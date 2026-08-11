from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
from typing import Any

from app.services.login_security import (
    login_is_locked,
    record_login_failure,
    record_login_success,
)


PBKDF2_ITERATIONS = 310_000
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,40}$")


def hash_pin(pin: str, *, salt: bytes | None = None) -> str:
    if len(pin) < 4 or len(pin) > 128:
        raise ValueError("pin_length")
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_pin(pin: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def validate_username(username: str) -> str:
    value = username.strip().lower()
    if not USERNAME_RE.fullmatch(value):
        raise ValueError("invalid_username")
    return value


def bootstrap_master(conn: sqlite3.Connection, *, username: str, display_name: str, pin: str) -> None:
    if conn.execute("SELECT 1 FROM admins LIMIT 1").fetchone():
        return
    conn.execute(
        "INSERT INTO admins(username, display_name, pin_hash, role) VALUES (?, ?, ?, 'master_admin')",
        (validate_username(username), display_name.strip() or "Мастер-администратор", hash_pin(pin)),
    )


def authenticate(conn: sqlite3.Connection, username: str, pin: str):
    try:
        normalized = validate_username(username)
    except ValueError:
        return None
    row = conn.execute(
        "SELECT * FROM admins WHERE username = ? AND is_active = 1",
        (normalized,),
    ).fetchone()
    if not row:
        return None
    admin_id = int(row["id"])
    if login_is_locked(
        conn,
        principal_type="admin",
        principal_id=admin_id,
    ):
        return None
    if not verify_pin(pin, row["pin_hash"]):
        record_login_failure(
            conn,
            principal_type="admin",
            principal_id=admin_id,
        )
        return None
    record_login_success(
        conn,
        principal_type="admin",
        principal_id=admin_id,
    )
    conn.execute(
        "UPDATE admins SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?",
        (admin_id,),
    )
    return row


def audit(
    conn: sqlite3.Connection,
    *,
    admin_id: int,
    admin_name: str,
    action: str,
    entity_type: str,
    entity_id: int | None,
    details: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO admin_audit_log(admin_id, admin_name, action, entity_type, entity_id, details) VALUES (?, ?, ?, ?, ?, ?)",
        (admin_id, admin_name, action, entity_type, entity_id, json.dumps(details or {}, ensure_ascii=False, sort_keys=True)),
    )
