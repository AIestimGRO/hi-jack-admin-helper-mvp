from __future__ import annotations

import json
import re
import urllib.parse
from base64 import b64decode
from pathlib import Path

import itsdangerous
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import connect, transaction
from app.main import create_app
from app.services.quiz_device import DEVICE_COOKIE_NAME, device_token_hash
from app.services.quiz_retention import cleanup_quiz_data


def make_client(tmp_path: Path, **overrides) -> tuple[TestClient, Settings]:
    values = {
        "admin_pin": "2468",
        "admin_name": "Test Admin",
        "secret_key": "test-secret-key-that-is-longer-than-32-characters",
        "db_path": tmp_path / "quiz-v2.sqlite3",
        "secure_cookie": False,
    }
    values.update(overrides)
    settings = Settings(**values)
    return TestClient(create_app(settings)), settings


def answer_all(client: TestClient, started: dict, *, correct: str | None = None) -> dict:
    token = started["attempt_token"]
    for question in started["questions"]:
        if correct is not None:
            answer = correct
        elif question["type"] == "single_choice":
            answer = question["options"][0]["id"]
        elif question["type"] == "multi_choice":
            answer = [question["options"][0]["id"]]
        else:
            answer = ""
        response = client.post(
            "/api/quiz/answer",
            json={"attempt_token": token, "question_id": question["id"], "answer": answer},
        )
        assert response.status_code == 200
    finished = client.post("/api/quiz/finish", json={"attempt_token": token})
    assert finished.status_code == 200
    return finished.json()


def login(client: TestClient) -> str:
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/login", data={"username": "master", "pin": "2468", "csrf_token": token}, follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/admin/rewards")
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def test_referral_link_counts_unique_completions_and_issues_separate_reward(tmp_path):
    client, settings = make_client(tmp_path, quiz_public_base_url="https://quiz-v2.hijackpoker.ru")
    with client:
        with transaction(settings.db_path) as conn:
            conn.execute(
                """
                UPDATE quiz_campaigns SET referral_enabled=1, referral_preference_code='free_entry',
                    referral_amount=1, referral_threshold=2, referral_repeatable=1,
                    referral_max_rewards=0, pass_score=999 WHERE code='default'
                """
            )

        owner = client.post(
            "/api/quiz/start", json={"campaign": "default", "phone": "9991000001", "username": "owner"},
        ).json()
        owner_result = answer_all(client, owner)
        assert owner_result["share_url"].startswith("https://quiz-v2.hijackpoker.ru/quiz?campaign=default&ref=")
        referral_code = urllib.parse.parse_qs(urllib.parse.urlparse(owner_result["share_url"]).query)["ref"][0]

        first = client.post(
            "/api/quiz/start",
            json={"campaign": "default", "phone": "9991000002", "username": "friend_one", "referrer_id": referral_code},
        ).json()
        assert answer_all(client, first)["referral_reward_issued"] is False

        second = client.post(
            "/api/quiz/start",
            json={"campaign": "default", "phone": "9991000003", "username": "friend_two", "referrer_id": referral_code},
        ).json()
        assert answer_all(client, second)["referral_reward_issued"] is True

        self_attempt = client.post(
            "/api/quiz/start",
            json={"campaign": "default", "phone": "9991000001", "username": "owner", "referrer_id": referral_code},
        ).json()
        answer_all(client, self_attempt)

        with connect(settings.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM quiz_referrals").fetchone()[0] == 2
            reward = conn.execute(
                "SELECT * FROM quiz_reward_codes WHERE reward_kind='referral'"
            ).fetchone()
            assert reward["client_id"] == conn.execute("SELECT id FROM clients WHERE username='owner'").fetchone()[0]
            assert reward["referral_milestone"] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM quiz_reward_codes WHERE reward_kind='quiz'"
            ).fetchone()[0] == 0

        login(client)
        stats = client.get("/admin/quiz-results")
        assert "Реферальная статистика" in stats.text
        assert "owner" in stats.text
        assert re.search(r"owner.*?<td>2</td><td>1</td>", stats.text, re.DOTALL)


def test_attempt_resumes_and_configured_limit_cannot_be_bypassed(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        with transaction(settings.db_path) as conn:
            conn.execute("UPDATE quiz_campaigns SET pass_score=999, max_attempts=2 WHERE code='default'")
        first = client.post(
            "/api/quiz/start", json={"campaign": "default", "phone": "9991234567", "username": "resume_user"},
        ).json()
        saved = client.post(
            "/api/quiz/answer",
            json={"attempt_token": first["attempt_token"], "question_id": "q1", "answer": "classic"},
        )
        assert saved.status_code == 200
        resumed_response = client.post(
            "/api/quiz/start", json={"campaign": "default", "phone": "+7 999 123-45-67"},
        )
        assert resumed_response.status_code == 200
        resumed = resumed_response.json()
        assert resumed["resumed"] is True
        assert resumed["attempt_number"] == 1
        assert resumed["answers"]["q1"] == "classic"
        assert resumed["deadline_at"] == first["deadline_at"]
        assert client.post(
            "/api/quiz/answer",
            json={"attempt_token": first["attempt_token"], "question_id": "q1", "answer": "bounty"},
        ).status_code == 409
        assert answer_all(client, resumed)["retry_allowed"] is True

        second = client.post(
            "/api/quiz/start", json={"campaign": "default", "phone": "89991234567"},
        )
        assert second.status_code == 200
        assert second.json()["attempt_number"] == 2
        assert answer_all(client, second.json())["retry_allowed"] is False
        blocked = client.post("/api/quiz/start", json={"campaign": "default", "phone": "9991234567"})
        assert blocked.status_code == 429
    with connect(settings.db_path) as conn:
        summary = conn.execute("SELECT * FROM quiz_participation_summary").fetchone()
        assert summary["attempts_used"] == 2
        assert conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 1


def test_username_only_creates_new_client_with_acquisition_campaign(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        started = client.post(
            "/api/quiz/start",
            json={"campaign": "summer", "username": "@new_player", "source": "telegram_post", "referrer_id": "app-42"},
        )
        assert started.status_code == 200
    with connect(settings.db_path) as conn:
        row = conn.execute("SELECT * FROM clients").fetchone()
        assert row["phone_local"] is None
        assert row["username"] == "new_player"
        assert row["client_status"] == "new"
        assert row["acquisition_campaign_code"] == "summer"
        assert row["acquisition_source"] == "telegram_post"
        source = conn.execute("SELECT * FROM client_quiz_campaigns").fetchone()
        assert source["first_referrer_id"] == "app-42"


def test_public_identity_screen_offers_telegram_or_phone_first(tmp_path):
    client, _ = make_client(
        tmp_path, telegram_client_id="123456789", telegram_client_secret="telegram-client-secret",
    )
    with client:
        page = client.get("/quiz")
    assert page.status_code == 200
    assert "Подтвердить через Telegram" in page.text
    assert 'data-action="phone-identity">Номер телефона</button>' in page.text
    assert 'class="quiz-contact quiz-identity" autocomplete="on" hidden' in page.text
    assert 'name="phone"' in page.text
    assert 'name="username"' in page.text
    assert 'name="name"' in page.text
    assert 'name="nickname"' in page.text
    assert "Получить код" not in page.text
    assert "Да, продолжить" in page.text
    assert "Нет, это не я" in page.text


def test_verified_client_is_remembered_by_opaque_device_token(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        started = client.post(
            "/api/quiz/start",
            json={
                "campaign": "default",
                "phone": "9994445566",
                "username": "remember_me",
                "name": "Роксана",
            },
        )
        assert started.status_code == 200
        device_token = client.cookies.get(DEVICE_COOKIE_NAME)
        assert device_token
        assert "9994445566" not in device_token
        assert "Роксана" not in device_token
        set_cookie = "\n".join(started.headers.get_list("set-cookie")).lower()
        assert "httponly" in set_cookie
        assert "samesite=lax" in set_cookie

        with connect(settings.db_path) as conn:
            stored = conn.execute("SELECT * FROM quiz_device_tokens").fetchone()
            assert stored["token_hash"] == device_token_hash(settings.secret_key, device_token)
            assert device_token not in stored["token_hash"]

        client.cookies.delete("hjc_admin_session")
        remembered = client.get("/api/quiz/identity?campaign=default")
        assert remembered.status_code == 200
        assert remembered.json() == {
            "verified": False,
            "remembered": True,
            "display_name": "Роксана",
        }

        confirmed = client.post("/api/quiz/identity/confirm", json={"campaign": "default"})
        assert confirmed.status_code == 200
        assert confirmed.json()["identity"] == {
            "verified": True,
            "method": "device",
            "display_name": "Роксана",
        }
        signed_session = client.cookies.get("hjc_admin_session")
        session_payload = json.loads(
            b64decode(itsdangerous.TimestampSigner(settings.secret_key).unsign(signed_session))
        )
        assert session_payload["quiz_verified_identity"] == {
            "campaign": "default",
            "method": "device",
            "client_id": 1,
        }
        assert "9994445566" not in json.dumps(session_payload, ensure_ascii=False)
        assert "Роксана" not in json.dumps(session_payload, ensure_ascii=False)
        resumed = client.post("/api/quiz/start", json={"campaign": "default"})
        assert resumed.status_code == 200
        assert resumed.json()["resumed"] is True

    with connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM quiz_attempts").fetchone()[0] == 1


def test_remembered_client_can_forget_only_this_browser(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        started = client.post(
            "/api/quiz/start",
            json={"campaign": "default", "phone": "9994445577", "nickname": "Rocky"},
        )
        assert started.status_code == 200
        assert client.cookies.get(DEVICE_COOKIE_NAME)

        forgotten = client.post("/api/quiz/identity/forget", json={})
        assert forgotten.status_code == 200
        assert DEVICE_COOKIE_NAME not in client.cookies
        identity = client.get("/api/quiz/identity?campaign=default").json()
        assert identity["remembered"] is False
        assert identity["verified"] is False
        assert client.post("/api/quiz/start", json={"campaign": "default"}).status_code == 422

    with connect(settings.db_path) as conn:
        assert conn.execute(
            "SELECT revoked_at IS NOT NULL FROM quiz_device_tokens"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM quiz_attempts").fetchone()[0] == 1


def test_remembered_identity_survives_new_quiz_version(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        first = client.post(
            "/api/quiz/start",
            json={"campaign": "default", "phone": "9994445588", "name": "Олег"},
        )
        assert first.status_code == 200
        with transaction(settings.db_path) as conn:
            conn.execute(
                "UPDATE quiz_campaigns SET current_version=2 WHERE code='default'"
            )

        client.cookies.delete("hjc_admin_session")
        remembered = client.get("/api/quiz/identity?campaign=default")
        assert remembered.json()["display_name"] == "Олег"
        assert client.post(
            "/api/quiz/identity/confirm", json={"campaign": "default"}
        ).status_code == 200
        second = client.post("/api/quiz/start", json={"campaign": "default"})
        assert second.status_code == 200
        assert second.json()["resumed"] is False
        assert second.json()["attempt_number"] == 1

    with connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 1
        versions = {
            row[0] for row in conn.execute(
                "SELECT campaign_version FROM quiz_attempts ORDER BY campaign_version"
            )
        }
        assert versions == {1, 2}


def test_verified_telegram_identity_can_start_required_campaign(tmp_path, monkeypatch):
    client, settings = make_client(
        tmp_path, telegram_client_id="123456789", telegram_client_secret="telegram-client-secret",
    )
    monkeypatch.setattr(
        "app.main_impl.exchange_telegram_code",
        lambda **_: {"sub": "88776655", "name": "Иван", "preferred_username": "telegram_user"},
    )
    with client:
        with transaction(settings.db_path) as conn:
            conn.execute("UPDATE quiz_campaigns SET verification_required=1 WHERE code='default'")
        blocked = client.post(
            "/api/quiz/start", json={"campaign": "default", "username": "telegram_user"},
        )
        assert blocked.status_code == 403
        authorize = client.get(
            "/quiz/telegram/start?campaign=default&source=tg_post&referrer_id=campaign-42",
            follow_redirects=False,
        )
        assert authorize.status_code == 302
        query = urllib.parse.parse_qs(urllib.parse.urlparse(authorize.headers["location"]).query)
        assert query["code_challenge_method"] == ["S256"]
        verified = client.get(
            f"/quiz/telegram/callback?code=test-code&state={query['state'][0]}", follow_redirects=False,
        )
        assert verified.status_code == 303
        return_query = urllib.parse.parse_qs(urllib.parse.urlparse(verified.headers["location"]).query)
        assert return_query["source"] == ["tg_post"]
        assert return_query["referrer_id"] == ["campaign-42"]
        assert return_query["telegram_verified"] == ["1"]
        started = client.post(
            "/api/quiz/start",
            json={"campaign": "default", "source": "tg_post", "referrer_id": "campaign-42"},
        )
        assert started.status_code == 200
    with connect(settings.db_path) as conn:
        row = conn.execute("SELECT telegram_user_id, username FROM clients").fetchone()
        assert tuple(row) == ("88776655", "telegram_user")
        campaign = conn.execute("SELECT first_source, first_referrer_id FROM client_quiz_campaigns").fetchone()
        assert tuple(campaign) == ("tg_post", "campaign-42")


def test_email_code_verifies_identity_without_sms(tmp_path, monkeypatch):
    sent: dict[str, str] = {}

    def fake_send(**kwargs):
        sent.update({"recipient": kwargs["recipient"], "code": kwargs["code"]})

    monkeypatch.setattr("app.main_impl.send_quiz_email_code", fake_send)
    client, settings = make_client(
        tmp_path, smtp_host="smtp.example.test", smtp_from="club@example.test",
    )
    with client:
        with transaction(settings.db_path) as conn:
            conn.execute("UPDATE quiz_campaigns SET verification_required=1 WHERE code='default'")
        requested = client.post(
            "/api/quiz/email/request",
            json={"campaign": "default", "email": "Player@Example.com", "phone": "9995556677"},
        )
        assert requested.status_code == 200
        assert sent["recipient"] == "player@example.com"
        assert client.post(
            "/api/quiz/email/verify",
            json={"campaign": "default", "email": "player@example.com", "code": "000000"},
        ).status_code == 422
        verified = client.post(
            "/api/quiz/email/verify",
            json={"campaign": "default", "email": "player@example.com", "code": sent["code"]},
        )
        assert verified.status_code == 200
        started = client.post("/api/quiz/start", json={"campaign": "default"})
        assert started.status_code == 200
    with connect(settings.db_path) as conn:
        row = conn.execute("SELECT email_normalized, phone_local FROM clients").fetchone()
        assert tuple(row) == ("player@example.com", "9995556677")


def test_custom_result_text_reward_redemption_and_retention(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        with transaction(settings.db_path) as conn:
            conn.execute(
                """
                UPDATE quiz_campaigns SET bonus_preference_code='free_entry', bonus_amount=1,
                    reward_delivery_mode='code',
                    victory_title='Победа!', victory_text='Баллы: {score}; попытка {attempts_used} из {max_attempts}',
                    reward_validity_mode='days', reward_validity_value=2
                WHERE code='default'
                """
            )
        started = client.post(
            "/api/quiz/start", json={"campaign": "default", "phone": "9997778899", "username": "winner_user"},
        )
        result = answer_all(client, started.json())
        assert result["title"] == "Победа!"
        assert "попытка 1 из 3" in result["message"]
        assert result["reward_code"].startswith("HJ-")
        assert result["reward_valid_until"]
        csrf = login(client)
        redeemed = client.post(
            "/api/rewards/redeem", data={"code": result["reward_code"], "csrf_token": csrf}, follow_redirects=False,
        )
        assert redeemed.status_code == 303
        with transaction(settings.db_path) as conn:
            conn.execute("UPDATE quiz_submissions SET created_at=datetime('now', '-8 days')")
            conn.execute("UPDATE quiz_attempts SET created_at=datetime('now', '-8 days')")
            cleanup_quiz_data(conn, detail_days=7, reward_days=14, action_log_days=31, force=True)
    with connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM quiz_submissions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM quiz_attempts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM quiz_reward_codes").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM quiz_participation_summary").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM preference_log").fetchone()[0] == 1


def test_automatic_reward_and_visual_question_block(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        login(client)
        with transaction(settings.db_path) as conn:
            campaign = conn.execute("SELECT id FROM quiz_campaigns WHERE code='default'").fetchone()
            conn.execute(
                """
                UPDATE quiz_campaigns SET bonus_preference_code='free_entry', bonus_amount=1,
                    reward_delivery_mode='automatic', pass_score=0 WHERE id=?
                """,
                (campaign["id"],),
            )
            section_id = conn.execute(
                "INSERT INTO quiz_sections(campaign_code,title,theme,position) VALUES ('default','Фотораздачи','photo',10)"
            ).lastrowid
            conn.execute(
                """
                UPDATE quiz_questions SET visual_type='photo', image_path='/quiz-media/default/hand.webp',
                    section_id=? WHERE campaign_code='default' AND id=(SELECT MIN(id) FROM quiz_questions WHERE campaign_code='default')
                """,
                (section_id,),
            )
        builder = client.get(f"/master/quiz-builder/{campaign['id']}")
        assert "Тематические блоки" in builder.text
        assert "Фотораздачи" in builder.text
        started = client.post(
            "/api/quiz/start",
            json={"campaign": "default", "phone": "9997779900", "username": "automatic_user"},
        )
        assert started.status_code == 200
        visual = next(item for item in started.json()["questions"] if item["visual_type"] == "photo")
        assert visual["visual_type"] == "photo"
        assert visual["image_path"] == "/quiz-media/default/hand.webp"
        assert visual["section"]["theme"] == "photo"
        result = answer_all(client, started.json())
        assert result["bonus_granted"] is True
        assert result["reward_code"] is None
        assert "начислен" in result["bonus_message"]
    with connect(settings.db_path) as conn:
        balance = conn.execute(
            """
            SELECT cp.balance_int FROM client_preferences cp
            JOIN preference_types pt ON pt.id=cp.preference_type_id
            JOIN clients c ON c.id=cp.client_id
            WHERE c.username='automatic_user' AND pt.code='free_entry'
            """
        ).fetchone()[0]
        assert balance == 1
        reward = conn.execute("SELECT status, code FROM quiz_reward_codes ORDER BY id DESC LIMIT 1").fetchone()
        assert reward["status"] == "used"
        assert reward["code"].startswith("AUTO-")
