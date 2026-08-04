import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import connect, init_db, transaction
from app.main import create_app
from app.services.member_accounts import (
    MEMBER_COOKIE_NAME,
    hash_password,
    issue_session,
    jackcoin_balance,
)
from app.services.reward_animations import (
    REWARD_ANIMATION_BY_KEY,
    validate_animation_upload,
)
from app.services.vault import (
    activate_reward,
    attach_final_table_reward,
    cancel_reward,
    create_catalog_reward,
    expire_activations,
    expire_rewards,
    purchase_reward,
    purchase_token,
    redeem_reward,
    valid_purchase_token,
)


def _client(conn, number: int) -> int:
    return int(
        conn.execute(
            "INSERT INTO clients(first_name, source) VALUES (?, 'test')",
            (f"Игрок {number}",),
        ).lastrowid
    )


def _catalog(
    conn,
    *,
    code: str = "free_entry",
    price: int = 1_000,
    inventory: int | None = None,
    validity_days: int = 30,
):
    conn.execute(
        """
        INSERT OR IGNORE INTO admins(
            id, username, display_name, pin_hash, role
        ) VALUES (1, 'vault-test', 'Vault Test', 'test', 'master_admin')
        """
    )
    return create_catalog_reward(
        conn,
        code=code,
        title="FREE ENTRY",
        description="Вход на один турнир клуба",
        category="entry",
        price_jc=price,
        validity_days=validity_days,
        inventory_total=inventory,
        redeem_instructions="Покажите карту администратору",
        position=10,
        admin_id=1,
    )


def _award_jackcoin(conn, client_id: int, amount: int) -> None:
    conn.execute(
        """
        INSERT INTO jackcoin_ledger(
            client_id, amount, operation_type, source_type,
            idempotency_key, comment
        ) VALUES (?, ?, 'test_award', 'test', ?, 'Тестовое начисление')
        """,
        (client_id, amount, f"test-award:{client_id}:{amount}"),
    )


def test_purchase_is_atomic_and_idempotent(tmp_path) -> None:
    db_path = tmp_path / "vault-purchase.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        client_id = _client(conn, 1)
        catalog = _catalog(conn, price=1_000, inventory=3)
        _award_jackcoin(conn, client_id, 1_200)
        first = purchase_reward(
            conn,
            client_id=client_id,
            catalog_reward_id=int(catalog["id"]),
            purchase_id="signed-request-1",
        )
        repeated = purchase_reward(
            conn,
            client_id=client_id,
            catalog_reward_id=int(catalog["id"]),
            purchase_id="signed-request-1",
        )

        assert first["id"] == repeated["id"]
        assert first["status"] == "active"
        assert jackcoin_balance(conn, client_id) == 200
        assert conn.execute(
            "SELECT COUNT(*) FROM vault_member_rewards"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM jackcoin_ledger WHERE operation_type='vault_purchase'"
        ).fetchone()[0] == 1


def test_purchase_rejects_insufficient_balance_without_partial_card(tmp_path) -> None:
    db_path = tmp_path / "vault-insufficient.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        client_id = _client(conn, 1)
        catalog = _catalog(conn, price=1_000)
        _award_jackcoin(conn, client_id, 999)
        with pytest.raises(ValueError, match="insufficient_jackcoin"):
            purchase_reward(
                conn,
                client_id=client_id,
                catalog_reward_id=int(catalog["id"]),
                purchase_id="signed-request-2",
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM vault_member_rewards"
        ).fetchone()[0] == 0
        assert jackcoin_balance(conn, client_id) == 999


def test_purchase_rejects_price_changed_after_catalog_was_opened(tmp_path) -> None:
    db_path = tmp_path / "vault-price-change.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        client_id = _client(conn, 1)
        catalog = _catalog(conn, price=350)
        _award_jackcoin(conn, client_id, 500)
        conn.execute(
            "UPDATE vault_catalog_rewards SET price_jc=400 WHERE id=?",
            (catalog["id"],),
        )
        with pytest.raises(ValueError, match="catalog_reward_price_changed"):
            purchase_reward(
                conn,
                client_id=client_id,
                catalog_reward_id=int(catalog["id"]),
                purchase_id="old-price-token",
                expected_price_jc=350,
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM vault_member_rewards"
        ).fetchone()[0] == 0
        assert jackcoin_balance(conn, client_id) == 500


def test_inventory_and_cancellation_refund_are_transactional(tmp_path) -> None:
    db_path = tmp_path / "vault-inventory.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        first_client = _client(conn, 1)
        second_client = _client(conn, 2)
        catalog = _catalog(conn, price=500, inventory=1)
        _award_jackcoin(conn, first_client, 500)
        _award_jackcoin(conn, second_client, 500)
        first = purchase_reward(
            conn,
            client_id=first_client,
            catalog_reward_id=int(catalog["id"]),
            purchase_id="inventory-1",
        )
        with pytest.raises(ValueError, match="catalog_reward_sold_out"):
            purchase_reward(
                conn,
                client_id=second_client,
                catalog_reward_id=int(catalog["id"]),
                purchase_id="inventory-2",
            )
        cancelled = cancel_reward(
            conn,
            reward_id=int(first["id"]),
            admin_id=1,
            admin_name="Master",
        )
        second = purchase_reward(
            conn,
            client_id=second_client,
            catalog_reward_id=int(catalog["id"]),
            purchase_id="inventory-2",
        )

        assert cancelled["status"] == "cancelled"
        assert jackcoin_balance(conn, first_client) == 500
        assert second["status"] == "active"
        assert jackcoin_balance(conn, second_client) == 0


def test_redeem_is_one_time_and_expiry_is_recorded(tmp_path) -> None:
    db_path = tmp_path / "vault-redeem.sqlite3"
    init_db(db_path)
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    with transaction(db_path) as conn:
        client_id = _client(conn, 1)
        other_client_id = _client(conn, 2)
        catalog = _catalog(conn, price=0, validity_days=1)
        active = purchase_reward(
            conn,
            client_id=client_id,
            catalog_reward_id=int(catalog["id"]),
            purchase_id="redeem-1",
            now=now,
        )
        with pytest.raises(ValueError, match="vault_reward_not_activated"):
            redeem_reward(
                conn,
                code=active["code"],
                admin_id=1,
                admin_name="Admin",
                now=now + timedelta(minutes=30),
            )
        with pytest.raises(ValueError, match="vault_reward_not_found"):
            activate_reward(
                conn,
                reward_id=int(active["id"]),
                client_id=other_client_id,
                now=now + timedelta(minutes=40),
            )
        activated = activate_reward(
            conn,
            reward_id=int(active["id"]),
            client_id=client_id,
            now=now + timedelta(minutes=45),
        )
        assert re.fullmatch(r"\d{4}", activated["activation_code"])
        assert activate_reward(
            conn,
            reward_id=int(active["id"]),
            client_id=client_id,
            now=now + timedelta(minutes=50),
        )["activation_code"] == activated["activation_code"]
        assert conn.execute(
            """
            SELECT COUNT(*) FROM vault_reward_events
            WHERE member_reward_id=? AND action='activated'
            """,
            (active["id"],),
        ).fetchone()[0] == 1
        redeemed = redeem_reward(
            conn,
            code=activated["activation_code"],
            admin_id=1,
            admin_name="Admin",
            now=now + timedelta(minutes=54),
        )
        assert redeemed["status"] == "redeemed"
        with pytest.raises(ValueError, match="vault_reward_redeemed"):
            redeem_reward(
                conn,
                code=active["code"],
                admin_id=1,
                admin_name="Admin",
                now=now + timedelta(hours=2),
            )

        qr_reward = purchase_reward(
            conn,
            client_id=client_id,
            catalog_reward_id=int(catalog["id"]),
            purchase_id="redeem-by-qr",
            now=now,
        )
        activate_reward(
            conn,
            reward_id=int(qr_reward["id"]),
            client_id=client_id,
            now=now + timedelta(minutes=10),
        )
        assert redeem_reward(
            conn,
            code=qr_reward["code"],
            admin_id=1,
            admin_name="Admin",
            now=now + timedelta(minutes=19),
        )["status"] == "redeemed"

        expiring = purchase_reward(
            conn,
            client_id=client_id,
            catalog_reward_id=int(catalog["id"]),
            purchase_id="expire-1",
            now=now,
        )
        assert expire_rewards(
            conn, now=now + timedelta(days=2), client_id=client_id
        ) == 1
        assert conn.execute(
            "SELECT status FROM vault_member_rewards WHERE id=?", (expiring["id"],)
        ).fetchone()[0] == "expired"


def test_activation_window_expires_without_consuming_reward(tmp_path) -> None:
    db_path = tmp_path / "vault-activation-window.sqlite3"
    init_db(db_path)
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    with transaction(db_path) as conn:
        client_id = _client(conn, 1)
        catalog = _catalog(conn, price=0)
        reward = purchase_reward(
            conn,
            client_id=client_id,
            catalog_reward_id=int(catalog["id"]),
            purchase_id="activation-window",
            now=now,
        )
        activated = activate_reward(
            conn,
            reward_id=int(reward["id"]),
            client_id=client_id,
            activation_minutes=10,
            now=now,
        )
        first_code = activated["activation_code"]
        assert activated["activation_expires_at"] == (
            now + timedelta(minutes=10)
        ).isoformat(timespec="seconds")
        assert expire_activations(
            conn, client_id=client_id, now=now + timedelta(minutes=10)
        ) == 1
        reset = conn.execute(
            "SELECT * FROM vault_member_rewards WHERE id=?", (reward["id"],)
        ).fetchone()
        assert reset["status"] == "active"
        assert reset["activation_code"] is None
        assert reset["activated_at"] is None
        assert reset["activation_expires_at"] is None
        with pytest.raises(ValueError, match="vault_reward_not_found"):
            redeem_reward(
                conn,
                code=first_code,
                admin_id=1,
                admin_name="Admin",
                now=now + timedelta(minutes=10),
            )
        reactivated = activate_reward(
            conn,
            reward_id=int(reward["id"]),
            client_id=client_id,
            activation_minutes=10,
            now=now + timedelta(minutes=11),
        )
        assert reactivated["activation_code"]
        assert reactivated["activation_expires_at"] == (
            now + timedelta(minutes=21)
        ).isoformat(timespec="seconds")
        assert conn.execute(
            """
            SELECT COUNT(*) FROM vault_reward_events
            WHERE member_reward_id=? AND action='activation_expired'
            """,
            (reward["id"],),
        ).fetchone()[0] == 1


def test_final_table_prize_is_issued_once(tmp_path) -> None:
    db_path = tmp_path / "vault-final-prize.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        client_id = _client(conn, 1)
        catalog = _catalog(conn, code="final_prize", price=1_200, inventory=1)
        conn.execute(
            """
            INSERT INTO quiz_campaigns(
                code, title, campaign_type, final_prize_catalog_reward_id
            ) VALUES ('daily_final_prize', '4:14 финал', 'daily_414', ?)
            """,
            (catalog["id"],),
        )
        submission_id = int(
            conn.execute(
                """
                INSERT INTO quiz_submissions(
                    campaign_code, client_id, phone_raw, phone_local,
                    answers_json, ip_hash
                ) VALUES ('daily_final_prize', ?, '', '', '{}', 'test')
                """,
                (client_id,),
            ).lastrowid
        )
        table_id = int(
            conn.execute(
                """
                INSERT INTO daily_414_final_tables(
                    campaign_code, campaign_version, starts_at,
                    questions_snapshot_json, prize_catalog_reward_id,
                    status, winner_submission_id,
                    completed_at
                ) VALUES (
                    'daily_final_prize', 1, '2026-08-03T18:23:14+00:00',
                    '[]', ?, 'completed', ?, CURRENT_TIMESTAMP
                )
                """,
                (catalog["id"], submission_id),
            ).lastrowid
        )
        replacement = _catalog(
            conn, code="replacement_prize", price=600, inventory=1
        )
        conn.execute(
            """
            UPDATE quiz_campaigns SET final_prize_catalog_reward_id=?
            WHERE code='daily_final_prize'
            """,
            (replacement["id"],),
        )
        first = attach_final_table_reward(conn, final_table_id=table_id)
        repeated = attach_final_table_reward(conn, final_table_id=table_id)

        assert first is not None
        assert first["id"] == repeated["id"]
        assert first["source_type"] == "final_prize"
        assert first["catalog_reward_id"] == catalog["id"]
        assert first["price_paid_jc"] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM vault_member_rewards"
        ).fetchone()[0] == 1


def test_final_table_can_award_jackcoin_once(tmp_path) -> None:
    db_path = tmp_path / "vault-final-jackcoin.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        client_id = _client(conn, 1)
        submission_id = int(
            conn.execute(
                """
                INSERT INTO quiz_submissions(
                    campaign_code, client_id, phone_raw, phone_local,
                    answers_json, ip_hash
                ) VALUES ('daily_final_jc', ?, '', '', '{}', 'test')
                """,
                (client_id,),
            ).lastrowid
        )
        table_id = int(
            conn.execute(
                """
                INSERT INTO daily_414_final_tables(
                    campaign_code, campaign_version, starts_at,
                    questions_snapshot_json, prize_type,
                    prize_jackcoin_amount, status, winner_submission_id,
                    completed_at
                ) VALUES (
                    'daily_final_jc', 1, '2026-08-03T18:23:14+00:00',
                    '[]', 'jackcoin', 750, 'completed', ?, CURRENT_TIMESTAMP
                )
                """,
                (submission_id,),
            ).lastrowid
        )

        first = attach_final_table_reward(conn, final_table_id=table_id)
        repeated = attach_final_table_reward(conn, final_table_id=table_id)

        assert first == {"kind": "jackcoin", "amount": 750}
        assert repeated == first
        assert jackcoin_balance(conn, client_id) == 750
        assert conn.execute(
            "SELECT COUNT(*) FROM jackcoin_ledger WHERE source_type='final_prize'"
        ).fetchone()[0] == 1
        table = conn.execute(
            "SELECT * FROM daily_414_final_tables WHERE id=?", (table_id,)
        ).fetchone()
        assert table["winner_jackcoin_awarded"] == 750


def test_purchase_token_is_bound_to_account_and_reward() -> None:
    token = purchase_token(
        "s" * 32,
        account_id=7,
        catalog_reward_id=11,
        price_jc=350,
        nonce="fixed",
    )
    assert valid_purchase_token(
        "s" * 32, token, account_id=7, catalog_reward_id=11, price_jc=350
    )
    assert not valid_purchase_token(
        "s" * 32, token, account_id=8, catalog_reward_id=11, price_jc=350
    )
    assert not valid_purchase_token(
        "s" * 32, token, account_id=7, catalog_reward_id=12, price_jc=350
    )
    assert not valid_purchase_token(
        "s" * 32, token, account_id=7, catalog_reward_id=11, price_jc=400
    )


def test_vault_tables_are_added_without_touching_existing_jackcoin(tmp_path) -> None:
    db_path = tmp_path / "vault-migration.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        client_id = _client(conn, 1)
        _award_jackcoin(conn, client_id, 321)
    init_db(db_path)
    with connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "vault_catalog_rewards",
            "vault_member_rewards",
            "vault_reward_events",
        }.issubset(tables)
        assert jackcoin_balance(conn, client_id) == 321


def test_activation_expiry_migration_resets_preexisting_codes_safely(tmp_path) -> None:
    db_path = tmp_path / "vault-activation-migration.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        client_id = _client(conn, 1)
        catalog = _catalog(conn, price=0)
        reward = purchase_reward(
            conn,
            client_id=client_id,
            catalog_reward_id=int(catalog["id"]),
            purchase_id="activation-migration",
        )
        activated = activate_reward(
            conn,
            reward_id=int(reward["id"]),
            client_id=client_id,
        )
        activated_at = activated["activated_at"]
        conn.execute(
            "ALTER TABLE vault_member_rewards DROP COLUMN activation_expires_at"
        )

    init_db(db_path)
    with connect(db_path) as conn:
        migrated = conn.execute(
            "SELECT * FROM vault_member_rewards WHERE id=?", (reward["id"],)
        ).fetchone()
        assert migrated["activation_code"]
        assert migrated["activation_expires_at"] == activated_at
        assert expire_activations(conn) == 1
        reset = conn.execute(
            "SELECT * FROM vault_member_rewards WHERE id=?", (reward["id"],)
        ).fetchone()
        assert reset["status"] == "active"
        assert reset["activation_code"] is None


def _csrf(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def test_vault_admin_member_qr_and_redeem_flow(tmp_path, monkeypatch) -> None:
    settings = Settings(
        admin_pin="2468",
        admin_name="Vault Master",
        secret_key="vault-route-secret-key-that-is-longer-than-32-characters",
        db_path=tmp_path / "vault-routes.sqlite3",
        secure_cookie=False,
        member_portal_enabled=True,
        public_base_url="https://club.example.test",
        quiz_public_base_url="https://quiz.example.test",
    )
    client = TestClient(create_app(settings), base_url=settings.public_base_url)
    captured: dict[str, str] = {}

    class FakeQr:
        def save(self, output, format):
            output.write(b"vault-qr")

    def fake_qr(value, **kwargs):
        captured["value"] = value
        return FakeQr()

    monkeypatch.setattr("app.main_impl.qrcode.make", fake_qr)
    with client:
        with transaction(settings.db_path) as conn:
            member_client_id = _client(conn, 1)
            account_id = int(
                conn.execute(
                    """
                    INSERT INTO member_accounts(
                        client_id, email, email_normalized, password_hash,
                        email_verified_at
                    ) VALUES (?, 'vault@example.test', 'vault@example.test', ?,
                              CURRENT_TIMESTAMP)
                    """,
                    (member_client_id, hash_password("abcdef")),
                ).lastrowid
            )
            for document in conn.execute(
                "SELECT * FROM legal_documents WHERE is_active=1"
            ).fetchall():
                conn.execute(
                    """
                    INSERT INTO member_consents(
                        account_id, document_id, document_code,
                        document_version, ip_hash, user_agent
                    ) VALUES (?, ?, ?, ?, 'test', 'pytest')
                    """,
                    (
                        account_id,
                        document["id"],
                        document["code"],
                        document["version"],
                    ),
                )
            _award_jackcoin(conn, member_client_id, 500)
            member_token = issue_session(
                conn,
                secret_key=settings.secret_key,
                account_id=account_id,
                session_version=1,
                days=30,
                ip_hash="test",
                user_agent="pytest",
            )
        client.cookies.set(MEMBER_COOKIE_NAME, member_token)

        login_page = client.get("/login")
        login = client.post(
            "/login",
            data={
                "username": "master",
                "pin": "2468",
                "csrf_token": _csrf(login_page),
            },
            follow_redirects=False,
        )
        assert login.status_code == 303
        vault_page = client.get("/admin/vault")
        assert vault_page.status_code == 200
        assert "THE VAULT" in vault_page.text
        created = client.post(
            "/api/vault/catalog/create",
            data={
                "code": "coffee",
                "title": "Капучино",
                "category": "drink",
                "price_jc": "350",
                "validity_days": "7",
                "inventory_total": "2",
                "position": "10",
                "animation_choice": "coffee_cup",
                "csrf_token": _csrf(vault_page),
            },
            follow_redirects=False,
        )
        assert created.status_code == 303

        campaigns_page = client.get("/master?tab=campaigns")
        campaign_created = client.post(
            "/api/master/quiz-campaigns/create",
            data={
                "code": "vault_daily",
                "title": "4:14 с главным призом",
                "campaign_type": "daily_414",
                "final_question_time_seconds": "45",
                "final_prize_type": "reward_card",
                "final_prize_catalog_reward_id": "1",
                "csrf_token": _csrf(campaigns_page),
            },
            follow_redirects=False,
        )
        assert campaign_created.status_code == 303
        with connect(settings.db_path) as conn:
            campaign = conn.execute(
                """
                SELECT final_question_time_seconds, final_prize_type,
                       final_prize_catalog_reward_id
                FROM quiz_campaigns WHERE code='vault_daily'
                """
            ).fetchone()
            assert campaign
            assert campaign["final_question_time_seconds"] == 45
            assert campaign["final_prize_type"] == "reward_card"
            assert campaign["final_prize_catalog_reward_id"] == 1

        member_page = client.get("/account?tab=rewards")
        assert member_page.status_code == 200
        assert "Капучино" in member_page.text
        assert (
            'data-reward-animation-src="/static/animations/rewards/coffee_cup.json"'
            in member_page.text
        )
        vault_page = client.get("/admin/vault")
        updated = client.post(
            "/api/vault/catalog/1/update",
            data={
                "title": "Капучино",
                "description": "Кофе в клубе",
                "category": "drink",
                "price_jc": "350",
                "validity_days": "7",
                "inventory_total": "2",
                "position": "10",
                "is_active": "true",
                "animation_choice": "coffee_cup",
                "csrf_token": _csrf(vault_page),
            },
            files={
                "animation_file": (
                    "coffee.gif",
                    b"GIF89a-reward-animation",
                    "image/gif",
                )
            },
            follow_redirects=False,
        )
        assert updated.status_code == 303
        with connect(settings.db_path) as conn:
            uploaded = conn.execute(
                "SELECT animation_key, animation_path, animation_mime FROM vault_catalog_rewards WHERE id=1"
            ).fetchone()
            assert uploaded["animation_key"] is None
            assert uploaded["animation_path"].startswith("/reward-media/reward-")
            assert uploaded["animation_mime"] == "image/gif"
        media = client.get(uploaded["animation_path"])
        assert media.status_code == 200
        assert media.content == b"GIF89a-reward-animation"
        member_page = client.get("/account?tab=rewards")
        assert f'src="{uploaded["animation_path"]}"' in member_page.text
        purchase_match = re.search(
            r'name="purchase_id" value="([^"]+)"', member_page.text
        )
        assert purchase_match
        purchase = client.post(
            "/account/rewards/1/purchase",
            data={
                "purchase_id": purchase_match.group(1),
                "expected_price_jc": "350",
                "csrf_token": _csrf(member_page),
            },
            follow_redirects=False,
        )
        assert purchase.status_code == 303
        assert "tab=vault" in purchase.headers["location"]

        active_page = client.get("/account?tab=rewards")
        assert "Активировать" in active_page.text
        with connect(settings.db_path) as conn:
            card = conn.execute("SELECT * FROM vault_member_rewards").fetchone()
            assert jackcoin_balance(conn, member_client_id) == 150
            assert card["activation_code"] is None
        assert card["code"] not in active_page.text
        blocked_qr = client.get(f"/account/rewards/{card['id']}/qr.png")
        assert blocked_qr.status_code == 409

        activated = client.post(
            f"/account/rewards/{card['id']}/activate",
            data={"csrf_token": _csrf(active_page)},
            follow_redirects=False,
        )
        assert activated.status_code == 303
        assert "tab=vault" in activated.headers["location"]
        with connect(settings.db_path) as conn:
            card = conn.execute("SELECT * FROM vault_member_rewards").fetchone()
            assert re.fullmatch(r"\d{4}", card["activation_code"])
            assert card["activated_at"]
            assert card["activation_expires_at"]
        activated_page = client.get("/account?tab=vault")
        assert card["activation_code"] in activated_page.text
        assert card["code"] not in activated_page.text
        assert 'data-reward-activation-countdown=' in activated_page.text
        qr = client.get(f"/account/rewards/{card['id']}/qr.png")
        assert qr.status_code == 200
        assert qr.content == b"vault-qr"
        assert captured["value"].endswith(f"/admin/vault?code={card['code']}")

        redeem_page = client.get(f"/admin/vault?code={card['activation_code']}")
        assert card["activation_code"] in redeem_page.text
        redeemed = client.post(
            "/api/vault/redeem",
            data={
                "code": card["activation_code"],
                "csrf_token": _csrf(redeem_page),
            },
            follow_redirects=False,
        )
        assert redeemed.status_code == 303
        with connect(settings.db_path) as conn:
            assert conn.execute(
                "SELECT status FROM vault_member_rewards WHERE id=?", (card["id"],)
            ).fetchone()[0] == "redeemed"


def test_reward_animation_library_and_upload_validation() -> None:
    assert {
        "casino_chips",
        "royal_cards",
        "lucky_crown",
        "champion_cup",
        "winner_badge",
        "premium_gem",
        "laurel_star",
        "jackcoin_stack",
        "coffee_cup",
        "club_cocktail",
    } == set(REWARD_ANIMATION_BY_KEY)
    content = (
        Path(__file__).resolve().parents[1]
        / "app/static/animations/rewards/casino_chips.json"
    ).read_bytes()
    assert validate_animation_upload("chip.json", content) == (
        ".json",
        "application/json",
    )
    assert validate_animation_upload("spark.gif", b"GIF89a-reward") == (
        ".gif",
        "image/gif",
    )
    with pytest.raises(ValueError, match="invalid_animation_file"):
        validate_animation_upload(
            "remote.json",
            b'{"v":"5","fr":30,"ip":0,"op":30,"w":512,"h":512,'
            b'"layers":[{"ref":"https://example.test/a.png"}]}',
        )


def test_reward_animation_columns_migrate_without_changing_existing_reward(
    tmp_path,
) -> None:
    db_path = tmp_path / "vault-animation-migration.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        reward = _catalog(conn)
        conn.execute("ALTER TABLE vault_catalog_rewards DROP COLUMN animation_key")
        conn.execute("ALTER TABLE vault_catalog_rewards DROP COLUMN animation_path")
        conn.execute("ALTER TABLE vault_catalog_rewards DROP COLUMN animation_mime")
    init_db(db_path)
    with connect(db_path) as conn:
        migrated = conn.execute(
            "SELECT * FROM vault_catalog_rewards WHERE id=?", (reward["id"],)
        ).fetchone()
        assert migrated["title"] == "FREE ENTRY"
        assert migrated["animation_key"] is None
        assert migrated["animation_path"] is None
        assert migrated["animation_mime"] is None
