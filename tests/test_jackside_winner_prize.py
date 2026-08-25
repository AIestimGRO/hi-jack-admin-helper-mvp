from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.db import init_db, transaction
from app.jackside_multi_issue import create_issue_multi
from app.jackside_winner_prize import (
    set_future_winner_card_prize,
    winner_prize_payload,
)
from app.services import jackside_issues as issue_service
from app.services.jackside_issues import ensure_issue_campaign
from app.services.vault import (
    attach_final_table_reward,
    create_catalog_reward,
    purchase_reward,
)


MOSCOW = ZoneInfo("Europe/Moscow")
ROOT = Path(__file__).resolve().parents[1]


def _catalog(conn, *, code: str = "free_reentry", active: bool = True):
    conn.execute(
        """
        INSERT OR IGNORE INTO admins(
            id,username,display_name,pin_hash,role
        ) VALUES (1,'winner-prize-test','Winner Prize Test','test','master_admin')
        """
    )
    reward = create_catalog_reward(
        conn,
        code=code,
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
    if not active:
        conn.execute(
            "UPDATE vault_catalog_rewards SET is_active=0 WHERE id=?",
            (int(reward["id"]),),
        )
    return reward


def test_compact_winner_prize_setting_syncs_issue_without_touching_jackcoin(tmp_path) -> None:
    db_path = tmp_path / "winner-prize-settings.sqlite3"
    init_db(db_path)
    day = (datetime.now(MOSCOW) + timedelta(days=3)).date()
    start = datetime.combine(day, datetime.min.time(), tzinfo=MOSCOW).replace(
        hour=18, minute=14
    )
    with transaction(db_path) as conn:
        issue = create_issue_multi(
            conn,
            issue_date_value=day,
            starts_at=start,
            title="JACKSIDE prize test",
            jackcoin_per_correct=10,
            jackcoin_completion_bonus=10,
            jackcoin_perfect_bonus=30,
        )
        campaign = ensure_issue_campaign(conn, issue=issue)
        reward = _catalog(conn)
        before = conn.execute(
            """
            SELECT jackcoin_per_correct,jackcoin_completion_bonus,
                   jackcoin_perfect_bonus
            FROM quiz_campaigns WHERE id=?
            """,
            (int(campaign["id"]),),
        ).fetchone()

        updated = set_future_winner_card_prize(
            conn,
            issue_id=int(issue["id"]),
            catalog_reward_id=int(reward["id"]),
        )
        campaign_after = conn.execute(
            "SELECT * FROM quiz_campaigns WHERE id=?",
            (int(campaign["id"]),),
        ).fetchone()

    assert updated["final_prize_type"] == "reward_card"
    assert int(updated["final_prize_catalog_reward_id"]) == int(reward["id"])
    assert int(updated["final_prize_jackcoin_amount"]) == 0
    assert campaign_after["final_prize_type"] == "reward_card"
    assert int(campaign_after["final_prize_catalog_reward_id"]) == int(reward["id"])
    assert int(campaign_after["final_prize_jackcoin_amount"]) == 0
    assert tuple(before) == (
        campaign_after["jackcoin_per_correct"],
        campaign_after["jackcoin_completion_bonus"],
        campaign_after["jackcoin_perfect_bonus"],
    )


def test_hidden_market_card_can_be_winner_prize_but_cannot_be_purchased(tmp_path) -> None:
    db_path = tmp_path / "winner-prize-hidden-market.sqlite3"
    init_db(db_path)
    day = (datetime.now(MOSCOW) + timedelta(days=3)).date()
    start = datetime.combine(day, datetime.min.time(), tzinfo=MOSCOW).replace(hour=18)
    with transaction(db_path) as conn:
        issue = create_issue_multi(
            conn,
            issue_date_value=day,
            starts_at=start,
            title="JACKSIDE hidden prize",
        )
        ensure_issue_campaign(conn, issue=issue)
        reward = _catalog(conn, active=False)

        updated = set_future_winner_card_prize(
            conn,
            issue_id=int(issue["id"]),
            catalog_reward_id=int(reward["id"]),
        )
        payload = winner_prize_payload(conn, issue_id=int(issue["id"]))
        refreshed = issue_service.get_issue(conn, int(issue["id"]))
        publish_errors = issue_service.validate_issue_for_publish(conn, refreshed)

        buyer_id = int(
            conn.execute(
                "INSERT INTO clients(first_name,source) VALUES ('Buyer','test')"
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO jackcoin_ledger(
                client_id,amount,operation_type,source_type,idempotency_key,comment
            ) VALUES (?,1000,'test_award','test',?,'test')
            """,
            (buyer_id, f"hidden-prize-buyer:{buyer_id}"),
        )
        with pytest.raises(ValueError, match="catalog_reward_inactive"):
            purchase_reward(
                conn,
                client_id=buyer_id,
                catalog_reward_id=int(reward["id"]),
                purchase_id="hidden-card-direct-purchase",
                expected_price_jc=700,
            )
        purchased = int(
            conn.execute(
                "SELECT COUNT(*) FROM vault_member_rewards WHERE client_id=?",
                (buyer_id,),
            ).fetchone()[0]
        )

    assert updated["final_prize_type"] == "reward_card"
    assert int(updated["final_prize_catalog_reward_id"]) == int(reward["id"])
    assert "invalid_card_prize" not in publish_errors
    prize_row = next(item for item in payload["rewards"] if item["id"] == int(reward["id"]))
    assert prize_row["is_active"] is False
    assert purchased == 0


def test_card_prize_is_additional_to_jackcoin_and_no_winner_gets_no_card(tmp_path) -> None:
    db_path = tmp_path / "winner-prize-delivery.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        reward = _catalog(conn)
        client_id = int(
            conn.execute(
                "INSERT INTO clients(first_name,source) VALUES ('Winner','test')"
            ).lastrowid
        )
        submission_id = int(
            conn.execute(
                """
                INSERT INTO quiz_submissions(
                    campaign_code,client_id,phone_raw,phone_local,
                    answers_json,ip_hash
                ) VALUES ('jackside_prize_delivery',?,'','','{}','test')
                """,
                (client_id,),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO jackcoin_ledger(
                client_id,amount,operation_type,source_type,source_id,
                idempotency_key,comment
            ) VALUES (?,414,'earn','jackside_final_win','core-414',
                      'test:core-414','JACKSIDE core total')
            """,
            (client_id,),
        )

        no_winner_table = int(
            conn.execute(
                """
                INSERT INTO daily_414_final_tables(
                    campaign_code,campaign_version,starts_at,
                    questions_snapshot_json,prize_type,prize_catalog_reward_id,
                    status,outcome,prize_resolution,completed_at
                ) VALUES (
                    'jackside_no_winner_prize',1,'2026-08-25T18:23:14+00:00',
                    '[]','reward_card',?,'completed','no_winner','none',CURRENT_TIMESTAMP
                )
                """,
                (int(reward["id"]),),
            ).lastrowid
        )
        assert attach_final_table_reward(conn, final_table_id=no_winner_table) is None
        assert conn.execute("SELECT COUNT(*) FROM vault_member_rewards").fetchone()[0] == 0

        winner_table = int(
            conn.execute(
                """
                INSERT INTO daily_414_final_tables(
                    campaign_code,campaign_version,starts_at,
                    questions_snapshot_json,prize_type,prize_catalog_reward_id,
                    status,outcome,winner_submission_id,completed_at
                ) VALUES (
                    'jackside_prize_delivery',1,'2026-08-25T18:23:14+00:00',
                    '[]','reward_card',?,'completed','single_winner',?,CURRENT_TIMESTAMP
                )
                """,
                (int(reward["id"]), submission_id),
            ).lastrowid
        )
        card = attach_final_table_reward(conn, final_table_id=winner_table)
        jc_after = int(
            conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM jackcoin_ledger WHERE client_id=?",
                (client_id,),
            ).fetchone()[0]
        )

    assert card is not None
    assert int(card["client_id"]) == client_id
    assert card["source_type"] == "final_prize"
    assert int(card["price_paid_jc"]) == 0
    assert jc_after == 414


def test_modern_jackside_ui_has_small_prize_control_and_clear_additive_copy() -> None:
    template = (ROOT / "app/templates/admin_jackside_workspace.html").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "app/static/js/jackside-winner-prize.js").read_text(
        encoding="utf-8"
    )
    module = (ROOT / "app/jackside_winner_prize.py").read_text(encoding="utf-8")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    issue_service_source = (ROOT / "app/services/jackside_issues.py").read_text(
        encoding="utf-8"
    )
    final_service_source = (ROOT / "app/services/daily_414_final.py").read_text(
        encoding="utf-8"
    )
    outcome = (ROOT / "app/jackside_final_outcome_only.py").read_text(
        encoding="utf-8"
    )
    outcome_js = (ROOT / "app/static/js/jackside-final-outcome-only.js").read_text(
        encoding="utf-8"
    )
    outcome_css = (ROOT / "app/static/css/jackside-final-recovery.css").read_text(
        encoding="utf-8"
    )

    assert "data-winner-prize-issue" in template
    assert "data-winner-prize-dialog" in template
    assert "дополнительно к 414 JC" in template
    assert "Если победитель не определён" in template
    assert "prize_state.selected" in template
    assert "Приз ✓" in template
    assert "/winner-prize" in script
    assert "не продаётся" in script
    assert "final_prize_jackcoin_amount=0" in module
    assert "apply_jackside_winner_prize_policy" not in module
    assert "final_service.ensure_final_table =" not in module
    assert "apply_jackside_winner_prize_policy" not in main
    assert "jackcoin_per_correct" not in module
    assert "jackcoin_completion_bonus" not in module
    assert "jackcoin_perfect_bonus" not in module

    assert "WHERE id=? AND is_active=1" not in issue_service_source.split(
        "def validate_issue_for_publish", 1
    )[1].split("def issue_schedule_local", 1)[0]
    assert "_resolved_prize_snapshot" in final_service_source
    assert "FROM quiz_campaigns" in final_service_source

    assert '"superprize": superprize' in outcome
    assert "winner_reward_id" in outcome
    assert "Суперприз выпуска" in outcome
    assert "СУПЕРПРИЗ ВЫПУСКА" in outcome_js
    assert "jackside-final-superprize" in outcome_js
    assert "superprize.title" in outcome_js
    assert ".jackside-final-superprize" in outcome_css
