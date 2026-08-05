"""JACKSIDE daily issues: create, copy, publish, legacy, participants, rules."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.db import init_db, transaction
from app.services.jackside_copy import result_copy_for_score
from app.services.jackside_issues import (
    accept_rules,
    active_rules,
    copy_issue,
    create_issue,
    current_featured_issue,
    legacy_issue_from_campaign,
    missing_jackside_rules,
    publish_rules_version,
    register_issue_participant,
    resolve_issue_for_campaign,
    schedule_issue,
    unique_participant_count,
    validate_issue_for_publish,
)


def _add_main_questions(conn, campaign_code: str, count: int = 10) -> None:
    for index in range(1, count + 1):
        qid = int(
            conn.execute(
                """
                INSERT INTO quiz_questions(
                    campaign_code, code, type, title, position, is_active, game_round
                ) VALUES (?, ?, 'single_choice', ?, ?, 1, 'main')
                """,
                (campaign_code, f"m{index}", f"Main {index}", index * 10),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO quiz_options(question_id, code, text, is_correct, position)
            VALUES (?, 'yes', 'Да', 1, 10), (?, 'no', 'Нет', 0, 20)
            """,
            (qid, qid),
        )


def _add_final_questions(conn, campaign_code: str, count: int = 2) -> None:
    for index in range(1, count + 1):
        qid = int(
            conn.execute(
                """
                INSERT INTO quiz_questions(
                    campaign_code, code, type, title, position, is_active, game_round
                ) VALUES (?, ?, 'single_choice', ?, ?, 1, 'final')
                """,
                (campaign_code, f"f{index}", f"Final {index}", index * 10),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO quiz_options(question_id, code, text, is_correct, position)
            VALUES (?, 'yes', 'Да', 1, 10), (?, 'no', 'Нет', 0, 20)
            """,
            (qid, qid),
        )


def test_create_and_copy_issue(tmp_path) -> None:
    db_path = tmp_path / "issues-create.sqlite3"
    init_db(db_path)
    starts = datetime(2026, 8, 10, 15, 14, tzinfo=timezone.utc)
    with transaction(db_path) as conn:
        first = create_issue(
            conn,
            issue_date_value=date(2026, 8, 10),
            starts_at=starts,
            final_prize_type="jackcoin",
            final_prize_jackcoin_amount=500,
        )
        _add_main_questions(conn, first["campaign_code"], 10)
        _add_final_questions(conn, first["campaign_code"], 2)
        copied = copy_issue(
            conn,
            source_issue_id=int(first["id"]),
            issue_date_value=date(2026, 8, 11),
            starts_at=starts + timedelta(days=1),
        )
        assert first["status"] == "draft"
        assert first["final_prize_jackcoin_amount"] == 500
        assert copied["issue_date"] == "2026-08-11"
        assert copied["campaign_code"] != first["campaign_code"]
        assert copied["final_prize_jackcoin_amount"] == 500
        main = conn.execute(
            """
            SELECT COUNT(*) FROM quiz_questions
            WHERE campaign_code=? AND game_round='main'
            """,
            (copied["campaign_code"],),
        ).fetchone()[0]
        assert main == 10


def test_publish_requires_exactly_ten_main_questions(tmp_path) -> None:
    db_path = tmp_path / "issues-publish.sqlite3"
    init_db(db_path)
    starts = datetime(2026, 8, 12, 15, 14, tzinfo=timezone.utc)
    with transaction(db_path) as conn:
        issue = create_issue(
            conn,
            issue_date_value=date(2026, 8, 12),
            starts_at=starts,
            final_prize_type="jackcoin",
            final_prize_jackcoin_amount=100,
        )
        _add_main_questions(conn, issue["campaign_code"], 9)
        _add_final_questions(conn, issue["campaign_code"], 1)
        errors = validate_issue_for_publish(conn, issue)
        assert "main_questions_must_be_ten" in errors
        try:
            schedule_issue(conn, issue_id=int(issue["id"]))
            assert False, "expected publish to fail"
        except ValueError as exc:
            assert "main_questions_must_be_ten" in str(exc)

        _add_main_questions(conn, issue["campaign_code"], 0)  # no-op helper path
        # Add one more to reach 10
        qid = int(
            conn.execute(
                """
                INSERT INTO quiz_questions(
                    campaign_code, code, type, title, position, is_active, game_round
                ) VALUES (?, 'm10', 'single_choice', 'Main 10', 100, 1, 'main')
                """,
                (issue["campaign_code"],),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO quiz_options(question_id, code, text, is_correct, position)
            VALUES (?, 'yes', 'Да', 1, 10), (?, 'no', 'Нет', 0, 20)
            """,
            (qid, qid),
        )
        # Also test 11 questions blocked
        qid11 = int(
            conn.execute(
                """
                INSERT INTO quiz_questions(
                    campaign_code, code, type, title, position, is_active, game_round
                ) VALUES (?, 'm11', 'single_choice', 'Main 11', 110, 1, 'main')
                """,
                (issue["campaign_code"],),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO quiz_options(question_id, code, text, is_correct, position)
            VALUES (?, 'yes', 'Да', 1, 10), (?, 'no', 'Нет', 0, 20)
            """,
            (qid11, qid11),
        )
        errors = validate_issue_for_publish(conn, issue)
        assert "main_questions_must_be_ten" in errors
        conn.execute("DELETE FROM quiz_questions WHERE code='m11'")
        scheduled = schedule_issue(conn, issue_id=int(issue["id"]))
        assert scheduled["status"] == "scheduled"
        campaign = conn.execute(
            "SELECT * FROM quiz_campaigns WHERE code=?",
            (scheduled["campaign_code"],),
        ).fetchone()
        assert campaign["is_active"] == 1
        assert campaign["campaign_type"] == "daily_414"


def test_legacy_daily_414_fallback(tmp_path) -> None:
    db_path = tmp_path / "issues-legacy.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            """
            INSERT INTO quiz_campaigns(
                code, title, campaign_type, is_active, active_from,
                jackcoin_per_correct, final_prize_type, final_prize_jackcoin_amount
            ) VALUES (
                'legacy_daily', 'Legacy 4:14', 'daily_414', 1,
                '2026-08-05T18:14:00', 5, 'jackcoin', 750
            )
            """
        )
        campaign = conn.execute(
            "SELECT * FROM quiz_campaigns WHERE code='legacy_daily'"
        ).fetchone()
        view = legacy_issue_from_campaign(conn, campaign)
        assert view["legacy"] is True
        assert view["campaign_code"] == "legacy_daily"
        assert "750 JACKCOIN" in view["prize_headline"]
        resolved = resolve_issue_for_campaign(conn, campaign)
        assert resolved["legacy"] is True


def test_unique_participant_counter_and_reopen(tmp_path) -> None:
    db_path = tmp_path / "issues-participants.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        issue = create_issue(
            conn,
            issue_date_value=date(2026, 8, 13),
            starts_at=datetime(2026, 8, 13, 15, 14, tzinfo=timezone.utc),
        )
        client_id = int(
            conn.execute(
                "INSERT INTO clients(first_name, source) VALUES ('A', 'test')"
            ).lastrowid
        )
        account_id = int(
            conn.execute(
                """
                INSERT INTO member_accounts(
                    client_id, email, email_normalized, password_hash, email_verified_at
                ) VALUES (?, 'a@test.local', 'a@test.local', 'x', CURRENT_TIMESTAMP)
                """,
                (client_id,),
            ).lastrowid
        )
        assert register_issue_participant(
            conn,
            issue_id=int(issue["id"]),
            client_id=client_id,
            account_id=account_id,
        )
        assert not register_issue_participant(
            conn,
            issue_id=int(issue["id"]),
            client_id=client_id,
            account_id=account_id,
        )
        assert unique_participant_count(conn, issue_id=int(issue["id"])) == 1


def test_rules_version_requires_reaccept(tmp_path) -> None:
    db_path = tmp_path / "issues-rules.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        client_id = int(
            conn.execute(
                "INSERT INTO clients(first_name, source) VALUES ('B', 'test')"
            ).lastrowid
        )
        account_id = int(
            conn.execute(
                """
                INSERT INTO member_accounts(
                    client_id, email, email_normalized, password_hash, email_verified_at
                ) VALUES (?, 'b@test.local', 'b@test.local', 'x', CURRENT_TIMESTAMP)
                """,
                (client_id,),
            ).lastrowid
        )
        rules = active_rules(conn)
        assert missing_jackside_rules(conn, account_id=account_id) is not None
        accept_rules(
            conn,
            account_id=account_id,
            rules=rules,
            ip_hash="hash",
        )
        assert missing_jackside_rules(conn, account_id=account_id) is None
        publish_rules_version(
            conn,
            version="1.1",
            title="Правила JACKSIDE 4:14",
            content="Обновлённые правила JACKSIDE. " + ("подробности " * 10),
        )
        missing = missing_jackside_rules(conn, account_id=account_id)
        assert missing is not None
        assert missing["version"] == "1.1"


def test_result_copy_ranges() -> None:
    assert result_copy_for_score(0)["code"] == "0_3"
    assert result_copy_for_score(3)["code"] == "0_3"
    assert result_copy_for_score(5)["code"] == "4_6"
    assert result_copy_for_score(8)["code"] == "7_8"
    assert result_copy_for_score(9)["code"] == "9"
    assert result_copy_for_score(10)["code"] == "10"


def test_cancelled_and_closed_issue_status(tmp_path) -> None:
    db_path = tmp_path / "issues-status.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        issue = create_issue(
            conn,
            issue_date_value=date(2026, 8, 14),
            starts_at=datetime(2026, 8, 14, 15, 14, tzinfo=timezone.utc),
        )
        from app.services.jackside_issues import update_issue_settings

        cancelled = update_issue_settings(
            conn, issue_id=int(issue["id"]), status="cancelled"
        )
        assert cancelled["status"] == "cancelled"
        closed = update_issue_settings(
            conn, issue_id=int(issue["id"]), status="closed"
        )
        assert closed["status"] == "closed"


def test_classic_campaign_unaffected_by_issues(tmp_path) -> None:
    db_path = tmp_path / "issues-classic.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        create_issue(
            conn,
            issue_date_value=date(2026, 8, 15),
            starts_at=datetime(2026, 8, 15, 15, 14, tzinfo=timezone.utc),
        )
        classic = conn.execute(
            "SELECT * FROM quiz_campaigns WHERE code='default'"
        ).fetchone()
        assert classic is not None
        assert classic["campaign_type"] in {None, "classic"} or classic["campaign_type"] == "classic"
        featured = current_featured_issue(
            conn, now=datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
        )
        # draft issues are ignored by featured selector
        assert featured is None or featured.get("campaign_code") != "default"
