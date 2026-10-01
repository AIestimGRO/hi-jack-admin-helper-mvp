from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.db import init_db, transaction
from app.services import vault
from app.vault_activation_policy import (
    CARD_ACTIVATION_MINUTES,
    activate_reward,
    expire_activations,
    redeem_reward,
)
from app.vault_audit_ui import query_economy_history


ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "app" / "static" / "js" / "vault-activation-policy.js"
PERFORMANCE_CSS = ROOT / "app" / "static" / "css" / "member-performance.css"
VAULT_CSS = ROOT / "app" / "static" / "css" / "admin-vault-scanner.css"
AUDIT_MODULE = ROOT / "app" / "vault_audit_ui.py"
BASE_TEMPLATE = ROOT / "app" / "templates" / "base.html"
HISTORY_TEMPLATE = ROOT / "app" / "templates" / "economy_history.html"


def _seed_reward(conn) -> tuple[int, int]:
    conn.execute(
        """
        INSERT INTO admins(id,username,display_name,pin_hash,role)
        VALUES (1,'master-test','Master Test','x','master_admin')
        """
    )
    client_id = int(
        conn.execute(
            """
            INSERT INTO clients(
                first_name,nickname,username,phone_raw,phone_local,source
            ) VALUES ('Игрок Тест','Tester','tester','+7 999 111-22-33','9991112233','test')
            """
        ).lastrowid
    )
    catalog = vault.create_catalog_reward(
        conn,
        code="test_reward",
        title="Тестовая награда",
        description="",
        category="club",
        price_jc=0,
        validity_days=30,
        inventory_total=None,
        redeem_instructions="Покажите карту администратору",
        position=10,
        admin_id=1,
    )
    reward = vault.issue_reward(
        conn,
        client_id=client_id,
        catalog_reward_id=int(catalog["id"]),
        source_type="admin",
        source_id="test",
        idempotency_key="test:reward:1",
        admin_id=1,
        admin_name="Master Test",
    )
    reward_id = int(reward["id"])
    conn.execute(
        """
        UPDATE vault_member_rewards
        SET valid_from='2026-10-01T08:00:00+00:00',
            valid_until='2026-10-31T08:00:00+00:00'
        WHERE id=?
        """,
        (reward_id,),
    )
    return client_id, reward_id


def test_activation_is_fixed_to_15_minutes_and_timeout_consumes_card(tmp_path) -> None:
    db_path = tmp_path / "vault-timeout.sqlite3"
    init_db(db_path)
    now = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

    with transaction(db_path) as conn:
        client_id, reward_id = _seed_reward(conn)
        activated = activate_reward(
            conn,
            reward_id=reward_id,
            client_id=client_id,
            activation_minutes=3,
            now=now,
        )
        activation_code = str(activated["activation_code"])
        assert CARD_ACTIVATION_MINUTES == 15
        assert activated["activation_expires_at"] == (
            now + timedelta(minutes=15)
        ).isoformat(timespec="seconds")

        assert expire_activations(
            conn,
            client_id=client_id,
            now=now + timedelta(minutes=15),
        ) == 1
        consumed = conn.execute(
            "SELECT * FROM vault_member_rewards WHERE id=?",
            (reward_id,),
        ).fetchone()
        assert consumed["status"] == "redeemed"
        assert consumed["redeemed_at"] == consumed["activation_expires_at"]
        assert consumed["redeemed_by_admin_id"] is None
        assert consumed["activation_code"] == activation_code

        event = conn.execute(
            """
            SELECT action,admin_name,created_at FROM vault_reward_events
            WHERE member_reward_id=?
            ORDER BY id DESC LIMIT 1
            """,
            (reward_id,),
        ).fetchone()
        assert event["action"] == "activation_timeout_redeemed"
        assert event["admin_name"] == "system"
        assert event["created_at"] == consumed["activation_expires_at"]

        with pytest.raises(ValueError, match="vault_reward_redeemed"):
            activate_reward(
                conn,
                reward_id=reward_id,
                client_id=client_id,
                now=now + timedelta(minutes=16),
            )
        with pytest.raises(ValueError, match="vault_reward_redeemed"):
            redeem_reward(
                conn,
                code=activation_code,
                admin_id=1,
                admin_name="Master Test",
                now=now + timedelta(minutes=16),
            )


def test_economy_history_combines_jc_cards_local_time_and_actors(tmp_path) -> None:
    db_path = tmp_path / "vault-audit.sqlite3"
    init_db(db_path)
    now = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

    with transaction(db_path) as conn:
        client_id, reward_id = _seed_reward(conn)
        conn.execute(
            """
            INSERT INTO jackcoin_ledger(
                client_id,amount,operation_type,source_type,source_id,
                idempotency_key,comment,created_by_admin_id,created_at
            ) VALUES (?,100,'earn','admin','audit-test','audit:jc:1',
                      'Ручное начисление',1,'2026-10-01 09:00:00')
            """,
            (client_id,),
        )
        activate_reward(
            conn,
            reward_id=reward_id,
            client_id=client_id,
            now=now,
        )
        expire_activations(
            conn,
            client_id=client_id,
            now=now + timedelta(minutes=15),
        )

    history = query_economy_history(
        str(db_path),
        timezone_name="Europe/Moscow",
    )

    assert history["total"] == 4
    entries = history["entries"]
    assert any(item["created_at"] == "01.10.2026 12:00:00" for item in entries)
    assert any(item["actor"] == "Master Test" for item in entries)
    assert any(
        item["operation"] == "15 минут истекли — карта использована автоматически"
        and item["actor"] == "Система"
        for item in entries
    )
    assert any(item["value"] == "+100 JC" for item in entries)

    jc_only = query_economy_history(
        str(db_path),
        timezone_name="Europe/Moscow",
        filters={"kind": "jackcoin"},
    )
    assert jc_only["total"] == 1
    assert jc_only["entries"][0]["operation"] == "Начисление JC"

    by_name = query_economy_history(
        str(db_path),
        timezone_name="Europe/Moscow",
        filters={"client": "Игрок Тест"},
    )
    assert by_name["total"] == 4

    no_match = query_economy_history(
        str(db_path),
        timezone_name="Europe/Moscow",
        filters={"client": "Клиент Которого Нет"},
    )
    assert no_match["total"] == 0

    by_phone = query_economy_history(
        str(db_path),
        timezone_name="Europe/Moscow",
        filters={"client": "+7 (999) 111-22-33"},
    )
    assert by_phone["total"] == 4


def test_economy_history_filters_operation_actor_amount_and_page_bounds(tmp_path) -> None:
    db_path = tmp_path / "economy-filters.sqlite3"
    init_db(db_path)

    with transaction(db_path) as conn:
        client_id, _reward_id = _seed_reward(conn)
        conn.execute(
            """
            INSERT INTO jackcoin_ledger(
                client_id,amount,operation_type,source_type,source_id,
                idempotency_key,comment,created_by_admin_id,created_at
            ) VALUES (?,150,'earn','admin','one','filter:jc:1','Начисление',1,
                      '2026-10-01 09:10:00')
            """,
            (client_id,),
        )
        conn.execute(
            """
            INSERT INTO jackcoin_ledger(
                client_id,amount,operation_type,source_type,source_id,
                idempotency_key,comment,created_by_admin_id,created_at
            ) VALUES (?,-30,'spend','quiz_error_review','two','filter:jc:2','Разбор',NULL,
                      '2026-10-01 09:20:00')
            """,
            (client_id,),
        )

    credits = query_economy_history(
        str(db_path),
        timezone_name="Europe/Moscow",
        filters={"operation": "jc_credit", "jc_min": "100", "jc_max": "200"},
    )
    assert credits["total"] == 1
    assert credits["entries"][0]["value"] == "+150 JC"

    user_spend = query_economy_history(
        str(db_path),
        timezone_name="Europe/Moscow",
        filters={"actor": "__user__", "source": "quiz_error_review"},
    )
    assert user_spend["total"] == 1
    assert user_spend["entries"][0]["value"] == "-30 JC"

    clamped = query_economy_history(
        str(db_path),
        timezone_name="Europe/Moscow",
        page=999,
        page_size=10,
    )
    assert clamped["page"] == clamped["pages"] == 1
    assert clamped["entries"]


def test_member_countdown_updates_only_fixed_width_digits() -> None:
    source = ASSET.read_text(encoding="utf-8")
    css = PERFORMANCE_CSS.read_text(encoding="utf-8")

    assert "После подтверждения карта будет активна 15 минут." in source
    assert "Повторная активация будет невозможна." in source
    assert "value.dataset.rewardCountdownValue = '1';" in source
    assert "value.textContent = `${minutes}:${String(seconds).padStart(2, '0')}`;" in source
    assert "node.textContent = `Карта активна" not in source
    assert "window.clearInterval(intervalId);" in source
    assert "window.location.reload()" in source
    assert ".reward-countdown-value" in css
    assert "width: 5ch;" in css
    assert "font-variant-numeric: tabular-nums;" in css


def test_scanner_expires_activation_before_reporting_card_state() -> None:
    source = (ROOT / "app" / "admin_vault_scanner.py").read_text(encoding="utf-8")

    assert "from app.db import transaction" in source
    assert "expire_vault_activations(conn)" in source
    assert "with transaction(settings.db_path) as conn:" in source


def test_economy_history_is_separate_master_section_and_legacy_log_hidden() -> None:
    audit_source = AUDIT_MODULE.read_text(encoding="utf-8")
    base_source = BASE_TEMPLATE.read_text(encoding="utf-8")
    history_source = HISTORY_TEMPLATE.read_text(encoding="utf-8")
    vault_css = VAULT_CSS.read_text(encoding="utf-8")

    assert '@app.get("/master/economy-history"' in audit_source
    assert "_role_from_request(request) != ACCESS_MASTER" in audit_source
    assert "vault-unified-audit" not in audit_source
    assert "История экономики" in base_source
    assert 'href="/master/economy-history"' in base_source
    assert "Дата и время" in history_source
    assert "Кто выполнил" in history_source
    assert "Код JACK CARD" in history_source
    assert ".vault-events" in vault_css
    assert "display: none !important;" in vault_css
