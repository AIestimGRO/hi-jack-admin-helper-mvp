from __future__ import annotations

from playwright.sync_api import Page, expect

from app.db import transaction
from app.services.member_accounts import jackcoin_balance
from test_jackside_welcome import jackside_server as _jackside_server


jackside_server = _jackside_server


def test_admin_manual_jackcoin_credit_and_debit_use_canonical_routes_in_browser(
    page: Page,
    jackside_server: dict,
) -> None:
    base_url = jackside_server["base_url"]
    db_path = jackside_server["db_path"]

    with transaction(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM clients WHERE first_name='E2E' ORDER BY id LIMIT 1"
        ).fetchone()
        assert row is not None
        client_id = int(row["id"])
        assert jackcoin_balance(conn, client_id) == 0

    page.goto(f"{base_url}/login", wait_until="domcontentloaded")
    page.locator('input[name="username"]').fill("master")
    page.locator('input[name="pin"]').fill("2468")
    page.locator('form[action="/login"] button[type="submit"]').click()
    page.wait_for_load_state("domcontentloaded")

    page.goto(f"{base_url}/clients/{client_id}", wait_until="domcontentloaded")
    card = page.locator(".jackcoin-admin-card")
    expect(card).to_be_visible()
    expect(card).to_contain_text("0 JC")

    page.locator('.jackcoin-credit-form input[name="amount"]').fill("200")
    page.locator('.jackcoin-credit-form select[name="reason"]').select_option(
        "Отзыв на Яндекс Картах"
    )

    requested_urls: list[str] = []
    page.on(
        "request",
        lambda request: requested_urls.append(request.url)
        if request.method == "POST"
        else None,
    )

    page.locator(".jackcoin-credit-submit").click()
    expect(page.locator(".jackcoin-admin-card")).to_contain_text("200 JC")

    assert any(
        f"/api/clients/{client_id}/jackcoin/credit" in url
        for url in requested_urls
    )
    assert not any(
        url.rstrip("/").endswith(f"/clients/{client_id}")
        for url in requested_urls
    )

    with transaction(db_path) as conn:
        assert jackcoin_balance(conn, client_id) == 200
        rows = conn.execute(
            """
            SELECT amount,source_type,comment
            FROM jackcoin_ledger
            WHERE client_id=?
            ORDER BY id
            """,
            (client_id,),
        ).fetchall()
    assert len(rows) == 1
    assert int(rows[0]["amount"]) == 200
    assert rows[0]["source_type"] == "admin"
    assert "Отзыв на Яндекс Картах" in rows[0]["comment"]

    page.locator('.jackcoin-credit-form input[name="amount"]').fill("50")
    page.locator('.jackcoin-credit-form select[name="reason"]').select_option(
        "Оплата / списание"
    )
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator(".jackcoin-debit-submit").click()
    expect(page.locator(".jackcoin-admin-card")).to_contain_text("150 JC")

    assert any(
        f"/api/clients/{client_id}/jackcoin/debit" in url
        for url in requested_urls
    )

    with transaction(db_path) as conn:
        assert jackcoin_balance(conn, client_id) == 150
        rows = conn.execute(
            """
            SELECT amount,operation_type,source_type,comment
            FROM jackcoin_ledger
            WHERE client_id=?
            ORDER BY id
            """,
            (client_id,),
        ).fetchall()

    assert [int(row["amount"]) for row in rows] == [200, -50]
    assert rows[1]["operation_type"] == "spend"
    assert rows[1]["source_type"] == "admin"
    assert "Оплата / списание" in rows[1]["comment"]
