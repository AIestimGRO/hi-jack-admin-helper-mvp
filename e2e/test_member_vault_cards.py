from __future__ import annotations

from playwright.sync_api import Page, expect

from app.db import transaction
from app.services.vault import create_catalog_reward, issue_reward
from test_jackside_welcome import jackside_server  # noqa: F401


def test_hidden_reward_refreshes_into_my_cards_and_remains_usable(
    page: Page, jackside_server: dict
) -> None:
    base_url = jackside_server["base_url"]
    db_path = jackside_server["db_path"]

    page.goto(
        f"{base_url}/account?tab=vault&store=market",
        wait_until="domcontentloaded",
    )
    expect(page.locator('[data-store-link="market"]')).to_have_class("active")
    expect(page.locator('[data-store-link="cards"]')).to_be_visible()

    with transaction(db_path) as conn:
        account = conn.execute(
            """
            SELECT ma.client_id
            FROM member_accounts ma
            WHERE ma.email_normalized='e2e@load.test'
            """
        ).fetchone()
        admin = conn.execute(
            "SELECT id FROM admins WHERE role='master_admin' ORDER BY id LIMIT 1"
        ).fetchone()
        assert account is not None
        assert admin is not None
        catalog = create_catalog_reward(
            conn,
            code="e2e_hidden_free_reentry",
            title="FreeReEntry",
            description="Hidden JACKSIDE prize",
            category="club",
            price_jc=1500,
            validity_days=0,
            inventory_total=None,
            redeem_instructions="Show to admin",
            position=999,
            admin_id=int(admin["id"]),
        )
        catalog_id = int(catalog["id"])
        conn.execute(
            "UPDATE vault_catalog_rewards SET is_active=0 WHERE id=?",
            (catalog_id,),
        )
        reward = issue_reward(
            conn,
            client_id=int(account["client_id"]),
            catalog_reward_id=catalog_id,
            source_type="final_prize",
            source_id="e2e-final-table",
            idempotency_key="e2e:hidden-final-prize",
            price_paid_jc=0,
            enforce_active=False,
        )
        reward_id = int(reward["id"])

    page.locator('[data-store-link="cards"]').click()
    expect(page).to_have_url(f"{base_url}/account?tab=vault&store=cards")

    card = page.locator(f'[data-member-reward-id="{reward_id}"]')
    expect(card).to_be_visible()
    expect(card).to_contain_text("FreeReEntry")
    expect(card).to_contain_text("ГЛАВНЫЙ ПРИЗ")
    expect(page.locator('[data-store-link="cards"]')).to_have_class("active")

    card.locator(".reward-activate-button").click()
    expect(page).to_have_url(
        f"{base_url}/account?tab=vault&store=cards#card-{reward_id}"
    )
    card = page.locator(f'[data-member-reward-id="{reward_id}"]')
    expect(card).to_be_visible()
    expect(card.locator(".reward-activation-code")).to_be_visible()
    expect(card.locator(".jack-card-qr")).to_be_visible()

    page.locator('[data-store-link="market"]').click()
    expect(page).to_have_url(f"{base_url}/account?tab=vault&store=market")
    expect(page.locator('[data-store-link="market"]')).to_have_class("active")
    expect(page.locator(".vault-catalog-grid")).to_be_visible()
    expect(page.locator(f'[data-member-reward-id="{reward_id}"]')).to_be_hidden()

    with transaction(db_path) as conn:
        catalog_row = conn.execute(
            "SELECT is_active FROM vault_catalog_rewards WHERE id=?",
            (catalog_id,),
        ).fetchone()
        member_reward = conn.execute(
            "SELECT status,activated_at FROM vault_member_rewards WHERE id=?",
            (reward_id,),
        ).fetchone()
        assert catalog_row is not None and int(catalog_row["is_active"]) == 0
        assert member_reward is not None
        assert member_reward["status"] == "active"
        assert member_reward["activated_at"] is not None
