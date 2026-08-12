from datetime import date, datetime, timezone

from app.db import connect, init_db, transaction
from app.prelaunch_economy_compat import ensure_prelaunch_economy_compat
from app.prelaunch_experience import ECONOMY_DEFAULTS, ensure_prelaunch_schema
from app.services.hijack_rating import ensure_hijack_rating_schema
from app.services.jackside_issues import create_issue


def _client(conn, name: str = "Player") -> int:
    return int(
        conn.execute(
            "INSERT INTO clients(first_name, source) VALUES (?, 'test')",
            (name,),
        ).lastrowid
    )


def test_prelaunch_economy_schema_seeds_launch_defaults(tmp_path) -> None:
    db_path = tmp_path / "prelaunch.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_hijack_rating_schema(conn)
        ensure_prelaunch_schema(conn)
        ensure_prelaunch_economy_compat(conn)
        amounts = {
            row["setting_key"]: int(row["amount"])
            for row in conn.execute(
                "SELECT setting_key, amount FROM jackcoin_economy_settings"
            ).fetchall()
        }

    expected = {key: amount for key, _section, _title, amount, _position in ECONOMY_DEFAULTS}
    assert amounts == expected
    assert amounts["jackside_correct"] == 10
    assert amounts["jackside_completion"] == 10
    assert amounts["jackside_perfect"] == 30
    assert amounts["jackside_final_correct"] == 50
    assert amounts["jackside_final_win"] == 414
    assert amounts["ref_jackside_first_l1"] == 150
    assert amounts["ref_jackside_first_l2"] == 50
    assert amounts["ref_jackside_first_l3"] == 20


def test_new_jackside_issue_snapshots_economy_and_does_not_reprice(tmp_path) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_prelaunch_schema(conn)
        ensure_prelaunch_economy_compat(conn)
        issue = create_issue(
            conn,
            issue_date_value=date(2026, 8, 20),
            starts_at=datetime(2026, 8, 20, 15, 14, tzinfo=timezone.utc),
        )
        issue = conn.execute(
            "SELECT * FROM jackside_issues WHERE id=?", (issue["id"],)
        ).fetchone()
        assert int(issue["jackcoin_per_correct"]) == 10
        assert int(issue["jackcoin_completion_bonus"]) == 10
        assert int(issue["jackcoin_perfect_bonus"]) == 30
        snapshot = int(
            conn.execute(
                """
                SELECT amount FROM jackcoin_economy_snapshots
                WHERE entity_type='jackside' AND entity_id=?
                  AND setting_key='jackside_correct'
                """,
                (issue["campaign_code"],),
            ).fetchone()[0]
        )
        assert snapshot == 10
        conn.execute(
            "UPDATE jackcoin_economy_settings SET amount=99 WHERE setting_key='jackside_correct'"
        )
        snapshot_after = int(
            conn.execute(
                """
                SELECT amount FROM jackcoin_economy_snapshots
                WHERE entity_type='jackside' AND entity_id=?
                  AND setting_key='jackside_correct'
                """,
                (issue["campaign_code"],),
            ).fetchone()[0]
        )
        assert snapshot_after == 10


def test_legacy_daily_campaign_is_not_repriced_by_launch_economy(tmp_path) -> None:
    db_path = tmp_path / "legacy-daily.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_prelaunch_schema(conn)
        ensure_prelaunch_economy_compat(conn)
        conn.execute(
            """
            INSERT INTO quiz_campaigns(
                code,title,campaign_type,jackcoin_per_correct,
                jackcoin_completion_bonus,jackcoin_perfect_bonus
            ) VALUES ('daily_test','Legacy daily','daily_414',5,10,20)
            """
        )
        row = conn.execute(
            "SELECT * FROM quiz_campaigns WHERE code='daily_test'"
        ).fetchone()
        assert int(row["jackcoin_per_correct"]) == 5
        assert int(row["jackcoin_completion_bonus"]) == 10
        assert int(row["jackcoin_perfect_bonus"]) == 20


def test_launch_streak_milestone_is_separate_and_idempotent(tmp_path) -> None:
    db_path = tmp_path / "streak.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_prelaunch_schema(conn)
        ensure_prelaunch_economy_compat(conn)
        client_id = _client(conn)
        create_issue(
            conn,
            issue_date_value=date(2026, 8, 20),
            starts_at=datetime(2026, 8, 20, 15, 14, tzinfo=timezone.utc),
        )
        conn.execute(
            """
            INSERT INTO daily_414_progress(
                client_id,current_streak,best_streak,last_issue_date
            ) VALUES (?,7,7,'2026-08-20')
            """,
            (client_id,),
        )
        rows = conn.execute(
            """
            SELECT amount,source_type,idempotency_key FROM jackcoin_ledger
            WHERE client_id=? ORDER BY id
            """,
            (client_id,),
        ).fetchall()
        assert [(int(row["amount"]), row["source_type"]) for row in rows] == [
            (70, "jackside_streak")
        ]

        conn.execute(
            """
            UPDATE daily_414_progress
            SET current_streak=7,best_streak=7,last_issue_date='2026-08-20'
            WHERE client_id=?
            """,
            (client_id,),
        )
        assert int(
            conn.execute(
                "SELECT COUNT(*) FROM jackcoin_ledger WHERE client_id=?",
                (client_id,),
            ).fetchone()[0]
        ) == 1


def test_hijack_regular_participation_awards_50_and_final_awards_zero(tmp_path) -> None:
    db_path = tmp_path / "hijack.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_hijack_rating_schema(conn)
        ensure_prelaunch_schema(conn)
        ensure_prelaunch_economy_compat(conn)
        regular_client = _client(conn, "Regular")
        final_client = _client(conn, "Final")

        regular_import = int(
            conn.execute(
                """
                INSERT INTO hi_jack_rating_imports(
                    tournament_name,tournament_date,source_filename
                ) VALUES ('Sunday Main','2026-08-20','regular.xlsx')
                """
            ).lastrowid
        )
        final_import = int(
            conn.execute(
                """
                INSERT INTO hi_jack_rating_imports(
                    tournament_name,tournament_date,source_filename
                ) VALUES ('Финал месяца','2026-08-21','final.xlsx')
                """
            ).lastrowid
        )
        assert conn.execute(
            "SELECT tournament_type FROM hi_jack_rating_imports WHERE id=?",
            (regular_import,),
        ).fetchone()[0] == "regular"
        assert conn.execute(
            "SELECT tournament_type FROM hi_jack_rating_imports WHERE id=?",
            (final_import,),
        ).fetchone()[0] == "final"

        conn.execute(
            """
            INSERT INTO hi_jack_rating_entries(
                import_id,client_id,phone_local,phone_raw,rating_points,kills,source_row
            ) VALUES (?,?,?,?,?,?,1)
            """,
            (regular_import, regular_client, "9000000001", "9000000001", 10, 1),
        )
        conn.execute(
            """
            INSERT INTO hi_jack_rating_entries(
                import_id,client_id,phone_local,phone_raw,rating_points,kills,source_row
            ) VALUES (?,?,?,?,?,?,1)
            """,
            (final_import, final_client, "9000000002", "9000000002", 20, 2),
        )

    with connect(db_path) as conn:
        regular = conn.execute(
            """
            SELECT amount FROM jackcoin_ledger
            WHERE client_id=? AND source_type='hijack_participation'
            """,
            (regular_client,),
        ).fetchall()
        final = conn.execute(
            """
            SELECT amount FROM jackcoin_ledger
            WHERE client_id=? AND source_type='hijack_participation'
            """,
            (final_client,),
        ).fetchall()
        assert [int(row["amount"]) for row in regular] == [50]
        assert final == []


def test_social_links_start_inactive_without_hardcoded_urls(tmp_path) -> None:
    db_path = tmp_path / "links.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_prelaunch_schema(conn)
        rows = conn.execute(
            "SELECT code,url,is_active FROM club_social_links ORDER BY position"
        ).fetchall()
    assert [row["code"] for row in rows[:3]] == ["telegram", "miniapp", "yandex_maps"]
    assert all(row["url"] == "" for row in rows[:3])
    assert all(int(row["is_active"]) == 0 for row in rows[:3])
