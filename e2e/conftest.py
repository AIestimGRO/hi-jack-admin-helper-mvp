"""Playwright defaults for JACKSIDE e2e."""

from __future__ import annotations

import pytest

from app.db import transaction


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "locale": "ru-RU",
        "viewport": {"width": 1280, "height": 720},
    }


@pytest.fixture(autouse=True)
def current_legal_consents_for_jackside_e2e(request):
    """Keep the synthetic E2E member accepted on the currently active legal editions."""
    if "jackside_server" not in request.fixturenames:
        yield
        return

    server = request.getfixturevalue("jackside_server")
    with transaction(server["db_path"]) as conn:
        account = conn.execute(
            "SELECT id FROM member_accounts WHERE email_normalized='e2e@load.test'"
        ).fetchone()
        if account:
            account_id = int(account["id"])
            documents = conn.execute(
                "SELECT * FROM legal_documents WHERE is_active=1"
            ).fetchall()
            for document in documents:
                exists = conn.execute(
                    """
                    SELECT 1 FROM member_consents
                    WHERE account_id=? AND document_code=? AND document_version=?
                    LIMIT 1
                    """,
                    (account_id, document["code"], document["version"]),
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    """
                    INSERT INTO member_consents(
                        account_id, document_id, document_code, document_version,
                        ip_hash, user_agent
                    ) VALUES (?, ?, ?, ?, 'e2e-ip', 'playwright')
                    """,
                    (
                        account_id,
                        document["id"],
                        document["code"],
                        document["version"],
                    ),
                )
    yield
