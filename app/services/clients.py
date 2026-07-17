from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from app.services.phone import full_phone, normalize_phone


CLIENT_FIELDS = (
    "app_user_id",
    "telegram_id",
    "referrer_app_user_id",
    "nickname",
    "username",
    "first_name",
    "phone_raw",
)


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in {"", "none", "nan", "null"}:
        return None
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def upsert_client(conn: sqlite3.Connection, values: Mapping[str, Any], source: str = "hijack_app") -> tuple[int, str]:
    data = {field: clean(values.get(field)) for field in CLIENT_FIELDS}
    data["phone_local"] = normalize_phone(data["phone_raw"])
    data["phone_full"] = full_phone(data["phone_local"])

    by_phone = None
    by_app = None
    if data["phone_local"]:
        by_phone = conn.execute("SELECT id FROM clients WHERE phone_local = ?", (data["phone_local"],)).fetchone()
    if data["app_user_id"]:
        by_app = conn.execute("SELECT id FROM clients WHERE app_user_id = ?", (data["app_user_id"],)).fetchone()
    if by_phone and by_app and by_phone["id"] != by_app["id"]:
        raise ValueError("phone_and_app_id_match_different_clients")

    existing = by_phone or by_app
    if existing:
        client_id = int(existing["id"])
        assignments = ", ".join(f"{field} = COALESCE(?, {field})" for field in CLIENT_FIELDS)
        params = [data[field] for field in CLIENT_FIELDS]
        conn.execute(
            f"UPDATE clients SET {assignments}, phone_full = COALESCE(?, phone_full), "
            "phone_local = COALESCE(?, phone_local), source = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (*params, data["phone_full"], data["phone_local"], source, client_id),
        )
        return client_id, "updated"

    columns = (*CLIENT_FIELDS, "phone_full", "phone_local", "source")
    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO clients ({', '.join(columns)}) VALUES ({placeholders})",
        tuple(data.get(column) for column in columns[:-1]) + (source,),
    )
    return int(cursor.lastrowid), "inserted"


def ensure_preferences(conn: sqlite3.Connection, client_id: int) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO client_preferences(client_id, preference_type_id)
        SELECT ?, id FROM preference_types WHERE is_active = 1
        """,
        (client_id,),
    )

