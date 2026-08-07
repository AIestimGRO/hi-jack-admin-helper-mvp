from __future__ import annotations

from datetime import datetime, timezone

from app.db import init_db, transaction
from app.services.jackside_engagement import (
    effective_title,
    ensure_jackside_referral_code,
    fix_jackside_referral,
    process_referral_qualification,
    refresh_member_engagement,
    select_permanent_title,
)


def seed_client(conn, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO clients(username, source) VALUES (?, 'test')", (name,)
    )
    return int(cur.lastrowid)


def seed_daily_campaign(conn, code: str = "jackside_test") -> None:
    conn.execute(
        "INSERT INTO quiz_campaigns(code,title,campaign_type) VALUES (?, ?, 'daily_414')",
        (code, code),
    )


def add_completion(conn, client_id: int, created_at: str, *, code: str = "jackside_test", correct: int = 7) -> int:
    cur = conn.execute(
        """
        INSERT INTO quiz_submissions(
            campaign_code,client_id,phone_raw,phone_local,answers_json,max_score,
            correct_count,max_correct_count,main_round_completed,created_at,ip_hash
        ) VALUES (?,?, '', '', '{}',10,?,10,1,?, 'test')
        """,
        (code, client_id, correct, created_at),
    )
    return int(cur.lastrowid)


def setup_db(tmp_path):
    path = tmp_path / "stage5.sqlite3"
    init_db(path)
    return path


def test_three_different_dates_qualify_and_repeat_is_idempotent(tmp_path):
    path = setup_db(tmp_path)
    with transaction(path) as conn:
        seed_daily_campaign(conn)
        referrer = seed_client(conn, "owner")
        invited = seed_client(conn, "guest")
        code = ensure_jackside_referral_code(conn, referrer)
        assert fix_jackside_referral(
            conn, invited_client_id=invited, referral_code=code["code"], campaign_code="jackside_test"
        )["status"] == "fixed"
        submissions = [
            add_completion(conn, invited, "2026-08-01T12:00:00+00:00"),
            add_completion(conn, invited, "2026-08-02T12:00:00+00:00"),
            add_completion(conn, invited, "2026-08-03T12:00:00+00:00"),
        ]
        for submission in submissions:
            process_referral_qualification(
                conn, invited_client_id=invited, submission_id=submission, timezone_name="Europe/Moscow"
            )
        progress = conn.execute(
            "SELECT * FROM referral_qualification_progress WHERE invited_client_id=?", (invited,)
        ).fetchone()
        assert progress["distinct_completed_days"] == 3
        assert progress["qualified_at"] is not None
        qualified_at = progress["qualified_at"]
        process_referral_qualification(
            conn, invited_client_id=invited, submission_id=submissions[-1], timezone_name="Europe/Moscow"
        )
        progress2 = conn.execute(
            "SELECT * FROM referral_qualification_progress WHERE invited_client_id=?", (invited,)
        ).fetchone()
        assert progress2["qualified_at"] == qualified_at
        assert conn.execute(
            "SELECT COUNT(*) FROM referral_qualification_progress WHERE invited_client_id=?", (invited,)
        ).fetchone()[0] == 1


def test_three_games_same_calendar_day_do_not_qualify(tmp_path):
    path = setup_db(tmp_path)
    with transaction(path) as conn:
        seed_daily_campaign(conn)
        referrer = seed_client(conn, "owner")
        invited = seed_client(conn, "guest")
        code = ensure_jackside_referral_code(conn, referrer)
        fix_jackside_referral(conn, invited_client_id=invited, referral_code=code["code"], campaign_code="jackside_test")
        last = None
        for hour in (8, 12, 18):
            last = add_completion(conn, invited, f"2026-08-01T{hour:02d}:00:00+00:00")
        process_referral_qualification(conn, invited_client_id=invited, submission_id=last, timezone_name="Europe/Moscow")
        progress = conn.execute("SELECT * FROM referral_qualification_progress WHERE invited_client_id=?", (invited,)).fetchone()
        assert progress["distinct_completed_days"] == 1
        assert progress["qualified_at"] is None


def test_self_referral_and_referrer_change_are_rejected(tmp_path):
    path = setup_db(tmp_path)
    with transaction(path) as conn:
        seed_daily_campaign(conn)
        first = seed_client(conn, "first")
        second = seed_client(conn, "second")
        guest = seed_client(conn, "guest")
        first_code = ensure_jackside_referral_code(conn, first)
        second_code = ensure_jackside_referral_code(conn, second)
        assert fix_jackside_referral(conn, invited_client_id=first, referral_code=first_code["code"], campaign_code="jackside_test")["status"] == "self_referral"
        assert conn.execute("SELECT COUNT(*) FROM referral_qualification_progress WHERE invited_client_id=?", (first,)).fetchone()[0] == 0
        assert fix_jackside_referral(conn, invited_client_id=guest, referral_code=first_code["code"], campaign_code="jackside_test")["status"] == "fixed"
        locked = fix_jackside_referral(conn, invited_client_id=guest, referral_code=second_code["code"], campaign_code="jackside_test")
        assert locked["status"] == "referrer_locked"
        assert locked["referrer_client_id"] == first


def test_temporary_title_expires_but_stays_in_history(tmp_path):
    path = setup_db(tmp_path)
    with transaction(path) as conn:
        seed_daily_campaign(conn)
        player = seed_client(conn, "player")
        add_completion(conn, player, "2026-08-10T12:00:00+00:00", correct=10)
        conn.execute(
            """INSERT INTO title_definitions(code,name,title_type,condition_code,threshold,period_code,priority)
               VALUES ('temp_test','Temp Test','temporary','completed_games',1,'month',9999)"""
        )
        refresh_member_engagement(
            conn, client_id=player, timezone_name="Europe/Moscow",
            now=datetime(2026, 8, 10, 12, tzinfo=timezone.utc),
        )
        current = effective_title(conn, client_id=player, now=datetime(2026, 8, 10, 12, tzinfo=timezone.utc))
        assert current and current["code"] == "temp_test"
        assert effective_title(conn, client_id=player, now=datetime(2026, 9, 2, 12, tzinfo=timezone.utc))["code"] != "temp_test"
        assert conn.execute(
            """SELECT COUNT(*) FROM member_titles mt JOIN title_definitions td ON td.id=mt.title_definition_id
               WHERE mt.client_id=? AND td.code='temp_test'""", (player,)
        ).fetchone()[0] == 1


def test_member_can_select_one_permanent_title(tmp_path):
    path = setup_db(tmp_path)
    with transaction(path) as conn:
        seed_daily_campaign(conn)
        player = seed_client(conn, "player")
        for day in range(1, 11):
            add_completion(conn, player, f"2026-08-{day:02d}T12:00:00+00:00")
        refresh_member_engagement(conn, client_id=player, timezone_name="Europe/Moscow")
        rows = conn.execute(
            """SELECT mt.id,td.code FROM member_titles mt JOIN title_definitions td ON td.id=mt.title_definition_id
               WHERE mt.client_id=? AND mt.temporary_period_id IS NULL ORDER BY mt.id""", (player,)
        ).fetchall()
        assert len(rows) >= 2
        select_permanent_title(conn, client_id=player, member_title_id=int(rows[0]["id"]))
        select_permanent_title(conn, client_id=player, member_title_id=int(rows[1]["id"]))
        selected = conn.execute("SELECT id FROM member_titles WHERE client_id=? AND selected=1", (player,)).fetchall()
        assert [row["id"] for row in selected] == [rows[1]["id"]]


def test_shared_final_win_counts_as_win_for_configurable_title(tmp_path):
    path = setup_db(tmp_path)
    with transaction(path) as conn:
        seed_daily_campaign(conn)
        player = seed_client(conn, "player")
        submission = add_completion(conn, player, "2026-08-01T12:00:00+00:00")
        ft = conn.execute(
            """INSERT INTO daily_414_final_tables(campaign_code,campaign_version,starts_at,questions_snapshot_json,status,outcome,completed_at)
               VALUES ('jackside_test',1,'2026-08-01T13:00:00+00:00','[]','completed','co_winners','2026-08-01T13:10:00+00:00')"""
        ).lastrowid
        conn.execute(
            "INSERT INTO daily_414_finalists(final_table_id,submission_id,client_id,seed,status) VALUES (?,?,?,?, 'winner')",
            (ft, submission, player, 1),
        )
        conn.execute(
            """INSERT INTO title_definitions(code,name,title_type,condition_code,threshold,period_code,priority)
               VALUES ('co_win_test','Co Win','permanent','wins',1,'all_time',9999)"""
        )
        refresh_member_engagement(conn, client_id=player, timezone_name="Europe/Moscow")
        assert conn.execute(
            """SELECT COUNT(*) FROM member_titles mt JOIN title_definitions td ON td.id=mt.title_definition_id
               WHERE mt.client_id=? AND td.code='co_win_test'""", (player,)
        ).fetchone()[0] == 1


def test_classic_referral_schema_and_scope_remain_compatible(tmp_path):
    path = setup_db(tmp_path)
    with transaction(path) as conn:
        owner = seed_client(conn, "classic_owner")
        guest = seed_client(conn, "classic_guest")
        conn.execute("INSERT INTO quiz_campaigns(code,title,campaign_type) VALUES ('classic_test','Classic','classic')")
        classic_code = conn.execute(
            "INSERT INTO quiz_referral_codes(code,client_id,campaign_code) VALUES ('CLASSICCODE',?,'classic_test')",
            (owner,),
        ).lastrowid
        conn.execute(
            """INSERT INTO quiz_referrals(campaign_code,referrer_client_id,invited_client_id,referral_code_id)
               VALUES ('classic_test',?,?,?)""",
            (owner, guest, classic_code),
        )
        assert conn.execute("SELECT COUNT(*) FROM quiz_referrals WHERE campaign_code='classic_test'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM referral_qualification_progress").fetchone()[0] == 0


def test_referrer_and_invited_rewards_are_configured_separately(tmp_path):
    path = setup_db(tmp_path)
    with transaction(path) as conn:
        seed_daily_campaign(conn)
        referrer = seed_client(conn, "reward_owner")
        invited = seed_client(conn, "reward_guest")
        conn.execute(
            """UPDATE jackside_referral_settings
               SET referrer_preference_code='free_entry',referrer_amount=2,
                   invited_preference_code='free_entry',invited_amount=1
               WHERE id=1"""
        )
        code = ensure_jackside_referral_code(conn, referrer)
        fix_jackside_referral(conn, invited_client_id=invited, referral_code=code["code"], campaign_code="jackside_test")
        last = None
        for day in (1, 2, 3):
            last = add_completion(conn, invited, f"2026-08-{day:02d}T12:00:00+00:00")
        process_referral_qualification(conn, invited_client_id=invited, submission_id=last, timezone_name="Europe/Moscow")
        progress = conn.execute("SELECT * FROM referral_qualification_progress WHERE invited_client_id=?", (invited,)).fetchone()
        assert progress["referrer_reward_id"] is not None
        assert progress["invited_reward_id"] is not None
        balances = {}
        for client_id, key in ((referrer, "referrer"), (invited, "invited")):
            balances[key] = conn.execute(
                """SELECT cp.balance_int FROM client_preferences cp JOIN preference_types pt ON pt.id=cp.preference_type_id
                   WHERE cp.client_id=? AND pt.code='free_entry'""", (client_id,)
            ).fetchone()[0]
        assert balances == {"referrer": 2, "invited": 1}
        reward_count = conn.execute(
            "SELECT COUNT(*) FROM quiz_reward_codes WHERE campaign_code LIKE 'jackside_referral_%'"
        ).fetchone()[0]
        process_referral_qualification(conn, invited_client_id=invited, submission_id=last, timezone_name="Europe/Moscow")
        assert conn.execute(
            "SELECT COUNT(*) FROM quiz_reward_codes WHERE campaign_code LIKE 'jackside_referral_%'"
        ).fetchone()[0] == reward_count
