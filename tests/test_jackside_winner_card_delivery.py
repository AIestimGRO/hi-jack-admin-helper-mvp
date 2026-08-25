from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db import init_db, transaction
from app.jackside_winner_prize import set_future_winner_card_prize
from app.prelaunch_economy_compat import ensure_prelaunch_economy_compat
from app.prelaunch_experience import ensure_prelaunch_schema
from app.services.daily_414_final import (
    ensure_final_table,
    list_final_winners,
    reconcile_final_table,
)
from app.services.jackside_issues import create_issue, ensure_issue_campaign
from app.services.member_accounts import jackcoin_balance
from app.services.vault import (
    activate_reward,
    attach_final_table_reward,
    create_catalog_reward,
    redeem_reward,
)


def _catalog(conn):
    conn.execute(
        """
        INSERT OR IGNORE INTO admins(
            id,username,display_name,pin_hash,role
        ) VALUES (1,'winner-delivery-test','Winner Delivery Test','test','master_admin')
        """
    )
    reward = create_catalog_reward(
        conn,
        code="free_reentry_delivery",
        title="FREE RE-ENTRY",
        description="Один бесплатный re-entry",
        category="entry",
        price_jc=700,
        validity_days=30,
        inventory_total=None,
        redeem_instructions="Покажите JACK CARD администратору",
        position=10,
        admin_id=1,
    )
    # Prize-only card: hidden from the member Market but still stored in THE VAULT.
    conn.execute(
        "UPDATE vault_catalog_rewards SET is_active=0 WHERE id=?",
        (int(reward["id"]),),
    )
    return reward


def _candidate(conn, *, campaign: str, number: int) -> tuple[int, int]:
    client_id = int(
        conn.execute(
            "INSERT INTO clients(first_name,source) VALUES (?, 'test')",
            (f"Player {number}",),
        ).lastrowid
    )
    submission_id = int(
        conn.execute(
            """
            INSERT INTO quiz_submissions(
                campaign_code,campaign_version,client_id,phone_raw,phone_local,
                answers_json,correct_count,max_correct_count,completion_time_ms,
                main_prize_eligible,main_round_completed,jackcoin_awarded,ip_hash
            ) VALUES (?,1,?,?,?,'{}',10,10,10000,1,1,0,?)
            """,
            (campaign, client_id, str(number), str(number), f"ip-{number}"),
        ).lastrowid
    )
    return client_id, submission_id


def test_issue_prize_survives_core_final_table_creation_without_prize_args(tmp_path) -> None:
    db_path = tmp_path / "winner-card-core-snapshot.sqlite3"
    init_db(db_path)
    issue_start = datetime(2026, 8, 26, 18, 10, tzinfo=timezone.utc)

    with transaction(db_path) as conn:
        reward = _catalog(conn)
        issue = create_issue(
            conn,
            issue_date_value=issue_start.date(),
            starts_at=issue_start,
            title="JACKSIDE core snapshot prize",
            admin_id=1,
        )
        ensure_issue_campaign(conn, issue=issue, timezone_name="UTC")
        updated = set_future_winner_card_prize(
            conn,
            issue_id=int(issue["id"]),
            catalog_reward_id=int(reward["id"]),
            now=issue_start - timedelta(minutes=10),
        )
        assert updated["final_prize_type"] == "reward_card"

        # Core callers may omit prize arguments. Snapshot ownership belongs to
        # the final-table service, which resolves the JACKSIDE campaign config.
        table = ensure_final_table(
            conn,
            campaign_code=str(issue["campaign_code"]),
            campaign_version=1,
            starts_at=issue_start + timedelta(minutes=5, seconds=14),
            questions=[{"id": "final-1", "time_limit_seconds": 30}],
        )
        repeated = ensure_final_table(
            conn,
            campaign_code=str(issue["campaign_code"]),
            campaign_version=1,
            starts_at=issue_start + timedelta(minutes=5, seconds=14),
            questions=[{"id": "final-1", "time_limit_seconds": 30}],
        )

    assert table["prize_type"] == "reward_card"
    assert int(table["prize_catalog_reward_id"]) == int(reward["id"])
    assert repeated["prize_type"] == "reward_card"
    assert int(repeated["prize_catalog_reward_id"]) == int(reward["id"])


def test_card_goes_only_to_first_correct_final_winner_and_keeps_414_jc(tmp_path) -> None:
    db_path = tmp_path / "winner-card-first-correct.sqlite3"
    init_db(db_path)
    start = datetime(2026, 8, 25, 18, 23, 14, tzinfo=timezone.utc)
    campaign = "jackside_winner_card_first_correct"

    with transaction(db_path) as conn:
        # Production installs the prelaunch schema before the economy-compat
        # triggers. Reproduce that real startup order in this isolated DB.
        ensure_prelaunch_schema(conn)
        ensure_prelaunch_economy_compat(conn)
        reward = _catalog(conn)
        late_client, _ = _candidate(conn, campaign=campaign, number=1)
        early_client, _ = _candidate(conn, campaign=campaign, number=2)
        table = ensure_final_table(
            conn,
            campaign_code=campaign,
            campaign_version=1,
            starts_at=start,
            questions=[{"id": "final-1", "time_limit_seconds": 30}],
            prize_type="reward_card",
            prize_catalog_reward_id=int(reward["id"]),
        )
        reconcile_final_table(conn, final_table_id=int(table["id"]), now=start)
        finalists = conn.execute(
            """
            SELECT id,client_id FROM daily_414_finalists
            WHERE final_table_id=? ORDER BY seed
            """,
            (int(table["id"]),),
        ).fetchall()
        finalist_by_client = {int(row["client_id"]): int(row["id"]) for row in finalists}

        conn.execute(
            """
            INSERT INTO daily_414_final_answers(
                final_table_id,finalist_id,question_index,question_code,
                answer_json,is_correct,response_time_ms,answered_at
            ) VALUES (?, ?, 0, 'final-1', '"yes"', 1, 4000, ?)
            """,
            (
                int(table["id"]),
                finalist_by_client[late_client],
                (start + timedelta(seconds=4)).isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO daily_414_final_answers(
                final_table_id,finalist_id,question_index,question_code,
                answer_json,is_correct,response_time_ms,answered_at
            ) VALUES (?, ?, 0, 'final-1', '"yes"', 1, 2000, ?)
            """,
            (
                int(table["id"]),
                finalist_by_client[early_client],
                (start + timedelta(seconds=2)).isoformat(),
            ),
        )

        completed = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start + timedelta(seconds=30),
        )
        winners = list_final_winners(conn, final_table_id=int(table["id"]))
        card = attach_final_table_reward(conn, final_table_id=int(table["id"]))
        table_after_reward = conn.execute(
            "SELECT * FROM daily_414_final_tables WHERE id=?",
            (int(table["id"]),),
        ).fetchone()
        early_jc = jackcoin_balance(conn, early_client)
        late_jc = jackcoin_balance(conn, late_client)
        card_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM vault_member_rewards WHERE source_type='final_prize'"
            ).fetchone()[0]
        )
        active_before_burn = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM vault_member_rewards
                WHERE client_id=? AND status='active'
                """,
                (early_client,),
            ).fetchone()[0]
        )

        activation_time = datetime.now(timezone.utc) + timedelta(seconds=1)
        activated = activate_reward(
            conn,
            reward_id=int(card["id"]),
            client_id=early_client,
            activation_minutes=10,
            now=activation_time,
        )
        burned = redeem_reward(
            conn,
            code=str(activated["activation_code"]),
            admin_id=1,
            admin_name="Winner Delivery Test",
            now=activation_time + timedelta(seconds=1),
        )
        active_after_burn = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM vault_member_rewards
                WHERE client_id=? AND status='active'
                """,
                (early_client,),
            ).fetchone()[0]
        )
        redeemed_event_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM vault_reward_events
                WHERE member_reward_id=? AND action='redeemed'
                """,
                (int(card["id"]),),
            ).fetchone()[0]
        )

    assert completed["status"] == "completed"
    assert completed["outcome"] == "single_winner"
    assert len(winners) == 1
    assert int(winners[0]["client_id"]) == early_client
    assert card is not None
    assert int(card["client_id"]) == early_client
    assert card["source_type"] == "final_prize"
    assert int(card["price_paid_jc"]) == 0
    assert int(table_after_reward["winner_reward_id"]) == int(card["id"])
    assert card_count == 1
    assert early_jc == 414
    assert late_jc < 414
    assert active_before_burn == 1
    assert burned["status"] == "redeemed"
    assert active_after_burn == 0
    assert redeemed_event_count == 1


def test_configured_hidden_card_is_not_issued_when_final_has_no_winner(tmp_path) -> None:
    db_path = tmp_path / "winner-card-no-winner.sqlite3"
    init_db(db_path)
    start = datetime(2026, 8, 25, 18, 23, 14, tzinfo=timezone.utc)
    campaign = "jackside_winner_card_no_winner"

    with transaction(db_path) as conn:
        reward = _catalog(conn)
        _candidate(conn, campaign=campaign, number=1)
        _candidate(conn, campaign=campaign, number=2)
        table = ensure_final_table(
            conn,
            campaign_code=campaign,
            campaign_version=1,
            starts_at=start,
            questions=[{"id": "final-1", "time_limit_seconds": 30}],
            prize_type="reward_card",
            prize_catalog_reward_id=int(reward["id"]),
        )
        reconcile_final_table(conn, final_table_id=int(table["id"]), now=start)
        completed = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start + timedelta(seconds=30),
        )
        card = attach_final_table_reward(conn, final_table_id=int(table["id"]))
        count = int(conn.execute("SELECT COUNT(*) FROM vault_member_rewards").fetchone()[0])

    assert completed["status"] == "completed"
    assert completed["outcome"] == "no_winner"
    assert completed["winner_submission_id"] is None
    assert card is None
    assert count == 0
