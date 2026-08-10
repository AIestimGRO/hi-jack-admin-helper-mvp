from __future__ import annotations

from datetime import date
from pathlib import Path

from app.db import init_db, transaction
from app.hijack_rating_baseline import (
    _relink_baseline,
    ensure_baseline_schema,
    hijack_member_metrics_v2,
    hijack_rating_payload_v2,
)
from app.services.hijack_rating import import_hijack_rating


def _client(conn, phone: str, nickname: str) -> int:
    cursor = conn.execute(
        "INSERT INTO clients(phone_raw,phone_full,phone_local,nickname,source) VALUES (?,?,?,?,?)",
        (f"+7{phone}", f"+7{phone}", phone, nickname, "test"),
    )
    return int(cursor.lastrowid)


def test_baseline_plus_tournaments_builds_global_without_fake_tournament(tmp_path: Path) -> None:
    db_path = tmp_path / "baseline.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        client_id = _client(conn, "9991112233", "Alpha")
        ensure_baseline_schema(conn)
        conn.execute(
            "INSERT INTO hi_jack_rating_baseline(id,source_filename,total_rows,matched_rows) VALUES (1,'base.xlsx',1,1)"
        )
        conn.execute(
            """
            INSERT INTO hi_jack_rating_baseline_entries(
                client_id,phone_local,phone_raw,rating_points,kills,source_row
            ) VALUES (?,?,?,?,?,?)
            """,
            (client_id, "9991112233", "+79991112233", 1000, 20, 2),
        )
        import_hijack_rating(
            conn,
            tournament_name="Sunday",
            tournament_date=date.today(),
            source_filename="sunday.xlsx",
            rows=[
                {
                    "source_row": 2,
                    "phone_raw": "+79991112233",
                    "phone_local": "9991112233",
                    "rating_points": 120,
                    "kills": 3,
                }
            ],
            admin_id=None,
        )

        payload = hijack_rating_payload_v2(conn, client_id=client_id)
        metrics = hijack_member_metrics_v2(conn, client_id=client_id)

    assert payload["year"]["is_global"] is True
    assert payload["year"]["me"]["points"] == 1120
    assert payload["year"]["me"]["kills"] == 23
    assert payload["year"]["me"]["tournaments"] == 1
    assert payload["latest"]["me"]["points"] == 120
    assert metrics["hijack_global_rating"] == 1120
    assert metrics["hijack_global_kills"] == 23
    assert metrics["hijack_tournaments_played"] == 1


def test_late_registration_relinks_accumulated_baseline(tmp_path: Path) -> None:
    db_path = tmp_path / "baseline-relink.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_baseline_schema(conn)
        conn.execute(
            "INSERT INTO hi_jack_rating_baseline(id,source_filename,total_rows,unmatched_rows) VALUES (1,'base.xlsx',1,1)"
        )
        conn.execute(
            """
            INSERT INTO hi_jack_rating_baseline_entries(
                client_id,phone_local,phone_raw,rating_points,kills,source_row
            ) VALUES (NULL,?,?,?,?,?)
            """,
            ("9991112233", "+7 (999) 111-22-33", 850, 12, 2),
        )
        client_id = _client(conn, "9991112233", "Late")
        linked = _relink_baseline(conn, client_id=client_id)
        payload = hijack_rating_payload_v2(conn, client_id=client_id)
        counters = conn.execute(
            "SELECT matched_rows,unmatched_rows FROM hi_jack_rating_baseline WHERE id=1"
        ).fetchone()

    assert linked == 1
    assert payload["year"]["me"]["points"] == 850
    assert counters["matched_rows"] == 1
    assert counters["unmatched_rows"] == 0
