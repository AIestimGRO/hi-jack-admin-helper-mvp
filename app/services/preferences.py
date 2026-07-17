from __future__ import annotations

import sqlite3


def change_counter(
    conn: sqlite3.Connection,
    *,
    client_id: int,
    code: str,
    delta: int,
    reason: str,
    comment: str,
    admin_name: str,
) -> int:
    if delta == 0 or abs(delta) > 1000:
        raise ValueError("invalid_delta")
    row = conn.execute(
        """
        SELECT cp.id, cp.balance_int, pt.id AS type_id
        FROM client_preferences cp
        JOIN preference_types pt ON pt.id = cp.preference_type_id
        WHERE cp.client_id = ? AND pt.code = ? AND pt.kind = 'counter'
        """,
        (client_id, code),
    ).fetchone()
    if not row:
        raise ValueError("preference_not_found")
    old_value = int(row["balance_int"])
    new_value = old_value + delta
    if new_value < 0:
        raise ValueError("insufficient_balance")
    conn.execute(
        "UPDATE client_preferences SET balance_int = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_value, row["id"]),
    )
    conn.execute(
        """
        INSERT INTO preference_log(
            client_id, preference_type_id, operation_type, delta_int,
            old_balance_int, new_balance_int, reason, comment, admin_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            client_id,
            row["type_id"],
            "add" if delta > 0 else "spend",
            delta,
            old_value,
            new_value,
            reason,
            comment,
            admin_name,
        ),
    )
    return new_value


def set_percent(
    conn: sqlite3.Connection,
    *,
    client_id: int,
    code: str,
    percent: float,
    reason: str,
    comment: str,
    admin_name: str,
) -> float:
    if percent < 0 or percent > 100:
        raise ValueError("invalid_percent")
    row = conn.execute(
        """
        SELECT cp.id, cp.percent_value, pt.id AS type_id
        FROM client_preferences cp
        JOIN preference_types pt ON pt.id = cp.preference_type_id
        WHERE cp.client_id = ? AND pt.code = ? AND pt.kind = 'percent'
        """,
        (client_id, code),
    ).fetchone()
    if not row:
        raise ValueError("preference_not_found")
    old_value = float(row["percent_value"])
    conn.execute(
        "UPDATE client_preferences SET percent_value = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (percent, row["id"]),
    )
    conn.execute(
        """
        INSERT INTO preference_log(
            client_id, preference_type_id, operation_type,
            old_percent_value, new_percent_value, reason, comment, admin_name
        ) VALUES (?, ?, 'set_percent', ?, ?, ?, ?, ?)
        """,
        (client_id, row["type_id"], old_value, percent, reason, comment, admin_name),
    )
    return percent

