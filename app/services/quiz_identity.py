from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from typing import Any

from app.services.clients import clean
from app.services.phone import full_phone, normalize_phone


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,64}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def normalize_username(value: Any) -> str | None:
    username = str(value or "").strip().lstrip("@")
    if not username:
        return None
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("username_invalid")
    return username


def normalize_email(value: Any) -> str | None:
    email = str(value or "").strip().casefold()
    if not email:
        return None
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        raise ValueError("email_invalid")
    return email


def email_code_hash(secret_key: str, email: str, code: str) -> str:
    message = f"quiz-email:{email}:{code}".encode("utf-8")
    return hmac.new(secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def generate_email_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def find_or_create_quiz_client(
    conn: sqlite3.Connection,
    *,
    campaign: str,
    phone_raw: str = "",
    username: str = "",
    name: str = "",
    nickname: str = "",
    email: str = "",
    telegram_user_id: str = "",
    source: str = "",
    referrer_id: str = "",
) -> tuple[int, bool, dict[str, str | None]]:
    phone_value = str(phone_raw or "").strip()[:80]
    phone_local = normalize_phone(phone_value) if phone_value else None
    if phone_value and not phone_local:
        raise ValueError("phone_invalid")
    normalized_username = normalize_username(username)
    normalized_email = normalize_email(email)
    telegram_user_id = str(telegram_user_id or "").strip()[:80]
    if not phone_local and not normalized_username:
        raise ValueError("identity_required")

    candidate_ids: set[int] = set()
    lookups = []
    if telegram_user_id:
        lookups.append(("telegram_user_id = ?", telegram_user_id))
    if phone_local:
        lookups.append(("phone_local = ?", phone_local))
    if normalized_username:
        lookups.append(("username = ? COLLATE NOCASE", normalized_username))
    if normalized_email:
        lookups.append(("email_normalized = ?", normalized_email))
    for clause, value in lookups:
        candidate_ids.update(int(row[0]) for row in conn.execute(f"SELECT id FROM clients WHERE {clause}", (value,)))
    if len(candidate_ids) > 1:
        raise ValueError("identity_conflict")

    first_name = clean(name)
    nickname_value = clean(nickname)
    is_new = not candidate_ids
    acquisition_source = str(source or "").strip()[:80] or f"quiz:{campaign}"
    if candidate_ids:
        client_id = candidate_ids.pop()
        conn.execute(
            """
            UPDATE clients SET
                phone_raw=CASE WHEN (phone_raw IS NULL OR phone_raw='') AND ? IS NOT NULL THEN ? ELSE phone_raw END,
                phone_full=CASE WHEN (phone_full IS NULL OR phone_full='') AND ? IS NOT NULL THEN ? ELSE phone_full END,
                phone_local=CASE WHEN (phone_local IS NULL OR phone_local='') AND ? IS NOT NULL THEN ? ELSE phone_local END,
                username=CASE WHEN (username IS NULL OR username='') AND ? IS NOT NULL THEN ? ELSE username END,
                first_name=CASE WHEN (first_name IS NULL OR first_name='') AND ? IS NOT NULL THEN ? ELSE first_name END,
                nickname=CASE WHEN (nickname IS NULL OR nickname='') AND ? IS NOT NULL THEN ? ELSE nickname END,
                email=CASE WHEN (email IS NULL OR email='') AND ? IS NOT NULL THEN ? ELSE email END,
                email_normalized=CASE WHEN (email_normalized IS NULL OR email_normalized='') AND ? IS NOT NULL THEN ? ELSE email_normalized END,
                telegram_user_id=CASE WHEN (telegram_user_id IS NULL OR telegram_user_id='') AND ?<>'' THEN ? ELSE telegram_user_id END,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                phone_local, phone_value or None, phone_local, full_phone(phone_local) if phone_local else None,
                phone_local, phone_local, normalized_username, normalized_username, first_name, first_name,
                nickname_value, nickname_value, normalized_email, normalized_email, normalized_email,
                normalized_email, telegram_user_id, telegram_user_id, client_id,
            ),
        )
    else:
        cursor = conn.execute(
            """
            INSERT INTO clients(
                first_name, nickname, username, phone_raw, phone_full, phone_local,
                telegram_user_id, email, email_normalized, source, client_status,
                acquisition_campaign_code, acquisition_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (
                first_name, nickname_value, normalized_username, phone_value or None,
                full_phone(phone_local) if phone_local else None, phone_local, telegram_user_id or None,
                normalized_email, normalized_email, acquisition_source, campaign, acquisition_source,
            ),
        )
        client_id = int(cursor.lastrowid)

    conn.execute(
        """
        INSERT INTO client_quiz_campaigns(client_id, campaign_code, first_source, first_referrer_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(client_id, campaign_code) DO UPDATE SET last_seen_at=CURRENT_TIMESTAMP
        """,
        (client_id, campaign, acquisition_source, str(referrer_id or "").strip()[:80] or None),
    )
    row = conn.execute(
        "SELECT phone_raw, phone_local, username, first_name, nickname, email, telegram_user_id FROM clients WHERE id=?",
        (client_id,),
    ).fetchone()
    return client_id, is_new, {key: row[key] for key in row.keys()}


def identity_json(**values: Any) -> str:
    return json.dumps(values, ensure_ascii=False, sort_keys=True)
