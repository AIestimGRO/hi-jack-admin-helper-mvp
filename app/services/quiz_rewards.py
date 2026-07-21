from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.services.clients import ensure_preferences
from app.services.preferences import change_counter


CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def _timestamp(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds") if value else None


def reward_period(campaign: sqlite3.Row | dict[str, Any], timezone_name: str) -> tuple[str | None, str | None]:
    now = datetime.now(timezone.utc)
    local_zone = ZoneInfo(timezone_name)
    mode = str(campaign["reward_validity_mode"] or "end_of_day")
    value = max(0, int(campaign["reward_validity_value"] or 0))
    if mode == "unlimited":
        return _timestamp(now), None
    if mode == "hours":
        return _timestamp(now), _timestamp(now + timedelta(hours=max(1, value)))
    if mode == "days":
        return _timestamp(now), _timestamp(now + timedelta(days=max(1, value)))
    if mode == "fixed":
        start_raw = campaign["reward_valid_from"]
        until_raw = campaign["reward_valid_until"]
        start = datetime.fromisoformat(start_raw).replace(tzinfo=local_zone).astimezone(timezone.utc) if start_raw else now
        until = datetime.fromisoformat(until_raw).replace(tzinfo=local_zone).astimezone(timezone.utc) if until_raw else None
        return _timestamp(start), _timestamp(until)
    local_now = now.astimezone(local_zone)
    local_end = datetime.combine(local_now.date(), time(23, 59, 59), tzinfo=local_zone)
    return _timestamp(now), _timestamp(local_end)


def _new_code(conn: sqlite3.Connection) -> str:
    for _ in range(20):
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        code = f"HJ-{raw[:4]}-{raw[4:]}"
        if not conn.execute("SELECT 1 FROM quiz_reward_codes WHERE code=?", (code,)).fetchone():
            return code
    raise RuntimeError("reward_code_generation_failed")


def issue_reward(
    conn: sqlite3.Connection,
    *,
    client_id: int,
    campaign: sqlite3.Row | dict[str, Any],
    submission_id: int,
    timezone_name: str,
    campaign_version: int = 1,
) -> sqlite3.Row | None:
    preference_code = campaign["bonus_preference_code"]
    amount = int(campaign["bonus_amount"] or 0)
    if not preference_code or amount < 1:
        return None
    existing = conn.execute(
        "SELECT * FROM quiz_reward_codes WHERE client_id=? AND campaign_code=? AND campaign_version=? AND reward_kind='quiz' ORDER BY id DESC LIMIT 1",
        (client_id, campaign["code"], campaign_version),
    ).fetchone()
    if existing:
        return existing
    code = _new_code(conn)
    valid_from, valid_until = reward_period(campaign, timezone_name)
    cursor = conn.execute(
        """
        INSERT INTO quiz_reward_codes(
            code, client_id, campaign_code, campaign_version, submission_id, preference_code, amount, valid_from, valid_until
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (code, client_id, campaign["code"], campaign_version, submission_id, preference_code, amount, valid_from, valid_until),
    )
    reward_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO quiz_reward_events(reward_id, code, client_id, campaign_code, action) VALUES (?, ?, ?, ?, 'issued')",
        (reward_id, code, client_id, campaign["code"]),
    )
    return conn.execute("SELECT * FROM quiz_reward_codes WHERE id=?", (reward_id,)).fetchone()


def issue_referral_reward(
    conn: sqlite3.Connection,
    *,
    client_id: int,
    campaign: sqlite3.Row | dict[str, Any],
    submission_id: int,
    milestone: int,
    timezone_name: str,
) -> sqlite3.Row | None:
    preference_code = campaign["referral_preference_code"]
    amount = int(campaign["referral_amount"] or 0)
    if not campaign["referral_enabled"] or not preference_code or amount < 1:
        return None
    existing = conn.execute(
        """
        SELECT * FROM quiz_reward_codes
        WHERE client_id=? AND campaign_code=? AND reward_kind='referral' AND referral_milestone=?
        """,
        (client_id, campaign["code"], milestone),
    ).fetchone()
    if existing:
        return existing
    code = _new_code(conn)
    valid_from, valid_until = reward_period(campaign, timezone_name)
    cursor = conn.execute(
        """
        INSERT INTO quiz_reward_codes(
            code, client_id, campaign_code, submission_id, reward_kind, referral_milestone,
            preference_code, amount, valid_from, valid_until
        ) VALUES (?, ?, ?, ?, 'referral', ?, ?, ?, ?, ?)
        """,
        (code, client_id, campaign["code"], submission_id, milestone, preference_code, amount, valid_from, valid_until),
    )
    reward_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO quiz_reward_events(reward_id, code, client_id, campaign_code, action, details)
        VALUES (?, ?, ?, ?, 'referral_issued', ?)
        """,
        (reward_id, code, client_id, campaign["code"], json.dumps({"milestone": milestone}, ensure_ascii=False)),
    )
    return conn.execute("SELECT * FROM quiz_reward_codes WHERE id=?", (reward_id,)).fetchone()


def redeem_reward(
    conn: sqlite3.Connection,
    *,
    code: str,
    admin_id: int,
    admin_name: str,
) -> sqlite3.Row:
    normalized = str(code or "").strip().upper()
    reward = conn.execute("SELECT * FROM quiz_reward_codes WHERE code=?", (normalized,)).fetchone()
    if not reward:
        raise ValueError("reward_not_found")
    if reward["status"] != "issued":
        raise ValueError(f"reward_{reward['status']}")
    now = datetime.now(timezone.utc)
    valid_from = datetime.fromisoformat(reward["valid_from"]) if reward["valid_from"] else None
    valid_until = datetime.fromisoformat(reward["valid_until"]) if reward["valid_until"] else None
    if valid_from and now < valid_from:
        raise ValueError("reward_not_started")
    if valid_until and now > valid_until:
        conn.execute("UPDATE quiz_reward_codes SET status='expired' WHERE id=?", (reward["id"],))
        conn.execute(
            "INSERT INTO quiz_reward_events(reward_id, code, client_id, campaign_code, action, admin_name) VALUES (?, ?, ?, ?, 'expired', ?)",
            (reward["id"], reward["code"], reward["client_id"], reward["campaign_code"], admin_name),
        )
        raise ValueError("reward_expired")
    preference = conn.execute(
        "SELECT kind, is_active FROM preference_types WHERE code=?", (reward["preference_code"],)
    ).fetchone()
    if not preference or preference["kind"] != "counter" or not preference["is_active"]:
        raise ValueError("reward_preference_unavailable")
    ensure_preferences(conn, int(reward["client_id"]))
    change_counter(
        conn,
        client_id=int(reward["client_id"]),
        code=reward["preference_code"],
        delta=int(reward["amount"]),
        reason="quiz_reward_redeemed",
        comment=f"Код {reward['code']}; campaign={reward['campaign_code']}",
        admin_name=admin_name,
    )
    conn.execute(
        "UPDATE quiz_reward_codes SET status='used', used_at=CURRENT_TIMESTAMP, used_by_admin_id=? WHERE id=?",
        (admin_id, reward["id"]),
    )
    conn.execute(
        """
        INSERT INTO quiz_reward_events(reward_id, code, client_id, campaign_code, action, admin_name, details)
        VALUES (?, ?, ?, ?, 'used', ?, ?)
        """,
        (reward["id"], reward["code"], reward["client_id"], reward["campaign_code"], admin_name,
         json.dumps({"preference_code": reward["preference_code"], "amount": reward["amount"]}, ensure_ascii=False)),
    )
    return conn.execute("SELECT * FROM quiz_reward_codes WHERE id=?", (reward["id"],)).fetchone()


def render_campaign_text(template: str, values: dict[str, Any]) -> str:
    result = str(template or "")
    for key, value in values.items():
        result = result.replace("{" + key + "}", str(value))
    return result
