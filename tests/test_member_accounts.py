from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import connect, transaction
from app.main import create_app
from app.services.member_accounts import (
    MEMBER_COOKIE_NAME,
    hash_password,
    issue_session,
    verify_password,
)


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


def test_registration_allows_resend_and_change_email(
    tmp_path: Path, monkeypatch
) -> None:
    client, settings = make_member_client(tmp_path)
    captured: dict[str, list[str]] = {"register": []}

    def fake_send(**kwargs):
        captured.setdefault(kwargs["purpose"], []).append(kwargs["code"])
        captured["recipient"] = kwargs["recipient"]

    monkeypatch.setattr("app.main_impl.send_member_email_code", fake_send)
    with client:
        accept_registration_documents(client)
        profile = client.get("/account/register")
        sent = client.post(
            "/account/register/request-code",
            data={
                "email": "wrong@example.com",
                "password": "PokerPlayer2026",
                "password_confirmation": "PokerPlayer2026",
                "phone": "+7 999 123-45-67",
                "first_name": "Алекс",
                "csrf_token": csrf_from(profile),
            },
            follow_redirects=False,
        )
        assert sent.status_code == 303
        assert captured["recipient"] == "wrong@example.com"
        first_code = captured["register"][-1]

        verify = client.get("/account/register")
        assert "Введите код" in verify.text
        assert "wrong@example.com" in verify.text
        assert "Отправить код ещё раз" in verify.text
        assert "Указать другую почту" in verify.text

        resent = client.post(
            "/account/register/resend-code",
            data={"csrf_token": csrf_from(verify)},
            follow_redirects=False,
        )
        assert resent.status_code == 303
        second_code = captured["register"][-1]
        assert second_code != first_code
        assert len(captured["register"]) == 2

        verify = client.get("/account/register")
        changed = client.post(
            "/account/register/change-email",
            data={"csrf_token": csrf_from(verify)},
            follow_redirects=False,
        )
        assert changed.status_code == 303
        profile = client.get("/account/register")
        assert "Личные данные" in profile.text
        assert "wrong@example.com" in profile.text
        assert "Введите код" not in profile.text

        with connect(settings.db_path) as conn:
            open_codes = conn.execute(
                """
                SELECT COUNT(*) FROM member_email_codes
                WHERE purpose='register' AND used_at IS NULL
                """
            ).fetchone()[0]
        assert open_codes == 0


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


def test_daily_414_campaign_requires_member_account(
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
                "jackcoin_per_correct": 7,
                "jackcoin_completion_bonus": 13,
                "jackcoin_perfect_bonus": 29,
                "csrf_token": csrf_from(master),
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        page = client.get("/master?tab=campaigns")
        assert "Новый подраздел" in page.text
        assert "JACKSIDE 4:14" in page.text
        assert (
            "https://club.example.test/quiz?campaign=jackside_daily"
            in page.text
        )
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
                "jackcoin_per_correct": 8,
                "jackcoin_completion_bonus": 14,
                "jackcoin_perfect_bonus": 30,
                "csrf_token": csrf_from(page),
            },
            follow_redirects=False,
        )
        assert updated.status_code == 303

        classic = client.get("/quiz?campaign=default")
        assert classic.status_code == 200
        isolated = client.get(
            "/quiz?campaign=jackside_daily",
            follow_redirects=False,
        )
        assert isolated.status_code == 303
        assert isolated.headers["location"].startswith(
            "/account/login?next="
        )

    with connect(settings.db_path) as conn:
        row = conn.execute(
            "SELECT * FROM quiz_campaigns WHERE code='jackside_daily'"
        ).fetchone()
        assert row["campaign_type"] == "daily_414"
        assert row["quiz_time_limit_seconds"] == 254
        assert row["max_attempts"] == 1
        assert row["verification_required"] == 1
        assert row["jackcoin_per_correct"] == 8
        assert row["jackcoin_completion_bonus"] == 14
        assert row["jackcoin_perfect_bonus"] == 30
        assert row["active_from"]
        assert row["active_until"]


def seed_daily_member(client: TestClient, settings: Settings) -> int:
    with transaction(settings.db_path) as conn:
        client_id = int(
            conn.execute(
                """
                INSERT INTO clients(
                    first_name, phone_raw, phone_full, phone_local, source
                ) VALUES ('Алекс', '+7 999 555-44-33', '79995554433',
                          '9995554433', 'member_portal')
                """
            ).lastrowid
        )
        account_id = int(
            conn.execute(
                """
                INSERT INTO member_accounts(
                    client_id, email, email_normalized, password_hash,
                    email_verified_at
                ) VALUES (?, 'daily@example.test', 'daily@example.test', ?,
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
                    account_id, document_id, document_code, document_version,
                    ip_hash, user_agent
                ) VALUES (?, ?, ?, ?, 'test-ip', 'pytest')
                """,
                (
                    account_id,
                    document["id"],
                    document["code"],
                    document["version"],
                ),
            )
        token = issue_session(
            conn,
            secret_key=settings.secret_key,
            account_id=account_id,
            session_version=1,
            days=30,
            ip_hash="test-ip",
            user_agent="pytest",
        )
    client.cookies.set(MEMBER_COOKIE_NAME, token)
    return client_id


def seed_daily_campaign(settings: Settings) -> None:
    local_now = datetime.now(ZoneInfo(settings.timezone_name)).replace(
        tzinfo=None
    )
    with transaction(settings.db_path) as conn:
        conn.execute(
            """
            INSERT INTO quiz_campaigns(
                code, title, campaign_type, quiz_time_limit_seconds,
                max_attempts, verification_required, current_version,
                active_from, active_until, welcome_kicker, welcome_text,
                start_button_text
            ) VALUES (
                'daily_test', 'JACKSIDE 4:14', 'daily_414', 254, 1, 1, 1,
                ?, ?, 'JACKSIDE 4:14',
                '10 вопросов. Одна попытка.',
                'ПОСМОТРЕТЬ ПРИЗ ДНЯ'
            )
            """,
            (
                (local_now - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S"),
                (local_now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),
            ),
        )
        for index in range(1, 11):
            question_id = int(
                conn.execute(
                    """
                    INSERT INTO quiz_questions(
                        campaign_code, code, type, title, required, points,
                        position, is_active
                    ) VALUES ('daily_test', ?, 'single_choice', ?, 1, 1, ?, 1)
                    """,
                    (f"d{index}", f"Вопрос {index}", index * 10),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO quiz_options(
                    question_id, code, text, is_correct, position
                ) VALUES (?, ?, 'Верно', 1, 10), (?, ?, 'Неверно', 0, 20)
                """,
                (
                    question_id,
                    f"d{index}_yes",
                    question_id,
                    f"d{index}_no",
                ),
            )
        final_question_id = int(
            conn.execute(
                """
                INSERT INTO quiz_questions(
                    campaign_code, code, type, title, game_round,
                    required, points, position, is_active
                ) VALUES (
                    'daily_test', 'final_1', 'single_choice',
                    'Вопрос финального стола', 'final', 1, 1, 10, 1
                )
                """
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO quiz_options(
                question_id, code, text, is_correct, position
            ) VALUES
                (?, 'final_yes', 'Верно', 1, 10),
                (?, 'final_no', 'Неверно', 0, 20)
            """,
            (final_question_id, final_question_id),
        )


def test_daily_414_upcoming_allows_welcome_meta_but_blocks_start(
    tmp_path: Path,
) -> None:
    client, settings = make_member_client(tmp_path)
    with client:
        seed_daily_member(client, settings)
        seed_daily_campaign(settings)
        with transaction(settings.db_path) as conn:
            conn.execute(
                """
                UPDATE quiz_campaigns
                SET active_from=?, active_until=NULL
                WHERE code='daily_test'
                """,
                (
                    (
                        datetime.now(ZoneInfo(settings.timezone_name)).replace(
                            tzinfo=None
                        )
                        + timedelta(hours=1)
                    ).strftime("%Y-%m-%dT%H:%M:%S"),
                ),
            )
        page = client.get("/quiz?campaign=daily_test")
        assert page.status_code == 200
        assert 'data-schedule-state="upcoming"' in page.text
        meta = client.get("/api/quiz/questions?campaign=daily_test")
        assert meta.status_code == 200
        body = meta.json()
        assert body["schedule_state"] == "upcoming"
        assert body["questions_count"] == 10
        identity = client.get("/api/quiz/identity?campaign=daily_test")
        assert identity.status_code == 200
        assert identity.json()["verified"] is True
        assert identity.json()["method"] == "member"
        assert client.post(
            "/api/quiz/start",
            json={"campaign": "daily_test"},
        ).status_code == 403


def test_daily_414_lobby_ranks_by_correct_count(
    tmp_path: Path,
) -> None:
    client, settings = make_member_client(tmp_path)
    with client:
        first_id = seed_daily_member(client, settings)
        seed_daily_campaign(settings)
        with transaction(settings.db_path) as conn:
            second = int(
                conn.execute(
                    """
                    INSERT INTO clients(phone_raw, phone_local, first_name)
                    VALUES ('79001112233', '9001112233', 'Second')
                    """
                ).lastrowid
            )
            account_id = int(
                conn.execute(
                    """
                    INSERT INTO member_accounts(
                        client_id, email, email_normalized, password_hash,
                        email_verified_at
                    ) VALUES (?, 'second@example.test', 'second@example.test', ?,
                              CURRENT_TIMESTAMP)
                    """,
                    (second, hash_password("abcdef")),
                ).lastrowid
            )
            for document in conn.execute(
                "SELECT * FROM legal_documents WHERE is_active=1"
            ).fetchall():
                conn.execute(
                    """
                    INSERT INTO member_consents(
                        account_id, document_id, document_code, document_version,
                        ip_hash, user_agent
                    ) VALUES (?, ?, ?, ?, 'test-ip-2', 'pytest')
                    """,
                    (
                        account_id,
                        document["id"],
                        document["code"],
                        document["version"],
                    ),
                )
            # first finishes with 8, second with 9 — second must be place 1
            conn.execute(
                """
                INSERT INTO quiz_submissions(
                    campaign_code, campaign_version, client_id, phone_raw, phone_local,
                    answers_json, questions_snapshot_json, score, max_score,
                    correct_count, max_correct_count, passed,
                    completion_time_ms, main_prize_eligible, jackcoin_awarded, ip_hash
                ) VALUES
                  ('daily_test', 1, ?, '', '', '{}', '[]', 8, 10, 8, 10, 1, 20000, 1, 40, 'h1'),
                  ('daily_test', 1, ?, '', '', '{}', '[]', 9, 10, 9, 10, 1, 25000, 1, 50, 'h2')
                """,
                (first_id, second),
            )
            token = issue_session(
                conn,
                secret_key=settings.secret_key,
                account_id=account_id,
                session_version=1,
                days=30,
                ip_hash="test-ip-2",
                user_agent="pytest",
            )
        client.cookies.set(MEMBER_COOKIE_NAME, token)
        lobby = client.get("/api/quiz/final-table/status?campaign=daily_test")
        assert lobby.status_code == 200
        body = lobby.json()
        assert body["state"] == "lobby"
        assert body["correct_count"] == 9
        assert body["provisional_place"] == 1


def test_daily_414_full_game_awards_jackcoin_and_locks_answers(
    tmp_path: Path,
) -> None:
    client, settings = make_member_client(tmp_path)
    with client:
        client_id = seed_daily_member(client, settings)
        seed_daily_campaign(settings)

        page = client.get("/quiz?campaign=daily_test")
        assert page.status_code == 200
        assert 'data-campaign-type="daily_414"' in page.text
        assert "СЕСТЬ ЗА СТОЛ" in page.text
        assert "РИВЕР" in page.text

        meta = client.get(
            "/api/quiz/questions?campaign=daily_test"
        ).json()
        assert meta["campaign_type"] == "daily_414"
        assert meta["questions_count"] == 10
        assert meta["time_limit_seconds"] == 254
        assert meta["max_attempts"] == 1
        assert meta["member_authenticated"] is True

        started = client.post(
            "/api/quiz/start",
            json={"campaign": "daily_test"},
        )
        assert started.status_code == 200
        attempt = started.json()
        assert attempt["campaign_type"] == "daily_414"
        assert [item["game_stage"] for item in attempt["questions"]] == [
            "preflop",
            "preflop",
            "flop",
            "flop",
            "flop",
            "turn",
            "turn",
            "turn",
            "river",
            "river",
        ]
        assert attempt["questions"][-1]["river_reveal"] is True
        assert all(len(item.get("options") or []) >= 2 for item in attempt["questions"])
        quiz_js = Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "quiz.js"
        quiz_html = Path(__file__).resolve().parents[1] / "app" / "templates" / "quiz.html"
        js_text = quiz_js.read_text(encoding="utf-8")
        html_text = quiz_html.read_text(encoding="utf-8")
        assert 'data-role="question-options"' in js_text
        assert 'data-role="question-options"' in html_text
        assert 'class="quiz-options final-question-options"' not in html_text
        assert "final-question-options" in html_text
        assert "requestDailySeat" in js_text
        assert "startQuizAfterCountdown" in js_text
        assert "zeroFetchPending" in js_text
        assert "provisional_place ?? result?.daily_place" in js_text or "data.provisional_place ?? result?.daily_place" in js_text

        token = attempt["attempt_token"]
        first = client.post(
            "/api/quiz/answer",
            json={
                "attempt_token": token,
                "question_id": "d1",
                "answer": "d1_yes",
            },
        )
        assert first.status_code == 200
        changed = client.post(
            "/api/quiz/answer",
            json={
                "attempt_token": token,
                "question_id": "d1",
                "answer": "d1_no",
            },
        )
        assert changed.status_code == 409
        skipped = client.post(
            "/api/quiz/answer",
            json={
                "attempt_token": token,
                "question_id": "d3",
                "answer": "d3_yes",
            },
        )
        assert skipped.status_code == 409

        for index in range(2, 11):
            saved = client.post(
                "/api/quiz/answer",
                json={
                    "attempt_token": token,
                    "question_id": f"d{index}",
                    "answer": f"d{index}_yes",
                },
            )
            assert saved.status_code == 200

        finished = client.post(
            "/api/quiz/finish",
            json={"attempt_token": token},
        )
        assert finished.status_code == 200
        result = finished.json()
        assert result["campaign_type"] == "daily_414"
        assert result["correct_count"] == 10
        assert result["jackcoin_awarded"] == 80
        assert result["jackcoin_breakdown"] == {
            "total": 80,
            "answers": 50,
            "completion": 10,
            "perfect": 20,
            "streak_bonus": 0,
            "streak_days": 1,
            "best_streak": 1,
        }
        assert result["main_prize_eligible"] is True
        assert result["daily_place"] == 1
        assert result["final_table_available"] is True
        assert result["final_table_starts_at"]
        assert result["retry_allowed"] is False

        lobby = client.get(
            "/api/quiz/final-table/status?campaign=daily_test"
        )
        assert lobby.status_code == 200
        assert lobby.json()["state"] == "lobby"
        assert lobby.json()["provisional_place"] == 1

        blocked = client.post(
            "/api/quiz/start",
            json={"campaign": "daily_test"},
        )
        assert blocked.status_code == 409

        # Closing the lobby and reopening the link must return to final flow,
        # not treat the finished attempt as a hard lockout with no recovery.
        resumed = client.get(
            "/api/quiz/final-table/status?campaign=daily_test"
        )
        assert resumed.status_code == 200
        assert resumed.json()["state"] == "lobby"

        with transaction(settings.db_path) as conn:
            conn.execute(
                """
                UPDATE quiz_campaigns
                SET active_until=?
                WHERE code='daily_test'
                """,
                (
                    (
                        datetime.now(ZoneInfo(settings.timezone_name)).replace(
                            tzinfo=None
                        )
                        - timedelta(minutes=1)
                    ).strftime("%Y-%m-%dT%H:%M:%S"),
                ),
            )
        after_window = client.get(
            "/api/quiz/final-table/status?campaign=daily_test"
        )
        assert after_window.status_code == 200
        assert after_window.json()["state"] == "lobby"

        account = client.get("/account?tab=stats")
        assert "+80 JC" in account.text

    with connect(settings.db_path) as conn:
        submission = conn.execute(
            "SELECT * FROM quiz_submissions WHERE campaign_code='daily_test'"
        ).fetchone()
        assert submission["jackcoin_awarded"] == 80
        assert submission["streak_days"] == 1
        assert submission["main_prize_eligible"] == 1
        assert submission["completion_time_ms"] >= 0
        assert conn.execute(
            "SELECT SUM(amount) FROM jackcoin_ledger WHERE client_id=?",
            (client_id,),
        ).fetchone()[0] == 80
        progress = conn.execute(
            "SELECT * FROM daily_414_progress WHERE client_id=?",
            (client_id,),
        ).fetchone()
        assert progress["current_streak"] == 1


def test_daily_414_final_answer_is_saved_once(tmp_path: Path) -> None:
    client, settings = make_member_client(tmp_path)
    with client:
        client_id = seed_daily_member(client, settings)
        local_now = datetime.now(ZoneInfo(settings.timezone_name)).replace(
            tzinfo=None
        )
        with transaction(settings.db_path) as conn:
            conn.execute(
                """
                INSERT INTO quiz_campaigns(
                    code, title, campaign_type, quiz_time_limit_seconds,
                    max_attempts, current_version, active_from, active_until
                ) VALUES (
                    'daily_live_final', 'Живой финал', 'daily_414',
                    254, 1, 1, ?, ?
                )
                """,
                (
                    (local_now - timedelta(minutes=9, seconds=15)).isoformat(
                        timespec="seconds"
                    ),
                    (local_now + timedelta(hours=1)).isoformat(
                        timespec="seconds"
                    ),
                ),
            )
            question_id = int(
                conn.execute(
                    """
                    INSERT INTO quiz_questions(
                        campaign_code, code, type, title, game_round,
                        position, is_active
                    ) VALUES (
                        'daily_live_final', 'final_live_1',
                        'single_choice', 'Кто победил?', 'final', 10, 1
                    )
                    """
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO quiz_options(
                    question_id, code, text, is_correct, position
                ) VALUES
                    (?, 'final_live_yes', 'Первый игрок', 1, 10),
                    (?, 'final_live_no', 'Второй игрок', 0, 20)
                """,
                (question_id, question_id),
            )
            second_client_id = int(
                conn.execute(
                    """
                    INSERT INTO clients(
                        first_name, phone_raw, phone_local, source
                    ) VALUES ('Соперник', '9990000002', '9990000002', 'test')
                    """
                ).lastrowid
            )
            for submission_client_id, phone, completion in (
                (client_id, "9990000001", 20_000),
                (second_client_id, "9990000002", 21_000),
            ):
                conn.execute(
                    """
                    INSERT INTO quiz_submissions(
                        campaign_code, campaign_version, client_id,
                        phone_raw, phone_local, answers_json, correct_count,
                        max_correct_count, completion_time_ms,
                        main_prize_eligible, ip_hash
                    ) VALUES (
                        'daily_live_final', 1, ?, ?, ?, '{}', 10, 10, ?, 1, ?
                    )
                    """,
                    (
                        submission_client_id,
                        phone,
                        phone,
                        completion,
                        f"ip-{submission_client_id}",
                    ),
                )

        status = client.get(
            "/api/quiz/final-table/status?campaign=daily_live_final"
        )
        assert status.status_code == 200
        assert status.json()["state"] == "final_question"
        assert status.json()["active_count"] == 2

        saved = client.post(
            "/api/quiz/final-table/answer",
            json={
                "campaign": "daily_live_final",
                "question_index": 0,
                "answer": "final_live_yes",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["answered"] is True

        changed = client.post(
            "/api/quiz/final-table/answer",
            json={
                "campaign": "daily_live_final",
                "question_index": 0,
                "answer": "final_live_no",
            },
        )
        assert changed.status_code == 409
