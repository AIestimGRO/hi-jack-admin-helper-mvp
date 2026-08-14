from __future__ import annotations

from pathlib import Path

from app.db import init_db, transaction
from app.hijack_rating_baseline import ensure_baseline_schema
from app.hijack_rating_paging import DEFAULT_PAGE_SIZE, hijack_rating_page_payload


def test_global_rating_pages_large_leaderboard(tmp_path: Path) -> None:
    db_path = tmp_path / "rating-paging.sqlite3"
    init_db(db_path)

    with transaction(db_path) as conn:
        ensure_baseline_schema(conn)
        client_ids = []
        for index in range(450):
            phone = f"999{index:07d}"
            cursor = conn.execute(
                """
                INSERT INTO clients(phone_raw,phone_full,phone_local,nickname,source)
                VALUES (?,?,?,?,?)
                """,
                (f"+7{phone}", f"+7{phone}", phone, f"Player {index + 1}", "test"),
            )
            client_id = int(cursor.lastrowid)
            client_ids.append(client_id)
            conn.execute(
                """
                INSERT INTO hi_jack_rating_baseline_entries(
                    client_id,phone_local,phone_raw,rating_points,kills,source_row
                ) VALUES (?,?,?,?,?,?)
                """,
                (client_id, phone, f"+7{phone}", 450 - index, index % 7, index + 2),
            )

        conn.execute(
            """
            INSERT INTO hi_jack_rating_baseline(
                id,source_filename,total_rows,matched_rows,unmatched_rows,invalid_rows
            ) VALUES (1,'all.xlsx',450,450,0,0)
            """
        )

        first = hijack_rating_page_payload(
            conn,
            client_id=client_ids[-1],
            period="global",
        )
        second = hijack_rating_page_payload(
            conn,
            client_id=client_ids[-1],
            period="global",
            offset=DEFAULT_PAGE_SIZE,
        )

    assert DEFAULT_PAGE_SIZE == 25
    assert first["total"] == 450
    assert len(first["rows"]) == 25
    assert first["has_more"] is True
    assert first["rows"][0]["place"] == 1
    assert first["me"]["client_id"] == client_ids[-1]
    assert first["me"]["place"] == 450

    assert second["offset"] == 25
    assert len(second["rows"]) == 25
    assert second["rows"][0]["place"] == 26


def test_global_rating_falls_back_to_latest_legacy_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "rating-legacy-fallback.sqlite3"
    init_db(db_path)

    with transaction(db_path) as conn:
        client_id = int(
            conn.execute(
                "INSERT INTO clients(first_name,source) VALUES ('Member','test')"
            ).lastrowid
        )
        old_snapshot = int(
            conn.execute(
                """
                INSERT INTO club_rating_snapshots(snapshot_date,source_file)
                VALUES ('2026-07-31','old.csv')
                """
            ).lastrowid
        )
        latest_snapshot = int(
            conn.execute(
                """
                INSERT INTO club_rating_snapshots(snapshot_date,source_file)
                VALUES ('2026-08-14','latest.csv')
                """
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO club_rating_entries(
                snapshot_id,client_id,external_user_id,display_name,points,place
            ) VALUES (?,?,'old-member','Old Member',100,9)
            """,
            (old_snapshot, client_id),
        )
        conn.execute(
            """
            INSERT INTO club_rating_entries(
                snapshot_id,client_id,external_user_id,display_name,points,place
            ) VALUES
                (?,NULL,'leader','Leader',1500,1),
                (?,?,'member','Member',1250,2)
            """,
            (latest_snapshot, latest_snapshot, client_id),
        )

        payload = hijack_rating_page_payload(
            conn,
            client_id=client_id,
            period="global",
        )

    assert payload["has_data"] is True
    assert payload["total"] == 2
    assert [row["place"] for row in payload["rows"]] == [1, 2]
    assert payload["rows"][0]["client_id"] is None
    assert payload["rows"][0]["display_name"] == "Leader"
    assert payload["me"]["client_id"] == client_id
    assert payload["me"]["place"] == 2
