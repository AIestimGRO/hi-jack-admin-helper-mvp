import re

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import transaction
from app.main import create_app
from app.services.member_accounts import MEMBER_COOKIE_NAME, hash_password, issue_session
from app.services.vault import create_catalog_reward, purchase_reward


def _csrf(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def test_activation_redirect_keeps_user_in_my_cards(tmp_path) -> None:
    settings = Settings(
        admin_pin="2468",
        admin_name="Vault Navigation Test",
        secret_key="vault-navigation-test-secret-key-longer-than-32-characters",
        db_path=tmp_path / "member-vault-navigation.sqlite3",
        secure_cookie=False,
        member_portal_enabled=True,
        public_base_url="https://club.example.test",
        quiz_public_base_url="https://quiz.example.test",
    )
    client = TestClient(create_app(settings), base_url=settings.public_base_url)

    with client:
        with transaction(settings.db_path) as conn:
            client_id = int(
                conn.execute(
                    "INSERT INTO clients(first_name, source) VALUES ('Card Player', 'test')"
                ).lastrowid
            )
            account_id = int(
                conn.execute(
                    """
                    INSERT INTO member_accounts(
                        client_id, email, email_normalized, password_hash,
                        email_verified_at
                    ) VALUES (?, 'card@example.test', 'card@example.test', ?,
                              CURRENT_TIMESTAMP)
                    """,
                    (client_id, hash_password("abcdef")),
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
            conn.execute(
                """
                INSERT OR IGNORE INTO admins(
                    id, username, display_name, pin_hash, role
                ) VALUES (1, 'vault-nav-test', 'Vault Nav Test', 'test', 'master_admin')
                """
            )
            catalog = create_catalog_reward(
                conn,
                code="nav_card",
                title="NAV CARD",
                description="Navigation test card",
                category="card",
                price_jc=0,
                validity_days=30,
                inventory_total=None,
                redeem_instructions="Show to admin",
                position=10,
                admin_id=1,
            )
            reward = purchase_reward(
                conn,
                client_id=client_id,
                catalog_reward_id=int(catalog["id"]),
                purchase_id="member-vault-navigation",
            )
            token = issue_session(
                conn,
                secret_key=settings.secret_key,
                account_id=account_id,
                session_version=1,
                days=30,
                ip_hash="test",
                user_agent="pytest",
            )

        client.cookies.set(MEMBER_COOKIE_NAME, token)
        cards_page = client.get("/account?tab=vault&store=cards")
        assert cards_page.status_code == 200

        activated = client.post(
            f"/account/rewards/{reward['id']}/activate",
            data={"csrf_token": _csrf(cards_page)},
            follow_redirects=False,
        )
        assert activated.status_code == 303
        location = activated.headers["location"]
        assert "tab=vault" in location
        assert "store=cards" in location
        assert location.endswith(f"#card-{reward['id']}")

        activated_page = client.get(location)
        assert activated_page.status_code == 200
        assert 'data-account-tab="vault"' in activated_page.text
        assert f'id="card-{reward["id"]}"' in activated_page.text
        assert "reward-activation-code" in activated_page.text
