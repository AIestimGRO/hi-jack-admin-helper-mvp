from __future__ import annotations

import io
from datetime import date
from pathlib import Path

from openpyxl import Workbook

from app.db import connect, init_db, transaction
from app.services.hijack_rating import (
    ensure_hijack_rating_schema,
    hijack_member_metrics,
    hijack_rating_payload,
    import_hijack_rating,
    parse_hijack_rating_workbook,
    referral_tree,
    refresh_hijack_engagement,
)


def _workbook_bytes(rows: list[tuple[object, object, object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Отчёт клуба"])
    sheet.append(["Phone", "ИГР Рейт", "ИГР Кил"])
    for row in rows:
        sheet.append(list(row))
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def _client(conn, phone: str, nickname: str) -> int:
    cursor = conn.execute(
        "INSERT INTO clients(phone_raw,phone_full,phone_local,nickname,source) VALUES (?,?,?,?,?)",
        (f"+7{phone}", f"+7{phone}", phone, nickname, "test"),
    )
    return int(cursor.lastrowid)


def test_parse_import_and_three_hi_jack_rating_views(tmp_path: Path) -> None:
    db_path = tmp_path / "rating.sqlite3"
    init_db(db_path)
    first_file = _workbook_bytes(
        [
            ("+7 999 111-22-33", 100, 2),
            ("8 999 444 55 66", "80,5", 1),
            ("bad", 40, 3),
        ]
    )
    second_file = _workbook_bytes(
        [
            ("+7 999 111-22-33", 60, 1),
            ("+7 999 444-55-66", 90, 4),
        ]
    )
    first_rows = parse_hijack_rating_workbook(first_file)
    second_rows = parse_hijack_rating_workbook(second_file)

    with transaction(db_path) as conn:
        first_id = _client(conn, "9991112233", "Alpha")
        second_id = _client(conn, "9994445566", "Bravo")
        ensure_hijack_rating_schema(conn)
        first = import_hijack_rating(
            conn,
            tournament_name="August One",
            tournament_date=date(2026, 8, 2),
            source_filename="one.xlsx",
            rows=first_rows,
            admin_id=None,
        )
        second = import_hijack_rating(
            conn,
            tournament_name="August Two",
            tournament_date=date(2026, 8, 8),
            source_filename="two.xlsx",
            rows=second_rows,
            admin_id=None,
        )
        assert first["matched_rows"] == 2
        assert first["invalid_rows"] == 1
        assert second["matched_rows"] == 2

        payload = hijack_rating_payload(conn, client_id=first_id, today=date(2026, 8, 10))
        assert payload["has_imports"] is True
        assert payload["latest"]["tournament_name"] == "August Two"
        assert payload["latest"]["me"]["points"] == 60
        assert payload["month"]["me"]["points"] == 160
        assert payload["year"]["me"]["points"] == 160
        assert payload["month"]["rows"][0]["client_id"] in {first_id, second_id}

        metrics = hijack_member_metrics(conn, client_id=second_id, today=date(2026, 8, 10))
        assert metrics["hijack_month_rating"] == 170.5
        assert metrics["hijack_month_kills"] == 5
        assert metrics["hijack_tournaments_played"] == 2
        assert metrics["hijack_best_rating"] == 90


def test_hi_jack_metric_can_award_title(tmp_path: Path) -> None:
    db_path = tmp_path / "titles.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        client_id = _client(conn, "9991112233", "Killer")
        ensure_hijack_rating_schema(conn)
        import_hijack_rating(
            conn,
            tournament_name="Kill Test",
            tournament_date=date.today(),
            source_filename="kills.xlsx",
            rows=[
                {
                    "source_row": 2,
                    "phone_raw": "+79991112233",
                    "phone_local": "9991112233",
                    "rating_points": 25,
                    "kills": 5,
                }
            ],
            admin_id=None,
        )
        cursor = conn.execute(
            """
            INSERT INTO title_definitions(
                code,name,description,icon,title_type,condition_code,threshold,period_code,priority
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                "hijack_killer_test",
                "Киллер клуба",
                "Пять киллов за год",
                "K",
                "permanent",
                "hijack_year_kills",
                5,
                "all_time",
                500,
            ),
        )
        title_definition_id = int(cursor.lastrowid)
        metrics = refresh_hijack_engagement(conn, client_id=client_id)
        assert metrics["hijack_year_kills"] == 5
        awarded = conn.execute(
            "SELECT * FROM member_titles WHERE client_id=? AND title_definition_id=?",
            (client_id, title_definition_id),
        ).fetchone()
        assert awarded is not None


def test_referral_tree_keeps_descendant_depth(tmp_path: Path) -> None:
    db_path = tmp_path / "tree.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        one = _client(conn, "9990000001", "One")
        two = _client(conn, "9990000002", "Two")
        three = _client(conn, "9990000003", "Three")
        four = _client(conn, "9990000004", "Four")
        conn.execute(
            "INSERT INTO referral_qualification_progress(referrer_client_id,invited_client_id,distinct_completed_days) VALUES (?,?,?)",
            (one, two, 3),
        )
        conn.execute(
            "INSERT INTO referral_qualification_progress(referrer_client_id,invited_client_id,distinct_completed_days) VALUES (?,?,?)",
            (two, three, 2),
        )
        conn.execute(
            "INSERT INTO referral_qualification_progress(referrer_client_id,invited_client_id,distinct_completed_days) VALUES (?,?,?)",
            (two, four, 1),
        )

        tree = referral_tree(conn, root_client_id=one)
        assert tree["direct"] == 1
        assert tree["total"] == 3
        assert tree["max_depth"] == 2
        assert tree["root"]["children"][0]["display_name"] == "Two"
        descendants = tree["root"]["children"][0]["children"]
        assert {item["display_name"] for item in descendants} == {"Three", "Four"}


def test_rating_schema_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "schema.sqlite3"
    init_db(db_path)
    with connect(db_path) as conn:
        ensure_hijack_rating_schema(conn)
        ensure_hijack_rating_schema(conn)
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='hi_jack_rating_imports'"
        ).fetchone()
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='hi_jack_rating_entries'"
        ).fetchone()
