from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.member_accounts import jackcoin_balance
from app.services.reward_animations import validate_animation_key


CATALOG_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,49}$")
CARD_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
ACTIVATION_CODE_LENGTH = 4
DEFAULT_ACTIVATION_MINUTES = 10
CATALOG_CATEGORIES = frozenset(
    {"club", "drink", "entry", "card", "profile", "protection"}
)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _utc_now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_catalog_code(value: str) -> str:
    code = str(value or "").strip().lower()
    if not CATALOG_CODE_RE.fullmatch(code):
        raise ValueError("invalid_catalog_code")
    return code


def validate_catalog_values(
    *,
    title: str,
    category: str,
    price_jc: int,
    validity_days: int,
    inventory_total: int | None,
    position: int,
) -> tuple[str, str, int, int, int | None, int]:
    clean_title = str(title or "").strip()
    clean_category = str(category or "").strip().lower()
    if not 2 <= len(clean_title) <= 100:
        raise ValueError("invalid_title")
    if clean_category not in CATALOG_CATEGORIES:
        raise ValueError("invalid_category")
    if not 0 <= int(price_jc) <= 1_000_000:
        raise ValueError("invalid_price")
    if not 0 <= int(validity_days) <= 3_650:
        raise ValueError("invalid_validity")
    if inventory_total is not None and not 0 <= int(inventory_total) <= 1_000_000:
        raise ValueError("invalid_inventory")
    if not 0 <= int(position) <= 9_999:
        raise ValueError("invalid_position")
    return (
        clean_title,
        clean_category,
        int(price_jc),
        int(validity_days),
        int(inventory_total) if inventory_total is not None else None,
        int(position),
    )


def create_catalog_reward(
    conn: sqlite3.Connection,
    *,
    code: str,
    title: str,
    description: str,
    category: str,
    price_jc: int,
    validity_days: int,
    inventory_total: int | None,
    redeem_instructions: str,
    position: int,
    admin_id: int,
    animation_key: str | None = None,
    animation_path: str | None = None,
    animation_mime: str | None = None,
) -> sqlite3.Row:
    normalized_code = normalize_catalog_code(code)
    (
        clean_title,
        clean_category,
        clean_price,
        clean_validity,
        clean_inventory,
        clean_position,
    ) = validate_catalog_values(
        title=title,
        category=category,
        price_jc=price_jc,
        validity_days=validity_days,
        inventory_total=inventory_total,
        position=position,
    )
    clean_animation_key = validate_animation_key(animation_key)
    clean_animation_path = str(animation_path or "").strip() or None
    clean_animation_mime = str(animation_mime or "").strip() or None
    if clean_animation_key:
        clean_animation_path = None
        clean_animation_mime = "application/json"
    elif clean_animation_path and not clean_animation_path.startswith("/reward-media/"):
        raise ValueError("invalid_animation_file")
    try:
        cursor = conn.execute(
            """
            INSERT INTO vault_catalog_rewards(
                code, title, description, category, price_jc, validity_days,
                inventory_total, redeem_instructions, animation_key,
                animation_path, animation_mime, position, created_by_admin_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_code,
                clean_title,
                str(description or "").strip()[:1_000],
                clean_category,
                clean_price,
                clean_validity,
                clean_inventory,
                str(redeem_instructions or "").strip()[:1_000],
                clean_animation_key,
                clean_animation_path,
                clean_animation_mime,
                clean_position,
                admin_id,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("catalog_code_exists") from exc
    return conn.execute(
        "SELECT * FROM vault_catalog_rewards WHERE id=?", (cursor.lastrowid,)
    ).fetchone()


def update_catalog_reward(
    conn: sqlite3.Connection,
    *,
    reward_id: int,
    title: str,
    description: str,
    category: str,
    price_jc: int,
    validity_days: int,
    inventory_total: int | None,
    redeem_instructions: str,
    position: int,
    is_active: bool,
    animation_key: str | None = None,
    animation_path: str | None = None,
    animation_mime: str | None = None,
) -> sqlite3.Row:
    current = conn.execute(
        "SELECT * FROM vault_catalog_rewards WHERE id=?", (reward_id,)
    ).fetchone()
    if not current:
        raise ValueError("catalog_reward_not_found")
    (
        clean_title,
        clean_category,
        clean_price,
        clean_validity,
        clean_inventory,
        clean_position,
    ) = validate_catalog_values(
        title=title,
        category=category,
        price_jc=price_jc,
        validity_days=validity_days,
        inventory_total=inventory_total,
        position=position,
    )
    allocated = catalog_inventory_used(conn, reward_id)
    if clean_inventory is not None and clean_inventory < allocated:
        raise ValueError("inventory_below_allocated")
    clean_animation_key = validate_animation_key(animation_key)
    clean_animation_path = str(animation_path or "").strip() or None
    clean_animation_mime = str(animation_mime or "").strip() or None
    if clean_animation_key:
        clean_animation_path = None
        clean_animation_mime = "application/json"
    elif clean_animation_path and not clean_animation_path.startswith("/reward-media/"):
        raise ValueError("invalid_animation_file")
    conn.execute(
        """
        UPDATE vault_catalog_rewards
        SET title=?, description=?, category=?, price_jc=?, validity_days=?,
            inventory_total=?, redeem_instructions=?, animation_key=?,
            animation_path=?, animation_mime=?, position=?, is_active=?,
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (
            clean_title,
            str(description or "").strip()[:1_000],
            clean_category,
            clean_price,
            clean_validity,
            clean_inventory,
            str(redeem_instructions or "").strip()[:1_000],
            clean_animation_key,
            clean_animation_path,
            clean_animation_mime,
            clean_position,
            int(is_active),
            reward_id,
        ),
    )
    return conn.execute(
        "SELECT * FROM vault_catalog_rewards WHERE id=?", (reward_id,)
    ).fetchone()


def catalog_inventory_used(conn: sqlite3.Connection, catalog_reward_id: int) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*) FROM vault_member_rewards
            WHERE catalog_reward_id=? AND status<>'cancelled'
            """,
            (catalog_reward_id,),
        ).fetchone()[0]
    )


def _new_card_code(conn: sqlite3.Connection) -> str:
    for _ in range(20):
        raw = "".join(secrets.choice(CARD_CODE_ALPHABET) for _ in range(8))
        code = f"JC-{raw[:4]}-{raw[4:]}"
        if not conn.execute(
            "SELECT 1 FROM vault_member_rewards WHERE code=?", (code,)
        ).fetchone():
            return code
    raise RuntimeError("vault_code_generation_failed")


def _new_activation_code(conn: sqlite3.Connection) -> str:
    for _ in range(100):
        code = f"{secrets.randbelow(10 ** ACTIVATION_CODE_LENGTH):0{ACTIVATION_CODE_LENGTH}d}"
        if not conn.execute(
            """
            SELECT 1 FROM vault_member_rewards
            WHERE activation_code=? AND status='active'
            """,
            (code,),
        ).fetchone():
            return code
    raise RuntimeError("vault_activation_code_generation_failed")


def purchase_token(
    secret_key: str,
    *,
    account_id: int,
    catalog_reward_id: int,
    price_jc: int = 0,
    nonce: str | None = None,
) -> str:
    clean_nonce = nonce or secrets.token_urlsafe(18)
    message = (
        f"vault-purchase:{account_id}:{catalog_reward_id}:"
        f"{int(price_jc)}:{clean_nonce}"
    )
    signature = hmac.new(
        secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{clean_nonce}.{signature}"


def valid_purchase_token(
    secret_key: str,
    token: str,
    *,
    account_id: int,
    catalog_reward_id: int,
    price_jc: int = 0,
) -> bool:
    if not 1 <= len(str(token or "")) <= 256:
        return False
    try:
        nonce, signature = str(token or "").rsplit(".", 1)
    except ValueError:
        return False
    if len(nonce) > 128 or len(signature) != 64:
        return False
    expected = purchase_token(
        secret_key,
        account_id=account_id,
        catalog_reward_id=catalog_reward_id,
        price_jc=price_jc,
        nonce=nonce,
    ).rsplit(".", 1)[1]
    return bool(nonce) and hmac.compare_digest(expected, signature)


def _insert_event(
    conn: sqlite3.Connection,
    *,
    reward: sqlite3.Row,
    action: str,
    admin_id: int | None = None,
    admin_name: str = "system",
    details: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO vault_reward_events(
            member_reward_id, catalog_reward_id, client_id, code, action,
            admin_id, admin_name, details
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            reward["id"],
            reward["catalog_reward_id"],
            reward["client_id"],
            reward["code"],
            action,
            admin_id,
            admin_name,
            json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
        ),
    )


def issue_reward(
    conn: sqlite3.Connection,
    *,
    client_id: int,
    catalog_reward_id: int,
    source_type: str,
    source_id: str | None,
    idempotency_key: str,
    price_paid_jc: int = 0,
    enforce_active: bool = True,
    now: datetime | None = None,
) -> sqlite3.Row:
    existing = conn.execute(
        "SELECT * FROM vault_member_rewards WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if existing:
        return existing
    if source_type not in {"purchase", "final_prize", "admin"}:
        raise ValueError("invalid_reward_source")
    catalog = conn.execute(
        "SELECT * FROM vault_catalog_rewards WHERE id=?", (catalog_reward_id,)
    ).fetchone()
    if not catalog:
        raise ValueError("catalog_reward_not_found")
    if enforce_active and not catalog["is_active"]:
        raise ValueError("catalog_reward_inactive")
    allocated = catalog_inventory_used(conn, catalog_reward_id)
    inventory_total = catalog["inventory_total"]
    if inventory_total is not None and allocated >= int(inventory_total):
        raise ValueError("catalog_reward_sold_out")
    issued_at = _utc_now(now)
    validity_days = max(0, int(catalog["validity_days"] or 0))
    valid_until = (
        _timestamp(issued_at + timedelta(days=validity_days))
        if validity_days
        else None
    )
    cursor = conn.execute(
        """
        INSERT INTO vault_member_rewards(
            code, catalog_reward_id, client_id, source_type, source_id,
            price_paid_jc, valid_from, valid_until, idempotency_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _new_card_code(conn),
            catalog_reward_id,
            client_id,
            source_type,
            source_id,
            max(0, int(price_paid_jc)),
            _timestamp(issued_at),
            valid_until,
            idempotency_key,
        ),
    )
    reward = conn.execute(
        "SELECT * FROM vault_member_rewards WHERE id=?", (cursor.lastrowid,)
    ).fetchone()
    _insert_event(
        conn,
        reward=reward,
        action="issued" if source_type == "admin" else f"{source_type}_issued",
        details={"source_id": source_id, "price_paid_jc": int(price_paid_jc)},
    )
    return reward


def purchase_reward(
    conn: sqlite3.Connection,
    *,
    client_id: int,
    catalog_reward_id: int,
    purchase_id: str,
    expected_price_jc: int | None = None,
    now: datetime | None = None,
) -> sqlite3.Row:
    idempotency_key = f"vault:purchase:{purchase_id}"
    existing = conn.execute(
        "SELECT * FROM vault_member_rewards WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if existing:
        if int(existing["client_id"]) != int(client_id):
            raise ValueError("invalid_purchase_token")
        return existing
    catalog = conn.execute(
        "SELECT * FROM vault_catalog_rewards WHERE id=?", (catalog_reward_id,)
    ).fetchone()
    if not catalog:
        raise ValueError("catalog_reward_not_found")
    price = int(catalog["price_jc"] or 0)
    if expected_price_jc is not None and price != int(expected_price_jc):
        raise ValueError("catalog_reward_price_changed")
    if jackcoin_balance(conn, client_id) < price:
        raise ValueError("insufficient_jackcoin")
    reward = issue_reward(
        conn,
        client_id=client_id,
        catalog_reward_id=catalog_reward_id,
        source_type="purchase",
        source_id=purchase_id,
        idempotency_key=idempotency_key,
        price_paid_jc=price,
        enforce_active=True,
        now=now,
    )
    if price:
        conn.execute(
            """
            INSERT INTO jackcoin_ledger(
                client_id, amount, operation_type, source_type, source_id,
                idempotency_key, comment
            ) VALUES (?, ?, 'vault_purchase', 'vault_reward', ?, ?, ?)
            """,
            (
                client_id,
                -price,
                str(reward["id"]),
                f"vault:purchase-ledger:{purchase_id}",
                f"THE VAULT: {catalog['title']}",
            ),
        )
    return reward


def activate_reward(
    conn: sqlite3.Connection,
    *,
    reward_id: int,
    client_id: int,
    activation_minutes: int = DEFAULT_ACTIVATION_MINUTES,
    now: datetime | None = None,
) -> sqlite3.Row:
    current = _utc_now(now)
    expire_activations(conn, now=current)
    reward = conn.execute(
        """
        SELECT * FROM vault_member_rewards
        WHERE id=? AND client_id=?
        """,
        (reward_id, client_id),
    ).fetchone()
    if not reward:
        raise ValueError("vault_reward_not_found")
    if reward["status"] != "active":
        raise ValueError(f"vault_reward_{reward['status']}")
    valid_from = datetime.fromisoformat(str(reward["valid_from"]))
    valid_until = (
        datetime.fromisoformat(str(reward["valid_until"]))
        if reward["valid_until"]
        else None
    )
    if current < _utc_now(valid_from):
        raise ValueError("vault_reward_not_started")
    if valid_until and current > _utc_now(valid_until):
        raise ValueError("vault_reward_expired")
    activation_expires_at = (
        datetime.fromisoformat(str(reward["activation_expires_at"]))
        if reward["activation_expires_at"]
        else None
    )
    if (
        reward["activated_at"]
        and reward["activation_code"]
        and activation_expires_at
        and current < _utc_now(activation_expires_at)
    ):
        return reward
    if reward["activation_code"] or reward["activated_at"]:
        conn.execute(
            """
            UPDATE vault_member_rewards
            SET activation_code=NULL, activated_at=NULL,
                activation_expires_at=NULL
            WHERE id=? AND client_id=? AND status='active'
            """,
            (reward_id, client_id),
        )
    activation_code = _new_activation_code(conn)
    activation_expires_at = current + timedelta(
        minutes=max(1, int(activation_minutes))
    )
    conn.execute(
        """
        UPDATE vault_member_rewards
        SET activation_code=?, activated_at=?, activation_expires_at=?
        WHERE id=? AND client_id=? AND status='active'
          AND activation_code IS NULL
        """,
        (
            activation_code,
            _timestamp(current),
            _timestamp(activation_expires_at),
            reward_id,
            client_id,
        ),
    )
    updated = conn.execute(
        "SELECT * FROM vault_member_rewards WHERE id=?", (reward_id,)
    ).fetchone()
    if (
        not updated
        or not updated["activated_at"]
        or not updated["activation_code"]
        or not updated["activation_expires_at"]
    ):
        raise RuntimeError("vault_reward_activation_failed")
    _insert_event(
        conn,
        reward=updated,
        action="activated",
        details={"expires_at": updated["activation_expires_at"]},
    )
    return updated


def expire_activations(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    client_id: int | None = None,
) -> int:
    current = _timestamp(_utc_now(now))
    clauses = [
        "status='active'",
        "activation_code IS NOT NULL",
        "activation_expires_at IS NOT NULL",
        "activation_expires_at<=?",
    ]
    params: list[Any] = [current]
    if client_id is not None:
        clauses.append("client_id=?")
        params.append(client_id)
    rows = conn.execute(
        f"SELECT * FROM vault_member_rewards WHERE {' AND '.join(clauses)}",
        params,
    ).fetchall()
    for reward in rows:
        conn.execute(
            """
            UPDATE vault_member_rewards
            SET activation_code=NULL, activated_at=NULL,
                activation_expires_at=NULL
            WHERE id=? AND status='active' AND activation_code=?
            """,
            (reward["id"], reward["activation_code"]),
        )
        _insert_event(
            conn,
            reward=reward,
            action="activation_expired",
            details={"expired_at": current},
        )
    return len(rows)


def expire_rewards(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    client_id: int | None = None,
) -> int:
    current = _timestamp(_utc_now(now))
    clauses = ["status='active'", "valid_until IS NOT NULL", "valid_until<?"]
    params: list[Any] = [current]
    if client_id is not None:
        clauses.append("client_id=?")
        params.append(client_id)
    rows = conn.execute(
        f"SELECT * FROM vault_member_rewards WHERE {' AND '.join(clauses)}",
        params,
    ).fetchall()
    for reward in rows:
        conn.execute(
            "UPDATE vault_member_rewards SET status='expired' WHERE id=? AND status='active'",
            (reward["id"],),
        )
        _insert_event(conn, reward=reward, action="expired")
    return len(rows)


def redeem_reward(
    conn: sqlite3.Connection,
    *,
    code: str,
    admin_id: int,
    admin_name: str,
    now: datetime | None = None,
) -> sqlite3.Row:
    normalized = str(code or "").strip().upper()
    reward = conn.execute(
        """
        SELECT * FROM vault_member_rewards
        WHERE code=? OR activation_code=?
        ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END, id DESC
        LIMIT 1
        """,
        (normalized, normalized),
    ).fetchone()
    if not reward:
        raise ValueError("vault_reward_not_found")
    if reward["status"] != "active":
        raise ValueError(f"vault_reward_{reward['status']}")
    if not reward["activated_at"] or not reward["activation_code"]:
        raise ValueError("vault_reward_not_activated")
    current = _utc_now(now)
    activation_expires_at = (
        datetime.fromisoformat(str(reward["activation_expires_at"]))
        if reward["activation_expires_at"]
        else None
    )
    if not activation_expires_at or current >= _utc_now(activation_expires_at):
        conn.execute(
            """
            UPDATE vault_member_rewards
            SET activation_code=NULL, activated_at=NULL,
                activation_expires_at=NULL
            WHERE id=? AND status='active'
            """,
            (reward["id"],),
        )
        _insert_event(
            conn,
            reward=reward,
            action="activation_expired",
            details={"expired_at": _timestamp(current)},
        )
        raise ValueError("vault_reward_activation_expired")
    valid_from = datetime.fromisoformat(str(reward["valid_from"]))
    valid_until = (
        datetime.fromisoformat(str(reward["valid_until"]))
        if reward["valid_until"]
        else None
    )
    if current < _utc_now(valid_from):
        raise ValueError("vault_reward_not_started")
    if valid_until and current > _utc_now(valid_until):
        raise ValueError("vault_reward_expired")
    conn.execute(
        """
        UPDATE vault_member_rewards
        SET status='redeemed', redeemed_at=?, redeemed_by_admin_id=?
        WHERE id=? AND status='active'
        """,
        (_timestamp(current), admin_id, reward["id"]),
    )
    updated = conn.execute(
        "SELECT * FROM vault_member_rewards WHERE id=?", (reward["id"],)
    ).fetchone()
    _insert_event(
        conn,
        reward=updated,
        action="redeemed",
        admin_id=admin_id,
        admin_name=admin_name,
    )
    return updated


def cancel_reward(
    conn: sqlite3.Connection,
    *,
    reward_id: int,
    admin_id: int,
    admin_name: str,
) -> sqlite3.Row:
    reward = conn.execute(
        "SELECT * FROM vault_member_rewards WHERE id=?", (reward_id,)
    ).fetchone()
    if not reward:
        raise ValueError("vault_reward_not_found")
    if reward["status"] != "active":
        raise ValueError(f"vault_reward_{reward['status']}")
    conn.execute(
        """
        UPDATE vault_member_rewards
        SET status='cancelled', cancelled_at=CURRENT_TIMESTAMP,
            cancelled_by_admin_id=?
        WHERE id=? AND status='active'
        """,
        (admin_id, reward_id),
    )
    price = int(reward["price_paid_jc"] or 0)
    if price:
        conn.execute(
            """
            INSERT OR IGNORE INTO jackcoin_ledger(
                client_id, amount, operation_type, source_type, source_id,
                idempotency_key, comment, created_by_admin_id
            ) VALUES (?, ?, 'vault_refund', 'vault_reward', ?, ?, ?, ?)
            """,
            (
                reward["client_id"],
                price,
                str(reward_id),
                f"vault:refund:{reward_id}",
                f"Возврат за отменённую карту {reward['code']}",
                admin_id,
            ),
        )
    updated = conn.execute(
        "SELECT * FROM vault_member_rewards WHERE id=?", (reward_id,)
    ).fetchone()
    _insert_event(
        conn,
        reward=updated,
        action="cancelled",
        admin_id=admin_id,
        admin_name=admin_name,
        details={"jackcoin_refunded": price},
    )
    return updated


def attach_final_table_reward(
    conn: sqlite3.Connection, *, final_table_id: int, now: datetime | None = None
) -> sqlite3.Row | dict[str, Any] | None:
    from app.services.daily_414_final import list_final_winners, split_jackcoin_amounts

    table = conn.execute(
        "SELECT * FROM daily_414_final_tables WHERE id=?",
        (final_table_id,),
    ).fetchone()
    if not table or table["status"] != "completed":
        return None
    outcome = str(table["outcome"] or "")
    prize_resolution = str(table["prize_resolution"] or "")
    if outcome == "no_winner" or prize_resolution == "none":
        return None

    winners = list_final_winners(conn, final_table_id=final_table_id)
    if not winners and table["winner_submission_id"]:
        legacy = conn.execute(
            """
            SELECT qs.id AS submission_id, qs.client_id, qs.completion_time_ms
                   AS main_completion_time_ms, 0 AS final_response_time_ms,
                   qs.id AS id
            FROM quiz_submissions qs
            WHERE qs.id=?
            """,
            (table["winner_submission_id"],),
        ).fetchone()
        winners = [legacy] if legacy else []
    if not winners:
        return None

    prize_type = str(table["prize_type"] or "none")
    if prize_type == "none" and table["prize_catalog_reward_id"]:
        prize_type = "reward_card"

    if prize_type == "jackcoin":
        amount = max(0, int(table["prize_jackcoin_amount"] or 0))
        if prize_resolution == "awarded" or table["winner_jackcoin_awarded"]:
            shares = conn.execute(
                """
                SELECT client_id, amount FROM jackcoin_ledger
                WHERE source_type='final_prize' AND source_id=?
                ORDER BY id
                """,
                (str(final_table_id),),
            ).fetchall()
            return {
                "kind": "jackcoin",
                "amount": int(table["winner_jackcoin_awarded"] or amount),
                "shares": [
                    {"client_id": int(row["client_id"]), "amount": int(row["amount"])}
                    for row in shares
                ],
                "winner_client_ids": [int(row["client_id"]) for row in winners],
            }
        if amount <= 0:
            return None
        portions = split_jackcoin_amounts(amount, len(winners))
        try:
            for winner, share in zip(winners, portions):
                if share <= 0:
                    continue
                # Single-winner keeps the legacy idempotency key for older rows.
                key = (
                    f"jackcoin:final-table:{final_table_id}"
                    if len(winners) == 1
                    else (
                        f"jackcoin:final-table:{final_table_id}"
                        f":client:{int(winner['client_id'])}"
                    )
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO jackcoin_ledger(
                        client_id, amount, operation_type, source_type, source_id,
                        idempotency_key, comment
                    ) VALUES (?, ?, 'earn', 'final_prize', ?, ?, ?)
                    """,
                    (
                        int(winner["client_id"]),
                        share,
                        str(final_table_id),
                        key,
                        (
                            f"Главный приз финального стола: {share} JACKCOIN"
                            + (
                                f" (доля из {amount})"
                                if len(winners) > 1
                                else ""
                            )
                        ),
                    ),
                )
            awarded_total = int(
                conn.execute(
                    """
                    SELECT COALESCE(SUM(amount), 0) FROM jackcoin_ledger
                    WHERE source_type='final_prize' AND source_id=?
                    """,
                    (str(final_table_id),),
                ).fetchone()[0]
            )
            if awarded_total <= 0:
                raise ValueError("final_prize_jackcoin_not_recorded")
        except (sqlite3.Error, ValueError) as exc:
            conn.execute(
                """
                UPDATE daily_414_final_tables
                SET winner_reward_error=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (str(exc), final_table_id),
            )
            return None
        conn.execute(
            """
            UPDATE daily_414_final_tables
            SET winner_jackcoin_awarded=?, winner_reward_error=NULL,
                prize_resolution='awarded', updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (awarded_total, final_table_id),
        )
        shares = conn.execute(
            """
            SELECT client_id, amount FROM jackcoin_ledger
            WHERE source_type='final_prize' AND source_id=?
            ORDER BY id
            """,
            (str(final_table_id),),
        ).fetchall()
        return {
            "kind": "jackcoin",
            "amount": awarded_total,
            "shares": [
                {"client_id": int(row["client_id"]), "amount": int(row["amount"])}
                for row in shares
            ],
            "winner_client_ids": [int(row["client_id"]) for row in winners],
        }

    if prize_type != "reward_card":
        return None

    if prize_resolution == "manual_task":
        task = conn.execute(
            "SELECT * FROM daily_414_master_tasks WHERE final_table_id=?",
            (final_table_id,),
        ).fetchone()
        return {
            "kind": "manual_task",
            "task_id": int(task["id"]) if task else None,
            "winner_client_ids": [int(row["client_id"]) for row in winners],
        }

    if len(winners) > 1:
        payload = json.dumps(
            {
                "reason": "indivisible_card_co_winners",
                "catalog_reward_id": table["prize_catalog_reward_id"],
                "winner_client_ids": [int(row["client_id"]) for row in winners],
                "winner_submission_ids": [
                    int(row["submission_id"]) for row in winners
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        try:
            conn.execute(
                """
                INSERT INTO daily_414_master_tasks(
                    final_table_id, task_type, status, payload_json
                ) VALUES (?, 'co_winner_card_prize', 'open', ?)
                """,
                (final_table_id, payload),
            )
        except sqlite3.IntegrityError:
            pass
        task = conn.execute(
            "SELECT * FROM daily_414_master_tasks WHERE final_table_id=?",
            (final_table_id,),
        ).fetchone()
        conn.execute(
            """
            UPDATE daily_414_final_tables
            SET prize_resolution='manual_task', winner_reward_error=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (final_table_id,),
        )
        return {
            "kind": "manual_task",
            "task_id": int(task["id"]) if task else None,
            "winner_client_ids": [int(row["client_id"]) for row in winners],
        }

    if table["winner_reward_id"]:
        return conn.execute(
            "SELECT * FROM vault_member_rewards WHERE id=?",
            (table["winner_reward_id"],),
        ).fetchone()
    catalog_reward_id = table["prize_catalog_reward_id"]
    if not catalog_reward_id:
        return None
    winner_client_id = int(winners[0]["client_id"])
    try:
        reward = issue_reward(
            conn,
            client_id=winner_client_id,
            catalog_reward_id=int(catalog_reward_id),
            source_type="final_prize",
            source_id=str(final_table_id),
            idempotency_key=f"vault:final-table:{final_table_id}",
            price_paid_jc=0,
            enforce_active=False,
            now=now,
        )
    except ValueError as exc:
        conn.execute(
            """
            UPDATE daily_414_final_tables
            SET winner_reward_error=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (str(exc), final_table_id),
        )
        return None
    conn.execute(
        """
        UPDATE daily_414_final_tables
        SET winner_reward_id=?, winner_reward_error=NULL,
            prize_resolution='awarded', updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (reward["id"], final_table_id),
    )
    return reward
