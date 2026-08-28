from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import Settings
from app.db import init_db, transaction
from app.legal_registration import (
    LEGAL_VERSION,
    _is_adult,
    _record_optional_consent,
    _record_rating_consent,
    ensure_legal_registration_schema,
)
from app.main import create_app


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        admin_pin="2468",
        admin_name="Test Admin",
        secret_key="test-secret-key-that-is-longer-than-32-characters",
        db_path=tmp_path / "legal-app.sqlite3",
        secure_cookie=False,
        public_base_url="https://club.example.test",
        quiz_public_base_url="https://quiz.example.test",
        smtp_host="smtp.example.test",
        smtp_from="club@example.test",
        member_portal_enabled=True,
    )


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _master_login(client: TestClient) -> None:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "username": "master",
            "pin": "2468",
            "csrf_token": _csrf(page.text),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/account/legal",
            "scheme": "https",
            "headers": [(b"user-agent", b"pytest-agent")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
        }
    )


def test_legal_source_slots_are_empty_and_registration_has_no_consent_ui() -> None:
    root = Path(__file__).resolve().parents[1]
    for filename in (
        "01_user_agreement.txt",
        "02_privacy_policy.txt",
        "03_personal_data_consent.txt",
        "04_marketing_consent.txt",
        "05_image_consent.txt",
        "06_public_rating_consent.txt",
    ):
        assert (root / "data/legal" / filename).read_text(encoding="utf-8") == ""

    register = (root / "app/templates/member_register.html").read_text(
        encoding="utf-8"
    )
    assert "data-registration-form" in register
    assert "data-consent-form" not in register
    assert 'type="checkbox"' not in register
    assert "marketing_consent" not in register
    assert "/legal/" not in register


def test_schema_replaces_legacy_seed_but_preserves_master_published_version(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legal.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        legacy = conn.execute(
            """
            SELECT version FROM legal_documents
            WHERE code='privacy' AND is_active=1
            """
        ).fetchone()
        assert legacy["version"] == "1.0"

        ensure_legal_registration_schema(conn)
        current = conn.execute(
            """
            SELECT version,title FROM legal_documents
            WHERE code='privacy' AND is_active=1
            """
        ).fetchone()
        assert current["version"] == LEGAL_VERSION
        assert current["title"] == (
            "Пользовательское соглашение и Правила Hi, Jack! Club"
        )
        assert conn.execute(
            """
            SELECT content FROM legal_documents
            WHERE code='privacy' AND is_active=1
            """
        ).fetchone()["content"] == ""
        reference_count = conn.execute(
            """
            SELECT COUNT(*) FROM legal_reference_documents
            WHERE is_active=1
            """
        ).fetchone()[0]
        assert reference_count == 4

        conn.execute("UPDATE legal_documents SET is_active=0 WHERE code='privacy'")
        conn.execute(
            """
            INSERT INTO legal_documents(code,version,title,content,is_active)
            VALUES (
                'privacy','2.0','Master edition',
                'Custom legal text that is intentionally kept by seeding.',1
            )
            """
        )
        ensure_legal_registration_schema(conn)
        preserved = conn.execute(
            """
            SELECT version,title,content FROM legal_documents
            WHERE code='privacy' AND is_active=1
            """
        ).fetchone()
        assert preserved["version"] == "2.0"
        assert preserved["title"] == "Master edition"
        assert preserved["content"].startswith("Custom legal text")


def test_optional_and_public_rating_consent_proof_is_versioned_and_redacted_on_delete(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "consents.sqlite3"
    init_db(db_path)
    settings = SimpleNamespace(secret_key="s" * 40)
    request = _request()

    with transaction(db_path) as conn:
        ensure_legal_registration_schema(conn)
        client_id = int(
            conn.execute(
                """
                INSERT INTO clients(first_name,client_status,source)
                VALUES ('Legal User','existing','test')
                """
            ).lastrowid
        )
        account_id = int(
            conn.execute(
                """
                INSERT INTO member_accounts(
                    client_id,email,email_normalized,password_hash,email_verified_at
                ) VALUES (?,?,?,?,CURRENT_TIMESTAMP)
                """,
                (client_id, "legal@example.com", "legal@example.com", "hash"),
            ).lastrowid
        )

        _record_optional_consent(
            conn,
            settings=settings,
            request=request,
            account_id=account_id,
            code="marketing-consent",
            granted=True,
        )
        _record_rating_consent(
            conn,
            settings=settings,
            request=request,
            account_id=account_id,
            categories={"nickname": True, "place": True},
            conditions_text="Только в рейтинге текущего сезона",
        )

        state = conn.execute(
            """
            SELECT document_version,granted
            FROM member_optional_consent_state
            WHERE account_id=? AND code='marketing-consent'
            """,
            (account_id,),
        ).fetchone()
        assert state["document_version"] == LEGAL_VERSION
        assert state["granted"] == 1

        rating = conn.execute(
            """
            SELECT document_version,categories_json,granted,conditions_text
            FROM member_public_rating_consent_state WHERE account_id=?
            """,
            (account_id,),
        ).fetchone()
        categories = json.loads(rating["categories_json"])
        assert rating["document_version"] == LEGAL_VERSION
        assert rating["granted"] == 1
        assert rating["conditions_text"] == "Только в рейтинге текущего сезона"
        assert categories["nickname"] is True
        assert categories["place"] is True
        assert categories["avatar"] is False

        conn.execute(
            "UPDATE clients SET client_status='deleted' WHERE id=?", (client_id,)
        )
        optional_event = conn.execute(
            """
            SELECT ip_hash,user_agent FROM member_optional_consent_events
            WHERE account_id=? ORDER BY id DESC LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        rating_event = conn.execute(
            """
            SELECT ip_hash,user_agent FROM member_public_rating_consent_events
            WHERE account_id=? ORDER BY id DESC LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        assert optional_event["ip_hash"] == "deleted"
        assert optional_event["user_agent"] is None
        assert rating_event["ip_hash"] == "deleted"
        assert rating_event["user_agent"] is None


def test_registration_starts_with_profile_and_public_legal_body_is_empty(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))
    with client:
        page = client.get("/account/register")
        assert page.status_code == 200
        assert "data-registration-form" in page.text
        assert "data-consent-form" not in page.text
        assert 'type="checkbox"' not in page.text
        assert "marketing_consent" not in page.text
        assert "/legal/" not in page.text

        stale = client.post(
            "/account/register/consent",
            data={
                "csrf_token": _csrf(page.text),
                "document_code": "privacy",
                "accepted": "true",
            },
            follow_redirects=False,
        )
        assert stale.status_code == 303
        assert stale.headers["location"] == "/account/register"

        policy = client.get("/legal/privacy-policy")
        assert policy.status_code == 200
        assert "legal-document-card" in policy.text
        assert "Политика в отношении обработки и защиты персональных данных" in policy.text
        assert "Политика подлежит размещению в свободном доступе" not in policy.text

        with transaction(settings.db_path) as conn:
            active_bodies = conn.execute(
                """
                SELECT content FROM legal_documents WHERE is_active=1
                UNION ALL
                SELECT content FROM legal_reference_documents WHERE is_active=1
                """
            ).fetchall()
            assert active_bodies
            assert all(str(row["content"] or "") == "" for row in active_bodies)


def test_existing_consent_history_is_preserved_when_document_bodies_are_blank(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    TestClient(create_app(settings))
    with transaction(settings.db_path) as conn:
        client_id = int(
            conn.execute(
                """
                INSERT INTO clients(first_name,client_status,source)
                VALUES ('History User','existing','test')
                """
            ).lastrowid
        )
        account_id = int(
            conn.execute(
                """
                INSERT INTO member_accounts(
                    client_id,email,email_normalized,password_hash,email_verified_at
                ) VALUES (?,?,?,?,CURRENT_TIMESTAMP)
                """,
                (client_id, "history@example.com", "history@example.com", "hash"),
            ).lastrowid
        )
        document = conn.execute(
            "SELECT id,code,version FROM legal_documents WHERE code='privacy' AND is_active=1"
        ).fetchone()
        conn.execute(
            """
            INSERT INTO member_consents(
                account_id,document_id,document_code,document_version,
                ip_hash,user_agent
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                account_id,
                int(document["id"]),
                str(document["code"]),
                str(document["version"]),
                "proof",
                "pytest",
            ),
        )
        before = conn.execute(
            "SELECT COUNT(*) FROM member_consents WHERE account_id=?",
            (account_id,),
        ).fetchone()[0]
        ensure_legal_registration_schema(conn)
        after = conn.execute(
            "SELECT COUNT(*) FROM member_consents WHERE account_id=?",
            (account_id,),
        ).fetchone()[0]
        content = conn.execute(
            "SELECT content FROM legal_documents WHERE id=?",
            (int(document["id"]),),
        ).fetchone()["content"]

    assert before == 1
    assert after == 1
    assert content == ""


def test_registration_rejects_under_18_before_account_creation(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    with client:
        page = client.get("/account/register")
        token = _csrf(page.text)
        today = date.today()
        underage = date(
            today.year - 17, today.month, min(today.day, 28)
        ).isoformat()
        response = client.post(
            "/api/account/register/legal-extra",
            data={"csrf_token": token, "birth_date": underage},
        )
        assert response.status_code == 409
        assert response.json()["ok"] is False
        assert "18 лет" in response.json()["error"]

    assert _is_adult("1990-01-01") is True


def test_quiz_requires_member_account_and_preserves_target_link(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    with client:
        page = client.get("/quiz?campaign=default", follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"].startswith("/account/login?next=")
        assert "%2Fquiz%3Fcampaign%3Ddefault" in page.headers["location"]

        api = client.get("/api/quiz/questions?campaign=default")
        assert api.status_code == 401
        assert api.json()["error"] == "account_required"

        login_page = client.get(page.headers["location"])
        assert login_page.status_code == 200
        assert "Создать аккаунт" in login_page.text
        # The target is kept server-side in member_next, so the register link can
        # remain clean while successful login/registration still returns to /quiz.
        assert 'href="/account/register"' in login_page.text


def test_master_registry_separates_active_and_archive_users(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))
    with client:
        _master_login(client)
        with transaction(settings.db_path) as conn:
            active_id = int(
                conn.execute(
                    """
                    INSERT INTO clients(first_name,client_status,source)
                    VALUES ('Active Legal','existing','test')
                    """
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO member_accounts(
                    client_id,email,email_normalized,password_hash,email_verified_at
                ) VALUES (?,?,?,?,CURRENT_TIMESTAMP)
                """,
                (active_id, "active@example.com", "active@example.com", "hash"),
            )
            conn.execute(
                """
                INSERT INTO clients(first_name,client_status,source)
                VALUES ('No Account Archive','existing','test')
                """
            )
            deleted_id = int(
                conn.execute(
                    """
                    INSERT INTO clients(first_name,client_status,source)
                    VALUES ('Deleted Archive','deleted','test')
                    """
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO member_accounts(
                    client_id,email,email_normalized,password_hash,
                    is_active,email_verified_at
                ) VALUES (?,?,?,?,0,CURRENT_TIMESTAMP)
                """,
                (
                    deleted_id,
                    "deleted-test@deleted.invalid",
                    "deleted-test@deleted.invalid",
                    "deleted",
                ),
            )

        active = client.get("/master/member-accounts?view=active")
        assert active.status_code == 200
        assert "Active Legal" in active.text
        assert "No Account Archive" not in active.text
        assert "Deleted Archive" not in active.text
        assert "Активные" in active.text and "Архив" in active.text

        archive = client.get("/master/member-accounts?view=archive")
        assert archive.status_code == 200
        assert "Active Legal" not in archive.text
        assert "No Account Archive" in archive.text
        assert "Deleted Archive" in archive.text

        legal_page = client.get("/master/legal-documents")
        assert legal_page.status_code == 200
        assert "Юридические документы" in legal_page.text
        assert (
            "Пользовательское соглашение и Правила Hi, Jack! Club"
            in legal_page.text
        )
        assert (
            "Согласие на получение рекламных и информационных сообщений"
            in legal_page.text
        )
