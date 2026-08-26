from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import Page, expect

from app.db import transaction
from app.services.member_accounts import MEMBER_COOKIE_NAME
from app.services.vault import create_catalog_reward
from test_jackside_welcome import jackside_server as _jackside_server


jackside_server = _jackside_server


def _set_member_cookie(page: Page, server: dict) -> None:
    page.context.add_cookies(
        [
            {
                "name": MEMBER_COOKIE_NAME,
                "value": server["member_token"],
                "url": server["base_url"],
            }
        ]
    )


def _configure_hidden_free_reentry(db_path: Path) -> int:
    with transaction(db_path) as conn:
        admin = conn.execute(
            "SELECT id FROM admins WHERE role='master_admin' ORDER BY id LIMIT 1"
        ).fetchone()
        assert admin is not None
        reward = create_catalog_reward(
            conn,
            code="e2e_prize_copy_free_reentry",
            title="FreeReEntry",
            description="Hidden JACKSIDE superprize",
            category="club",
            price_jc=1500,
            validity_days=0,
            inventory_total=None,
            redeem_instructions="Show to admin",
            position=900,
            admin_id=int(admin["id"]),
        )
        catalog_id = int(reward["id"])
        conn.execute(
            "UPDATE vault_catalog_rewards SET is_active=0 WHERE id=?",
            (catalog_id,),
        )
        conn.execute(
            """
            UPDATE quiz_campaigns
            SET final_prize_type='reward_card',
                final_prize_catalog_reward_id=?,
                final_prize_jackcoin_amount=0
            WHERE code=?
            """,
            (catalog_id, "e2e_daily_414"),
        )
    return catalog_id


def test_welcome_and_prize_screen_show_configured_extra_card(
    page: Page, jackside_server: dict
) -> None:
    _configure_hidden_free_reentry(Path(jackside_server["db_path"]))
    _set_member_cookie(page, jackside_server)

    page.goto(
        f"{jackside_server['base_url']}/quiz?campaign={jackside_server['campaign']}",
        wait_until="domcontentloaded",
    )

    welcome = page.locator('[data-screen="welcome"]')
    expect(welcome).to_be_visible(timeout=20_000)
    expect(welcome.locator('[data-role="welcome-base-prize"]')).to_contain_text(
        "414 JACKCOIN победителю"
    )
    extra = welcome.locator('[data-role="welcome-superprize"]')
    expect(extra).to_be_visible()
    expect(extra).to_contain_text("Дополнительно на кону")
    expect(extra).to_contain_text("FreeReEntry")

    welcome.locator('[data-action="identify"]').click()
    prize_screen = page.locator('[data-screen="daily-prize"]')
    expect(prize_screen).to_be_visible()
    expect(prize_screen.locator(".daily-prize-card").first).to_contain_text(
        "414 JACKCOIN"
    )
    extra_card = prize_screen.locator('[data-role="daily-extra-prize"]')
    expect(extra_card).to_be_visible()
    expect(extra_card).to_contain_text("СУПЕРПРИЗ ВЫПУСКА")
    expect(extra_card).to_contain_text("FreeReEntry")
    expect(extra_card).not_to_contain_text("Дополнительный приз")
    expect(extra_card).not_to_contain_text("Победитель получит карту")
    expect(extra_card.locator("small")).to_have_count(0)


def test_welcome_does_not_invent_extra_prize_when_none_is_configured(
    page: Page, jackside_server: dict
) -> None:
    with transaction(Path(jackside_server["db_path"])) as conn:
        conn.execute(
            """
            UPDATE quiz_campaigns
            SET final_prize_type='none',
                final_prize_catalog_reward_id=NULL,
                final_prize_jackcoin_amount=0
            WHERE code=?
            """,
            ("e2e_daily_414",),
        )
    _set_member_cookie(page, jackside_server)

    page.goto(
        f"{jackside_server['base_url']}/quiz?campaign={jackside_server['campaign']}",
        wait_until="domcontentloaded",
    )

    welcome = page.locator('[data-screen="welcome"]')
    expect(welcome).to_be_visible(timeout=20_000)
    expect(welcome.locator('[data-role="welcome-base-prize"]')).to_contain_text(
        "414 JACKCOIN победителю"
    )
    expect(welcome.locator('[data-role="welcome-superprize"]')).to_have_count(0)


def test_winner_screen_renders_issued_superprize_and_my_cards_link(
    page: Page, jackside_server: dict
) -> None:
    catalog_id = _configure_hidden_free_reentry(Path(jackside_server["db_path"]))
    _set_member_cookie(page, jackside_server)

    payload = {
        "ok": True,
        "state": "winner",
        "campaign": jackside_server["campaign"],
        "issue_jackcoin_total": 414,
        "issue_jackcoin_breakdown": {
            "main": 140,
            "final_correct": 0,
            "final_win": 274,
            "final_prize": 0,
        },
        "message": "Победа! За выпуск у вас 414 JACKCOIN.",
        "superprize": {
            "kind": "jack_card",
            "member_reward_id": 13,
            "catalog_reward_id": catalog_id,
            "title": "FreeReEntry",
            "status": "active",
            "my_cards_url": "/account?tab=vault&store=cards",
        },
    }

    def fulfill_outcome(route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload, ensure_ascii=False),
        )

    page.route("**/api/jackside/final-outcome?campaign=*", fulfill_outcome)
    page.goto(
        f"{jackside_server['base_url']}/quiz?campaign={jackside_server['campaign']}",
        wait_until="domcontentloaded",
    )

    page.evaluate(
        """
        () => {
          document.querySelectorAll('[data-screen]').forEach((node) => {
            node.classList.toggle('active', node.dataset.screen === 'final-outcome');
          });
        }
        """
    )

    block = page.locator(".jackside-final-superprize")
    expect(block).to_be_visible(timeout=10_000)
    expect(block).to_contain_text("СУПЕРПРИЗ ВЫПУСКА")
    expect(block).to_contain_text("FreeReEntry")
    expect(block).to_contain_text("JACK CARD добавлена в My Cards")
    expect(block.locator(".jackside-final-superprize-link")).to_have_attribute(
        "href", "/account?tab=vault&store=cards"
    )
