from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import connect, transaction
from app.main import create_app
from app.services.member_accounts import hash_password, verify_password


def make_member_client(
    tmp_path: Path, *, enabled: bool = True
) -> tuple[TestClient, Settings]:
    settings = Settings(
        admin_pin="2468",
        admin_name="Test Admin",
        secret_key="member-test-secret-key-that-is-longer-than-32-characters",
        db_path=tmp_path / "member.sqlite3",
        secure_cookie=False,
        public_base_url="https://club.example.test",
        quiz_public_base_url="https://quiz.example.test",
        smtp_host="smtp.example.test",
        smtp_from="club@example.test",
        telegram_client_id="telegram-client",
        telegram_client_secret="telegram-secret",
        member_portal_enabled=enabled,
    )
    return TestClient(
        create_app(settings), base_url=settings.public_base_url
    ), settings


def csrf_from(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def admin_login(client: TestClient) -> None:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "username": "master",
            "pin": "2468",
            "csrf_token": csrf_from(page),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def accept_registration_documents(client: TestClient) -> None:
    privacy = client.get("/account/register")
    assert privacy.status_code == 200
    assert "Политика конфиденциальности" in privacy.text
    response = client.post(
        "/account/register/consent",
        data={
            "document_code": "privacy",
            "accepted": "true",
            "csrf_token": csrf_from(privacy),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    rewards = client.get("/account/register")
    assert "Условия безденежных вознаграждений" in rewards.text
    response = client.post(
        "/account/register/consent",
        data={
            "document_code": "rewards",
            "accepted": "true",
            "csrf_token": csrf_from(rewards),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def connect_member_telegram(
    client: TestClient, monkeypatch, *, username: str | None = "poker_player"
) -> None:
    captured: dict[str, str] = {}

    def fake_authorization_url(**kwargs):
        captured["state"] = kwargs["state"]
        captured["redirect_uri"] = kwargs["redirect_uri"]
        return "https://oauth.telegram.example/authorize"

    def fake_exchange(**kwargs):
        return {
            "sub": "tg-permanent-101",
            "preferred_username": username,
            "name": "Алекс",
        }

    monkeypatch.setattr(
        "app.main_impl.authorization_url", fake_authorization_url
    )
    monkeypatch.setattr(
        "app.main_impl.exchange_telegram_code", fake_exchange
    )
    started = client.get(
        "/account/telegram/start", follow_redirects=False
    )
    assert started.status_code == 302
    assert (
        captured["redirect_uri"]
        == "https://club.example.test/account/telegram/callback"
    )
    callback = client.get(
        f"/account/telegram/callback?code=oauth-code&state={captured['state']}",
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"].startswith("/account/telegram?")
    telegram_page = client.get("/account/telegram")
    assert "Telegram подключён" in telegram_page.text
    if username:
        assert f"@{username}" in telegram_page.text


def request_registration_code(
    client: TestClient, captured: dict[str, str]
) -> None:
    profile = client.get("/account/register")
    assert "Личные данные" in profile.text
    response = client.post(
        "/account/register/request-code",
        data={
            "email": "Player@Example.com",
            "password": "PokerPlayer2026",
            "password_confirmation": "PokerPlayer2026",
            "phone": "+7 999 123-45-67",
            "first_name": "Алекс",
            "csrf_token": csrf_from(profile),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "register" in captured


def test_password_hash_is_salted_and_verifiable() -> None:
    first = hash_password("abcdef")
    second = hash_password("abcdef")
    assert first != second
    assert verify_password("abcdef", first)
    assert not verify_password("wrong-password", first)
    with pytest.raises(ValueError, match="от 6 до 128"):
        hash_password("abcde")
    with pytest.raises(ValueError, match="хотя бы одну букву"):
        hash_password("123456")


def test_member_portal_is_hidden_by_default(tmp_path: Path) -> None:
    client, _ = make_member_client(tmp_path, enabled=False)
    with client:
        assert client.get("/account").status_code == 404
        assert client.get("/account/register").status_code == 404


def test_registration_consents_account_session_and_profile(
    tmp_path: Path, monkeypatch
) -> None:
    client, settings = make_member_client(tmp_path)
    captured: dict[str, str] = {}

    def fake_send(**kwargs):
        captured[kwargs["purpose"]] = kwargs["code"]

    monkeypatch.setattr("app.main_impl.send_member_email_code", fake_send)
    with client:
        accept_registration_documents(client)
        profile = client.get("/account/register")
        assert "Минимум 6 символов и хотя бы одна буква" in profile.text
        assert "привязанный к вашей карточке участника Hi Jack UP!" in profile.text
        assert "Подтвердить через Telegram" not in profile.text
        submit = re.search(
            r"<button[^>]+data-registration-submit[^>]*>", profile.text
        )
        assert submit
        assert "disabled" not in submit.group(0)
        request_registration_code(client, captured)

        verify = client.get("/account/register")
        assert "Введите код" in verify.text
        wrong = client.post(
            "/account/register/verify",
            data={"code": "000000", "csrf_token": csrf_from(verify)},
            follow_redirects=False,
        )
        assert wrong.status_code == 303
        with connect(settings.db_path) as conn:
            assert (
                conn.execute(
                    "SELECT attempts_left FROM member_email_codes WHERE purpose='register'"
                ).fetchone()[0]
                == 4
            )

        verify = client.get("/account/register")
        created = client.post(
            "/account/register/verify",
            data={"code": captured["register"], "csrf_token": csrf_from(verify)},
            follow_redirects=False,
        )
        assert created.status_code == 303
        assert created.headers["location"] == "/account/telegram"
        assert "hjc_member_session" in created.cookies

        telegram_step = client.get("/account/telegram")
        assert telegram_step.status_code == 200
        assert "Подключить Telegram?" in telegram_step.text
        assert (
            "Telegram необходим чтобы объединить Ваши достижения "
            "из приложения Hi Jack UP!"
        ) in telegram_step.text
        assert "Дополнительный шаг" not in telegram_step.text
        assert "Это необязательно" not in telegram_step.text
        assert "Пропустить и перейти в кабинет" in telegram_step.text

        telegram_start = client.get(
            "/account/telegram/start", follow_redirects=False
        )
        assert telegram_start.status_code == 302
        authorization = urlparse(telegram_start.headers["location"])
        assert authorization.scheme == "https"
        assert authorization.netloc == "oauth.telegram.org"
        parameters = parse_qs(authorization.query)
        assert parameters["client_id"] == ["telegram-client"]
        assert parameters["redirect_uri"] == [
            "https://club.example.test/account/telegram/callback"
        ]
        assert parameters["response_type"] == ["code"]
        assert parameters["scope"] == ["openid profile"]
        assert parameters["state"]
        assert parameters["code_challenge"]
        assert parameters["code_challenge_method"] == ["S256"]

        skipped_profile = client.get("/account")
        assert skipped_profile.status_code == 200
        assert "Подключить" in skipped_profile.text
        connect_member_telegram(client, monkeypatch)

        profile = client.get("/account")
        assert profile.status_code == 200
        assert "Алекс" in profile.text
        assert "player@example.com" in profile.text
        assert "@poker_player" in profile.text
        assert "0 <small>JC</small>" in profile.text
        assert "Личные данные" in profile.text
        assert "Статистика" in profile.text
        assert "Награды" in profile.text

        stats = client.get("/account?tab=stats")
        assert "Рейтинг HI, JACK CLUB!" in stats.text
        assert "История участия в квизах" in stats.text
        rewards = client.get("/account?tab=rewards")
        assert "Каталог наград" in rewards.text
        assert "Активные награды" in rewards.text

        with transaction(settings.db_path) as conn:
            conn.execute(
                "UPDATE legal_documents SET is_active=0 WHERE code='privacy'"
            )
            conn.execute(
                """
                INSERT INTO legal_documents(code, version, title, content)
                VALUES ('privacy', '2.0', 'Новая политика', 'Обновлённые условия')
                """
            )
        gated = client.get("/account", follow_redirects=False)
        assert gated.status_code == 303
        assert gated.headers["location"] == "/account/consents"
        consent_page = client.get("/account/consents")
        assert "Новая политика" in consent_page.text
        document_id = re.search(
            r'name="document_id" value="([0-9]+)"', consent_page.text
        ).group(1)
        accepted = client.post(
            "/account/consents",
            data={
                "document_id": document_id,
                "accepted": "true",
                "csrf_token": csrf_from(consent_page),
            },
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        assert client.get("/account").status_code == 200

    with connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM member_accounts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM member_consents").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM member_sessions").fetchone()[0] == 1
        account = conn.execute("SELECT * FROM member_accounts").fetchone()
        assert account["email_normalized"] == "player@example.com"
        assert account["password_hash"] != "PokerPlayer2026"
        client_row = conn.execute(
            "SELECT * FROM clients WHERE id=?", (account["client_id"],)
        ).fetchone()
        assert client_row["phone_local"] == "9991234567"
        assert client_row["username"] == "poker_player"
        assert client_row["telegram_user_id"] == "tg-permanent-101"


def test_login_logout_and_password_reset(tmp_path: Path, monkeypatch) -> None:
    client, settings = make_member_client(tmp_path)
    captured: dict[str, str] = {}

    def fake_send(**kwargs):
        captured[kwargs["purpose"]] = kwargs["code"]

    monkeypatch.setattr("app.main_impl.send_member_email_code", fake_send)
    with client:
        accept_registration_documents(client)
        request_registration_code(client, captured)
        verify = client.get("/account/register")
        client.post(
            "/account/register/verify",
            data={"code": captured["register"], "csrf_token": csrf_from(verify)},
        )

        account_page = client.get("/account")
        logged_out = client.post(
            "/account/logout",
            data={"csrf_token": csrf_from(account_page)},
            follow_redirects=False,
        )
        assert logged_out.status_code == 303
        assert client.get("/account", follow_redirects=False).status_code == 303

        login_page = client.get("/account/login")
        bad_login = client.post(
            "/account/login",
            data={
                "email": "player@example.com",
                "password": "WrongPassword2026",
                "csrf_token": csrf_from(login_page),
            },
            follow_redirects=False,
        )
        assert "error=" in bad_login.headers["location"]

        forgot = client.get("/account/forgot-password")
        requested = client.post(
            "/account/forgot-password",
            data={
                "email": "player@example.com",
                "csrf_token": csrf_from(forgot),
            },
            follow_redirects=False,
        )
        assert requested.status_code == 303
        assert "reset_password" in captured

        reset_page = client.get("/account/reset-password")
        reset = client.post(
            "/account/reset-password",
            data={
                "code": captured["reset_password"],
                "password": "NewPokerPassword2026",
                "password_confirmation": "NewPokerPassword2026",
                "csrf_token": csrf_from(reset_page),
            },
            follow_redirects=False,
        )
        assert reset.status_code == 303
        assert reset.headers["location"].startswith("/account/login?")

        login_page = client.get("/account/login")
        old_password = client.post(
            "/account/login",
            data={
                "email": "player@example.com",
                "password": "PokerPlayer2026",
                "csrf_token": csrf_from(login_page),
            },
            follow_redirects=False,
        )
        assert "error=" in old_password.headers["location"]

        login_page = client.get("/account/login")
        new_password = client.post(
            "/account/login",
            data={
                "email": "player@example.com",
                "password": "NewPokerPassword2026",
                "csrf_token": csrf_from(login_page),
            },
            follow_redirects=False,
        )
        assert new_password.status_code == 303
        assert new_password.headers["location"] == "/account"

    with connect(settings.db_path) as conn:
        account = conn.execute("SELECT * FROM member_accounts").fetchone()
        assert account["session_version"] == 2
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM member_sessions WHERE revoked_at IS NOT NULL"
            ).fetchone()[0]
            >= 1
        )


def test_telegram_connect_requires_account_and_accepts_missing_username(
    tmp_path: Path, monkeypatch
) -> None:
    client, settings = make_member_client(tmp_path)
    captured: dict[str, str] = {}

    def fake_send(**kwargs):
        captured[kwargs["purpose"]] = kwargs["code"]

    monkeypatch.setattr("app.main_impl.send_member_email_code", fake_send)
    with client:
        anonymous = client.get(
            "/account/telegram/start", follow_redirects=False
        )
        assert anonymous.status_code == 303
        assert anonymous.headers["location"].startswith("/account/login?")

        accept_registration_documents(client)
        request_registration_code(client, captured)
        verify = client.get("/account/register")
        client.post(
            "/account/register/verify",
            data={
                "code": captured["register"],
                "csrf_token": csrf_from(verify),
            },
            follow_redirects=False,
        )
        connect_member_telegram(client, monkeypatch, username=None)
        account_page = client.get("/account")
        assert "Подключён" in account_page.text

    with connect(settings.db_path) as conn:
        client_row = conn.execute(
            """
            SELECT c.* FROM clients c
            JOIN member_accounts ma ON ma.client_id=c.id
            """
        ).fetchone()
        assert client_row["telegram_user_id"] == "tg-permanent-101"
        assert client_row["username"] is None


def test_daily_414_campaign_is_isolated_from_classic_quiz(
    tmp_path: Path,
) -> None:
    client, settings = make_member_client(tmp_path)
    with client:
        admin_login(client)
        master = client.get("/master?tab=campaigns")
        created = client.post(
            "/api/master/quiz-campaigns/create",
            data={
                "campaign_type": "daily_414",
                "code": "jackside_daily",
                "title": "JACKSIDE 4:14",
                "csrf_token": csrf_from(master),
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        page = client.get("/master?tab=campaigns")
        assert "Новый подраздел" in page.text
        assert "JACKSIDE 4:14" in page.text
        with connect(settings.db_path) as conn:
            campaign_id = conn.execute(
                "SELECT id FROM quiz_campaigns WHERE code='jackside_daily'"
            ).fetchone()[0]
        updated = client.post(
            f"/api/master/quiz-campaigns/{campaign_id}/update",
            data={
                "title": "JACKSIDE 4:14",
                "quiz_time_limit_seconds": 999,
                "max_attempts": 9,
                "csrf_token": csrf_from(page),
            },
            follow_redirects=False,
        )
        assert updated.status_code == 303

        classic = client.get("/quiz?campaign=default")
        assert classic.status_code == 200
        isolated = client.get("/quiz?campaign=jackside_daily")
        assert isolated.status_code == 503
        assert (
            client.get(
                "/api/quiz/questions?campaign=jackside_daily"
            ).status_code
            == 503
        )
        assert (
            client.post(
                "/api/quiz/start", json={"campaign": "jackside_daily"}
            ).status_code
            == 503
        )

    with connect(settings.db_path) as conn:
        row = conn.execute(
            "SELECT * FROM quiz_campaigns WHERE code='jackside_daily'"
        ).fetchone()
        assert row["campaign_type"] == "daily_414"
        assert row["quiz_time_limit_seconds"] == 254
        assert row["max_attempts"] == 1
        assert row["verification_required"] == 1
