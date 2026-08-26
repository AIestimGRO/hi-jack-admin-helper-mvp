from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import connect, transaction
from app.main import create_app
from app.services.daily_414_final import ensure_final_table, reconcile_final_table
from app.services.jackside_issues import create_issue, ensure_issue_campaign
from app.services.member_accounts import MEMBER_COOKIE_NAME, issue_session
from app.services.quiz import load_final_questions
from app.services.vault import create_catalog_reward


SECRET = "test-secret-key-that-is-longer-than-32-characters"
TZ = ZoneInfo("Europe/Moscow")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        admin_pin="2468",
        admin_name="Test Admin",
        secret_key=SECRET,
        db_path=tmp_path / "winner-superprize.sqlite3",
        secure_cookie=False,
        member_portal_enabled=True,
        timezone_name="Europe/Moscow",
    )


def _seed_member(conn, settings: Settings) -> tuple[int, int, str]:
    client_id = int(
        conn.execute(
            "INSERT INTO clients(first_name, phone_raw, phone_local, source) "
            "VALUES ('Virtual Winner', '+79990000001', '9990000001', 'test')"
        ).lastrowid
    )
    account_id = int(
        conn.execute(
            """
            INSERT INTO member_accounts(
                client_id, email, email_normalized, password_hash, email_verified_at
            ) VALUES (?, 'winner@example.test', 'winner@example.test', 'test-hash', CURRENT_TIMESTAMP)
            """,
            (client_id,),
        ).lastrowid
    )
    for document in conn.execute(
        "SELECT id, code, version FROM legal_documents WHERE is_active=1"
    ).fetchall():
        conn.execute(
            """
            INSERT INTO member_consents(
                account_id, document_id, document_code, document_version,
                ip_hash, user_agent
            ) VALUES (?, ?, ?, ?, 'test-ip', 'testclient')
            """,
            (
                account_id,
                int(document["id"]),
                str(document["code"]),
                str(document["version"]),
            ),
        )
    token = issue_session(
        conn,
        secret_key=settings.secret_key,
        account_id=account_id,
        session_version=1,
        days=30,
        ip_hash="test-ip",
        user_agent="testclient",
    )
    return client_id, account_id, token


def _seed_hidden_superprize(conn, admin_id: int) -> int:
    reward = create_catalog_reward(
        conn,
        code="virtual_free_reentry",
        title="FreeReEntry",
        description="Hidden JACKSIDE winner prize",
        category="club",
        price_jc=1500,
        validity_days=0,
        inventory_total=None,
        redeem_instructions="Show to admin",
        position=100,
        admin_id=admin_id,
    )
    conn.execute(
        "UPDATE vault_catalog_rewards SET is_active=0 WHERE id=?",
        (int(reward["id"]),),
    )
    return int(reward["id"])


def _seed_virtual_game(conn, *, client_id: int, catalog_reward_id: int) -> tuple[str, int]:
    now = datetime.now(timezone.utc)
    final_start = now - timedelta(seconds=35)
    issue_start = final_start - timedelta(minutes=5, seconds=14)
    local_day = issue_start.astimezone(TZ).date()

    issue = create_issue(
        conn,
        issue_date_value=local_day,
        starts_at=issue_start,
        title="JACKSIDE virtual winner E2E",
        jackcoin_per_correct=10,
        jackcoin_completion_bonus=10,
        jackcoin_perfect_bonus=30,
        final_question_time_seconds=30,
        final_prize_type="reward_card",
        final_prize_catalog_reward_id=catalog_reward_id,
        final_prize_jackcoin_amount=0,
        timezone_name="Europe/Moscow",
    )
    campaign = ensure_issue_campaign(
        conn,
        issue=issue,
        timezone_name="Europe/Moscow",
    )
    campaign_code = str(campaign["code"])
    conn.execute(
        """
        UPDATE quiz_campaigns
        SET is_active=1,
            final_prize_type='reward_card',
            final_prize_catalog_reward_id=?,
            final_prize_jackcoin_amount=0,
            final_question_time_seconds=30
        WHERE code=?
        """,
        (catalog_reward_id, campaign_code),
    )
    conn.execute(
        """
        UPDATE jackside_issues
        SET status='scheduled', published_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (int(issue["id"]),),
    )
    question_id = int(
        conn.execute(
            """
            INSERT INTO quiz_questions(
                campaign_code, code, type, title, accepted_text_answers_json,
                game_round, required, points, time_limit_seconds, position, is_active
            ) VALUES (?, 'final_1', 'text', 'Virtual decisive question',
                      '[\"yes\"]', 'final', 1, 1, 30, 10, 1)
            """,
            (campaign_code,),
        ).lastrowid
    )
    submission_id = int(
        conn.execute(
            """
            INSERT INTO quiz_submissions(
                campaign_code, campaign_version, client_id, phone_raw, phone_local,
                answers_json, correct_count, max_correct_count, completion_time_ms,
                main_prize_eligible, main_round_completed, jackcoin_awarded, ip_hash
            ) VALUES (?, 1, ?, '+79990000001', '9990000001', '{}',
                      10, 10, 90000, 1, 1, 140, 'test-ip')
            """,
            (campaign_code, client_id),
        ).lastrowid
    )
    conn.execute(
        """
        INSERT INTO jackcoin_ledger(
            client_id, amount, operation_type, source_type, source_id,
            idempotency_key, comment
        ) VALUES (?, 140, 'earn', 'daily_414', ?, ?, 'virtual main round')
        """,
        (
            client_id,
            str(submission_id),
            f"daily_414:submission:{submission_id}",
        ),
    )
    final_questions = load_final_questions(conn, campaign_code)
    assert final_questions
    assert int(final_questions[0]["db_id"]) == question_id
    assert final_questions[0]["id"] == "final_1"
    table = ensure_final_table(
        conn,
        campaign_code=campaign_code,
        campaign_version=1,
        starts_at=final_start,
        questions=final_questions,
        question_time_seconds=30,
    )
    assert table["prize_type"] == "reward_card"
    assert int(table["prize_catalog_reward_id"]) == catalog_reward_id
    live = reconcile_final_table(
        conn,
        final_table_id=int(table["id"]),
        now=final_start,
        schedule_starts_at=final_start,
    )
    assert live["status"] == "live"
    finalist = conn.execute(
        "SELECT * FROM daily_414_finalists WHERE final_table_id=?",
        (int(table["id"]),),
    ).fetchone()
    assert finalist is not None
    conn.execute(
        """
        INSERT INTO daily_414_final_answers(
            final_table_id, finalist_id, question_index, question_code,
            answer_json, is_correct, response_time_ms, answered_at
        ) VALUES (?, ?, 0, 'final_1', '\"yes\"', 1, 800, ?)
        """,
        (
            int(table["id"]),
            int(finalist["id"]),
            (final_start + timedelta(milliseconds=800)).isoformat(),
        ),
    )
    return campaign_code, int(table["id"])


def test_virtual_winner_gets_hidden_free_reentry_through_real_status_flow(tmp_path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        with transaction(settings.db_path) as conn:
            admin_id = int(
                conn.execute(
                    "SELECT id FROM admins WHERE role='master_admin' ORDER BY id LIMIT 1"
                ).fetchone()[0]
            )
            client_id, _account_id, token = _seed_member(conn, settings)
            card_id = _seed_hidden_superprize(conn, admin_id)
            campaign_code, final_table_id = _seed_virtual_game(
                conn,
                client_id=client_id,
                catalog_reward_id=card_id,
            )

        client.cookies.set(MEMBER_COOKIE_NAME, token)
        status = client.get(
            "/api/quiz/final-table/status",
            params={"campaign": campaign_code},
        )
        assert status.status_code == 200, status.text
        payload = status.json()
        assert payload["state"] == "winner", payload

        with connect(settings.db_path) as conn:
            table = conn.execute(
                "SELECT * FROM daily_414_final_tables WHERE id=?",
                (final_table_id,),
            ).fetchone()
            assert table["status"] == "completed"
            assert table["outcome"] == "single_winner"
            assert table["prize_type"] == "reward_card"
            assert int(table["prize_catalog_reward_id"]) == card_id
            assert table["prize_resolution"] == "awarded"
            assert table["winner_reward_error"] is None
            assert table["winner_reward_id"] is not None
            reward = conn.execute(
                "SELECT * FROM vault_member_rewards WHERE id=?",
                (int(table["winner_reward_id"]),),
            ).fetchone()
            assert reward is not None
            assert int(reward["client_id"]) == client_id
            assert int(reward["catalog_reward_id"]) == card_id
            assert reward["source_type"] == "final_prize"
            assert reward["source_id"] == str(final_table_id)
            assert int(reward["price_paid_jc"] or 0) == 0
            assert reward["status"] == "active"
            assert conn.execute(
                "SELECT COUNT(*) FROM vault_member_rewards "
                "WHERE source_type='final_prize' AND source_id=?",
                (str(final_table_id),),
            ).fetchone()[0] == 1
            balance = int(
                conn.execute(
                    "SELECT COALESCE(SUM(amount),0) FROM jackcoin_ledger WHERE client_id=?",
                    (client_id,),
                ).fetchone()[0]
            )
            assert balance == 414

        outcome = client.get(
            "/api/jackside/final-outcome",
            params={"campaign": campaign_code},
        )
        assert outcome.status_code == 200, outcome.text
        outcome_payload = outcome.json()
        assert outcome_payload["state"] == "winner"
        superprize = outcome_payload["superprize"]
        assert superprize["member_reward_id"] == int(table["winner_reward_id"])
        assert superprize["catalog_reward_id"] == card_id
        assert superprize["title"] == "FreeReEntry"
        assert superprize["kind"] == "jack_card"
        assert superprize["status"] == "active"
        assert superprize["my_cards_url"] == "/account?tab=vault&store=cards"

        vault_page = client.get("/account", params={"tab": "vault"})
        assert vault_page.status_code == 200
        assert "FreeReEntry" in vault_page.text


def test_virtual_no_winner_does_not_issue_superprize(tmp_path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        with transaction(settings.db_path) as conn:
            admin_id = int(
                conn.execute(
                    "SELECT id FROM admins WHERE role='master_admin' ORDER BY id LIMIT 1"
                ).fetchone()[0]
            )
            client_id, _account_id, token = _seed_member(conn, settings)
            card_id = _seed_hidden_superprize(conn, admin_id)
            campaign_code, final_table_id = _seed_virtual_game(
                conn,
                client_id=client_id,
                catalog_reward_id=card_id,
            )
            conn.execute(
                "UPDATE daily_414_final_answers SET is_correct=0 WHERE final_table_id=?",
                (final_table_id,),
            )

        client.cookies.set(MEMBER_COOKIE_NAME, token)
        status = client.get(
            "/api/quiz/final-table/status",
            params={"campaign": campaign_code},
        )
        if status.status_code == 404:
            fallback = client.get(
                "/api/jackside/final-result",
                params={"campaign": campaign_code},
            )
            assert fallback.status_code == 200, fallback.text
            assert fallback.json()["state"] != "winner"
        else:
            assert status.status_code == 200, status.text
            assert status.json()["state"] != "winner"

        with connect(settings.db_path) as conn:
            table = conn.execute(
                "SELECT * FROM daily_414_final_tables WHERE id=?",
                (final_table_id,),
            ).fetchone()
            assert table["status"] == "completed"
            assert table["outcome"] == "no_winner"
            assert table["winner_reward_id"] is None
            assert conn.execute(
                "SELECT COUNT(*) FROM vault_member_rewards "
                "WHERE source_type='final_prize' AND source_id=?",
                (str(final_table_id),),
            ).fetchone()[0] == 0
