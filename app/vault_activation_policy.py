from __future__ import annotations

import sqlite3
from datetime import datetime
from types import ModuleType
from typing import Any

import app.services.vault as vault


CARD_ACTIVATION_MINUTES = 15


def _auto_redeem_expired_activation(
    conn: sqlite3.Connection,
    reward: sqlite3.Row,
    *,
    current: str,
) -> bool:
    cursor = conn.execute(
        """
        UPDATE vault_member_rewards
        SET status='redeemed',
            redeemed_at=COALESCE(activation_expires_at, ?),
            redeemed_by_admin_id=NULL
        WHERE id=? AND status='active' AND activation_code=?
        """,
        (current, int(reward["id"]), reward["activation_code"]),
    )
    if cursor.rowcount != 1:
        return False
    updated = conn.execute(
        "SELECT * FROM vault_member_rewards WHERE id=?",
        (int(reward["id"]),),
    ).fetchone()
    vault._insert_event(
        conn,
        reward=updated,
        action="activation_timeout_redeemed",
        admin_name="system",
        details={
            "expired_at": reward["activation_expires_at"] or current,
            "reason": "activation_timeout",
        },
    )
    return True


def expire_activations(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    client_id: int | None = None,
) -> int:
    current = vault._timestamp(vault._utc_now(now))
    clauses = [
        "status='active'",
        "activation_code IS NOT NULL",
        "activation_expires_at IS NOT NULL",
        "activation_expires_at<=?",
    ]
    params: list[Any] = [current]
    if client_id is not None:
        clauses.append("client_id=?")
        params.append(int(client_id))
    rows = conn.execute(
        f"SELECT * FROM vault_member_rewards WHERE {' AND '.join(clauses)}",
        params,
    ).fetchall()
    consumed = 0
    for reward in rows:
        if _auto_redeem_expired_activation(conn, reward, current=current):
            consumed += 1
    return consumed


def activate_reward(
    conn: sqlite3.Connection,
    *,
    reward_id: int,
    client_id: int,
    activation_minutes: int = CARD_ACTIVATION_MINUTES,
    now: datetime | None = None,
) -> sqlite3.Row:
    del activation_minutes
    expire_activations(conn, now=now, client_id=client_id)
    return vault.activate_reward(
        conn,
        reward_id=reward_id,
        client_id=client_id,
        activation_minutes=CARD_ACTIVATION_MINUTES,
        now=now,
    )


def redeem_reward(
    conn: sqlite3.Connection,
    *,
    code: str,
    admin_id: int,
    admin_name: str,
    now: datetime | None = None,
) -> sqlite3.Row:
    expire_activations(conn, now=now)
    return vault.redeem_reward(
        conn,
        code=code,
        admin_id=admin_id,
        admin_name=admin_name,
        now=now,
    )


def apply_vault_activation_policy(main_impl: ModuleType) -> None:
    if getattr(main_impl, "_hj_irreversible_activation_policy", False):
        return
    main_impl.activate_vault_reward = activate_reward
    main_impl.expire_vault_activations = expire_activations
    main_impl.redeem_vault_reward = redeem_reward
    main_impl._hj_irreversible_activation_policy = True


__all__ = [
    "CARD_ACTIVATION_MINUTES",
    "activate_reward",
    "apply_vault_activation_policy",
    "expire_activations",
    "redeem_reward",
]
