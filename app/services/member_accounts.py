from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.quiz_identity import normalize_email


MEMBER_COOKIE_NAME = "hjc_member_session"
PASSWORD_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def validate_password(password: str) -> str:
    value = str(password or "")
    if not MIN_PASSWORD_LENGTH <= len(value) <= MAX_PASSWORD_LENGTH:
        raise ValueError("Пароль должен содержать от 10 до 128 символов")
    if not any(character.isalpha() for character in value) or not any(
        character.isdigit() for character in value
    ):
        raise ValueError("Добавьте в пароль хотя бы одну букву и одну цифру")
    return value


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    value = validate_password(password)
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", value.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256", str(password).encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def member_code_hash(secret_key: str, purpose: str, email: str, code: str) -> str:
    message = f"member-email:{purpose}:{email}:{code}".encode("utf-8")
    return hmac.new(secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def generate_email_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def session_token_hash(secret_key: str, token: str) -> str:
    return hmac.new(
        secret_key.encode("utf-8"),
        f"member-session:{token}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def issue_session(
    conn: sqlite3.Connection,
    *,
    secret_key: str,
    account_id: int,
    session_version: int,
    days: int,
    ip_hash: str,
    user_agent: str,
) -> str:
    token = secrets.token_urlsafe(40)
    conn.execute(
        """
        INSERT INTO member_sessions(
            account_id, token_hash, session_version, expires_at, ip_hash, user_agent
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            account_id,
            session_token_hash(secret_key, token),
            session_version,
            _timestamp(datetime.now(timezone.utc) + timedelta(days=days)),
            ip_hash,
            user_agent[:500],
        ),
    )
    return token


def authenticated_member(
    conn: sqlite3.Connection,
    *,
    secret_key: str,
    token: str,
    touch: bool = True,
) -> sqlite3.Row | None:
    if not token:
        return None
    now = _timestamp(datetime.now(timezone.utc))
    row = conn.execute(
        """
        SELECT ma.*, ms.id AS member_session_id, c.first_name, c.nickname, c.username,
               c.phone_raw, c.phone_full, c.phone_local, c.telegram_user_id
        FROM member_sessions ms
        JOIN member_accounts ma ON ma.id = ms.account_id
        JOIN clients c ON c.id = ma.client_id
        WHERE ms.token_hash=? AND ms.revoked_at IS NULL AND ms.expires_at>?
          AND ma.is_active=1 AND ms.session_version=ma.session_version
        """,
        (session_token_hash(secret_key, token), now),
    ).fetchone()
    if row and touch:
        conn.execute(
            "UPDATE member_sessions SET last_used_at=CURRENT_TIMESTAMP WHERE id=?",
            (row["member_session_id"],),
        )
    return row


def revoke_session(conn: sqlite3.Connection, *, secret_key: str, token: str) -> None:
    if token:
        conn.execute(
            """
            UPDATE member_sessions SET revoked_at=CURRENT_TIMESTAMP
            WHERE token_hash=? AND revoked_at IS NULL
            """,
            (session_token_hash(secret_key, token),),
        )


def authenticate_account(
    conn: sqlite3.Connection, *, email: str, password: str
) -> sqlite3.Row | None:
    normalized = normalize_email(email)
    if not normalized:
        return None
    row = conn.execute(
        "SELECT * FROM member_accounts WHERE email_normalized=? AND is_active=1",
        (normalized,),
    ).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        return None
    conn.execute(
        "UPDATE member_accounts SET last_login_at=CURRENT_TIMESTAMP WHERE id=?",
        (row["id"],),
    )
    return row


def active_legal_documents(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT * FROM legal_documents WHERE is_active=1
        ORDER BY CASE code WHEN 'privacy' THEN 1 WHEN 'rewards' THEN 2 ELSE 99 END, id
        """
    ).fetchall()
    return {str(row["code"]): row for row in rows}


def consent_payload(documents: dict[str, sqlite3.Row], accepted: dict[str, Any]) -> str:
    values = []
    for code in ("privacy", "rewards"):
        document = documents.get(code)
        item = accepted.get(code) if isinstance(accepted, dict) else None
        if not document or not isinstance(item, dict):
            raise ValueError("Подтвердите оба документа")
        if str(item.get("version")) != str(document["version"]):
            raise ValueError("Условия изменились. Ознакомьтесь с документами заново")
        values.append(
            {
                "document_id": int(document["id"]),
                "code": code,
                "version": str(document["version"]),
                "accepted_at": str(item.get("accepted_at") or ""),
            }
        )
    return json.dumps(values, ensure_ascii=False, sort_keys=True)


def jackcoin_balance(conn: sqlite3.Connection, client_id: int) -> int:
    return int(
        conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM jackcoin_ledger WHERE client_id=?",
            (client_id,),
        ).fetchone()[0]
    )
