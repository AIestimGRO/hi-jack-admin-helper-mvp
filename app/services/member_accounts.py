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

from app.services.login_security import (
    login_is_locked,
    record_login_failure,
    record_login_success,
)
from app.services.quiz_identity import normalize_email


MEMBER_COOKIE_NAME = "hjc_member_session"
PASSWORD_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
SESSION_TOUCH_INTERVAL = timedelta(minutes=5)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def validate_password(password: str) -> str:
    value = str(password or "")
    if not MIN_PASSWORD_LENGTH <= len(value) <= MAX_PASSWORD_LENGTH:
        raise ValueError(
            f"Пароль должен содержать от {MIN_PASSWORD_LENGTH} до {MAX_PASSWORD_LENGTH} символов"
        )
    if not any(character.isalpha() for character in value):
        raise ValueError("Добавьте в пароль хотя бы одну букву")
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


def _session_touch_due(value: Any, now: datetime) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return True
    try:
        touched = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if touched.tzinfo is None:
        touched = touched.replace(tzinfo=timezone.utc)
    else:
        touched = touched.astimezone(timezone.utc)
    return touched <= now - SESSION_TOUCH_INTERVAL


def authenticated_member(
    conn: sqlite3.Connection,
    *,
    secret_key: str,
    token: str,
    touch: bool = True,
) -> sqlite3.Row | None:
    if not token:
        return None
    now_dt = datetime.now(timezone.utc)
    now = _timestamp(now_dt)
    row = conn.execute(
        """
        SELECT ma.*, ms.id AS member_session_id,
               ms.last_used_at AS member_session_last_used_at,
               c.first_name, c.nickname, c.username,
               c.phone_raw, c.phone_full, c.phone_local, c.telegram_user_id
        FROM member_sessions ms
        JOIN member_accounts ma ON ma.id = ms.account_id
        JOIN clients c ON c.id = ma.client_id
        WHERE ms.token_hash=? AND ms.revoked_at IS NULL AND ms.expires_at>?
          AND ma.is_active=1 AND ms.session_version=ma.session_version
        """,
        (session_token_hash(secret_key, token), now),
    ).fetchone()
    if row and touch and _session_touch_due(row["member_session_last_used_at"], now_dt):
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
    if not row:
        return None
    account_id = int(row["id"])
    if login_is_locked(
        conn,
        principal_type="member",
        principal_id=account_id,
    ):
        return None
    if not verify_password(password, row["password_hash"]):
        record_login_failure(
            conn,
            principal_type="member",
            principal_id=account_id,
        )
        return None
    record_login_success(
        conn,
        principal_type="member",
        principal_id=account_id,
    )
    conn.execute(
        "UPDATE member_accounts SET last_login_at=CURRENT_TIMESTAMP WHERE id=?",
        (account_id,),
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


def credit_jackcoin_manually(
    conn: sqlite3.Connection,
    *,
    client_id: int,
    amount: int,
    admin_id: int,
    reason: str,
    comment: str,
    operation_token: str,
) -> tuple[bool, int]:
    if amount <= 0 or amount > 1_000_000:
        raise ValueError("invalid_jackcoin_amount")

    clean_reason = " ".join(str(reason or "").split())[:120]
    if not clean_reason:
        raise ValueError("jackcoin_reason_required")

    clean_comment = " ".join(str(comment or "").split())[:300]
    clean_token = "".join(
        character
        for character in str(operation_token or "")
        if character.isalnum() or character in {"_", "-"}
    )[:128]
    if len(clean_token) < 12:
        raise ValueError("invalid_jackcoin_operation_token")

    client = conn.execute(
        "SELECT id FROM clients WHERE id=? AND IFNULL(client_status,'')<>'deleted'",
        (int(client_id),),
    ).fetchone()
    if not client:
        raise ValueError("client_not_found")

    note = clean_reason if not clean_comment else f"{clean_reason} · {clean_comment}"
    idempotency_key = f"admin:jackcoin:credit:{int(client_id)}:{clean_token}"
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO jackcoin_ledger(
            client_id,amount,operation_type,source_type,source_id,
            idempotency_key,comment,created_by_admin_id
        ) VALUES (?,?,'earn','admin',?,?,?,?)
        """,
        (
            int(client_id),
            int(amount),
            clean_token,
            idempotency_key,
            note,
            int(admin_id),
        ),
    )
    return cursor.rowcount == 1, jackcoin_balance(conn, int(client_id))


def debit_jackcoin_manually(
    conn: sqlite3.Connection,
    *,
    client_id: int,
    amount: int,
    admin_id: int,
    reason: str,
    comment: str,
    operation_token: str,
) -> tuple[bool, int]:
    if amount <= 0 or amount > 1_000_000:
        raise ValueError("invalid_jackcoin_amount")

    clean_reason = " ".join(str(reason or "").split())[:120]
    if not clean_reason:
        raise ValueError("jackcoin_reason_required")

    clean_comment = " ".join(str(comment or "").split())[:300]
    clean_token = "".join(
        character
        for character in str(operation_token or "")
        if character.isalnum() or character in {"_", "-"}
    )[:128]
    if len(clean_token) < 12:
        raise ValueError("invalid_jackcoin_operation_token")

    client = conn.execute(
        "SELECT id FROM clients WHERE id=? AND IFNULL(client_status,'')<>'deleted'",
        (int(client_id),),
    ).fetchone()
    if not client:
        raise ValueError("client_not_found")

    idempotency_key = f"admin:jackcoin:debit:{int(client_id)}:{clean_token}"
    existing = conn.execute(
        "SELECT id FROM jackcoin_ledger WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if existing:
        return False, jackcoin_balance(conn, int(client_id))

    balance = jackcoin_balance(conn, int(client_id))
    if balance < int(amount):
        raise ValueError("insufficient_jackcoin")

    note = clean_reason if not clean_comment else f"{clean_reason} · {clean_comment}"
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO jackcoin_ledger(
            client_id,amount,operation_type,source_type,source_id,
            idempotency_key,comment,created_by_admin_id
        ) VALUES (?,?,'spend','admin',?,?,?,?)
        """,
        (
            int(client_id),
            -int(amount),
            clean_token,
            idempotency_key,
            note,
            int(admin_id),
        ),
    )
    return cursor.rowcount == 1, jackcoin_balance(conn, int(client_id))
