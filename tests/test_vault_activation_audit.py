from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import init_db, transaction
from app.services import vault
from app.vault_activation_policy import (
    CARD_ACTIVATION_MINUTES,
    apply_vault_activation_policy,
)
from app.vault_audit_ui import _render_audit


ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "app" / "static" / "js" / "vault-activation-policy.js"


def _seed_reward(conn) -> tuple[int, int]:
    conn.execute(
        """
        INSERT INTO admins(id,username,display_name,pin_hash,role)
        VALUES (1,'master-test','Master Test','x','master_admin')
        """
    )
    client_id = int(
        conn.execute(
            "INSERT INTO clients(first_name,source) VALUES ('Игрок Тест','test')"
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
    return client_id, int(reward["id"])


def test_activation_is_fixed_to_15_minutes_and_timeout_consumes_card(tmp_path) -> None:
    apply_vault_activation_policy()
    db_path = tmp_path / "vault-timeout.sqlite3"
    init_db(db_path)
    now = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

    with transaction(db_path) as conn:
        client_id, reward_id = _seed_reward(conn)
        activated = vault.activate_reward(
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

        assert vault.expire_activations(
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
            SELECT action,admin_name FROM vault_reward_events
            WHERE member_reward_id=?
            ORDER BY id DESC LIMIT 1
            """,
            (reward_id,),
        ).fetchone()
        assert event["action"] == "activation_timeout_redeemed"
        assert event["admin_name"] == "system"

        with pytest.raises(ValueError, match="vault_reward_redeemed"):
            vault.activate_reward(
                conn,
                reward_id=reward_id,
                client_id=client_id,
                now=now + timedelta(minutes=16),
            )
        with pytest.raises(ValueError, match="vault_reward_redeemed"):
            vault.redeem_reward(
                conn,
                code=activation_code,
                admin_id=1,
                admin_name="Master Test",
                now=now + timedelta(minutes=16),
            )


def test_audit_renders_local_time_admin_and_system_actor(tmp_path) -> None:
    apply_vault_activation_policy()
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
        vault.activate_reward(
            conn,
            reward_id=reward_id,
            client_id=client_id,
            now=now,
        )
        vault.expire_activations(
            conn,
            client_id=client_id,
            now=now + timedelta(minutes=15),
        )

    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(db_path=db_path, timezone_name="Europe/Moscow")
        )
    )
    rendered = _render_audit(app)

    assert "Полная история JACK CARDS и JACKCOIN" in rendered
    assert "01.10.2026 12:00:00" in rendered
    assert "Master Test" in rendered
    assert "15 минут истекли — карта использована автоматически" in rendered
    assert "Система" in rendered
    assert "+100 JC" in rendered


def test_member_asset_warns_about_irreversible_15_minute_activation() -> None:
    source = ASSET.read_text(encoding="utf-8")

    assert "После подтверждения карта будет активна 15 минут." in source
    assert "Повторная активация будет невозможна." in source
    assert "form[action*=\"/account/rewards/\"][action$=\"/activate\"]" in source
    assert "node.dataset.rewardActivationCountdown" in source
    assert "window.location.reload()" in source
