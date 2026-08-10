from __future__ import annotations

from datetime import date
from pathlib import Path

from app.db import init_db, transaction
from app.hijack_rating_relink import relink_hijack_history
from app.services.hijack_rating import hijack_rating_payload, import_hijack_rating


def _client(conn, phone: str, nickname: str) -> int:
    cursor = conn.execute(
        "INSERT INTO clients(phone_raw,phone_full,phone_local,nickname,source) VALUES (?,?,?,?,?)",
        (f"+7{phone}", f"+7{phone}", phone, nickname, "test"),
    )
    return int(cursor.lastrowid)


def test_full_rating_history_relinks_when_client_appears_later(tmp_path: Path) -> None:
    db_path = tmp_path / "late-registration.sqlite3"
    init_db(db_path)

    with transaction(db_path) as conn:
        result = import_hijack_rating(
            conn,
            tournament_name="Before Registration",
            tournament_date=date(2026, 8, 9),
            source_filename="full-rating.xlsx",
            rows=[
                {
                    "source_row": 2,
                    "phone_raw": "+7 (999) 111-22-33",
                    "phone_local": "9991112233",
                    "rating_points": 125,
                    "kills": 4,
                }
            ],
            admin_id=None,
        )
        assert result["matched_rows"] == 0
        assert result["unmatched_rows"] == 1
        stored = conn.execute(
            "SELECT client_id,phone_local,rating_points,kills FROM hi_jack_rating_entries"
        ).fetchone()
        assert stored["client_id"] is None
        assert stored["phone_local"] == "9991112233"
        assert stored["rating_points"] == 125
        assert stored["kills"] == 4

        client_id = _client(conn, "9991112233", "Late Player")
        linked = relink_hijack_history(conn, client_id=client_id)
        assert linked == 1

        stored = conn.execute(
            "SELECT client_id FROM hi_jack_rating_entries"
        ).fetchone()
        assert stored["client_id"] == client_id
        import_row = conn.execute(
            "SELECT matched_rows,unmatched_rows,invalid_rows FROM hi_jack_rating_imports"
        ).fetchone()
        assert import_row["matched_rows"] == 1
        assert import_row["unmatched_rows"] == 0
        assert import_row["invalid_rows"] == 0

        payload = hijack_rating_payload(
            conn,
            client_id=client_id,
            today=date(2026, 8, 10),
        )
        assert payload["year"]["me"]["points"] == 125
        assert payload["month"]["me"]["kills"] == 4
        assert payload["latest"]["me"]["points"] == 125
