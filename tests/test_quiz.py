import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import connect, init_db, transaction
from app.main import create_app
from app.services.clients import upsert_client
from app.services.quiz import (
    load_questions,
    normalize_text_answer,
    parse_quick_questions,
    score_answers,
    validate_answers,
)


def make_client(tmp_path: Path) -> tuple[TestClient, Settings]:
    settings = Settings(
        admin_pin="2468",
        admin_name="Test Admin",
        secret_key="test-secret-key-that-is-longer-than-32-characters",
        db_path=tmp_path / "quiz.sqlite3",
        secure_cookie=False,
    )
    return TestClient(create_app(settings)), settings


def login(client: TestClient) -> None:
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/login",
        data={"username": "master", "pin": "2468", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303


def complete_quiz(
    client: TestClient,
    campaign: str,
    answer_by_question: dict | None = None,
    *,
    phone: str = "9991112233",
    username: str = "ivan_test",
    **identity,
) -> str:
    response = client.post(
        "/api/quiz/start",
        json={"campaign": campaign, "phone": phone, "username": username, "name": "Иван", "nickname": "Vanya", **identity},
    )
    assert response.status_code == 200
    data = response.json()
    attempt_token = data["attempt_token"]
    for question in data["questions"]:
        if answer_by_question and question["id"] in answer_by_question:
            answer = answer_by_question[question["id"]]
        elif question["type"] == "single_choice":
            answer = question["options"][0]["id"]
        elif question["type"] == "multi_choice":
            answer = [question["options"][0]["id"]]
        else:
            answer = ""
        response = client.post(
            "/api/quiz/answer",
            json={"attempt_token": attempt_token, "question_id": question["id"], "answer": answer},
        )
        assert response.status_code == 200
    return attempt_token


def submit(
    client: TestClient,
    *,
    campaign: str,
    phone: str,
    answer_by_question: dict | None = None,
    **extra,
):
    token = complete_quiz(
        client, campaign, answer_by_question, phone=phone,
        username=extra.pop("username", f"user_{''.join(ch for ch in phone if ch.isdigit())[-6:]}"), **extra,
    )
    return client.post("/api/quiz/finish", json={"attempt_token": token})


def test_questions_support_campaign_fallback(tmp_path):
    path = Path(__file__).resolve().parents[1] / "data" / "quiz_questions.json"
    default = load_questions(path, "default")
    summer = load_questions(path, "summer")
    assert summer == default
    valid = {question["id"]: ([question["options"][0]["id"]] if question["type"] == "multi_choice" else question.get("options", [{}])[0].get("id", "")) for question in default}
    assert validate_answers(default, valid)["q1"] == "classic"


def test_scoring_requires_exact_match_for_multiple_choice():
    questions = [
        {
            "id": "single", "type": "single_choice", "points": 2,
            "options": [{"id": "a", "correct": True}, {"id": "b", "correct": False}],
        },
        {
            "id": "multi", "type": "multi_choice", "points": 3,
            "options": [
                {"id": "x", "correct": True}, {"id": "y", "correct": True},
                {"id": "z", "correct": False},
            ],
        },
        {"id": "text", "type": "text", "points": 100, "options": []},
    ]
    perfect = score_answers(questions, {"single": "a", "multi": ["x", "y"], "text": "answer"})
    assert perfect == {"score": 5, "max_score": 5, "correct_count": 2, "max_correct_count": 2}
    partial = score_answers(questions, {"single": "a", "multi": ["x"]})
    assert partial["score"] == 2
    assert partial["correct_count"] == 1


def test_text_answers_ignore_case_punctuation_spaces_and_yo() -> None:
    question = {
        "id": "rebus",
        "type": "text",
        "points": 4,
        "options": [],
        "accepted_text_answers": ["Флеш-рояль", "Королевский флеш"],
    }
    assert normalize_text_answer("  ФЛЕШ-РОЯЛЬ... ") == "флешрояль"
    assert normalize_text_answer("флеш рояль") == "флешрояль"
    assert normalize_text_answer("Всё.") == normalize_text_answer("все")
    result = score_answers([question], {"rebus": "флеш-РОЯЛЬ."})
    assert result == {"score": 4, "max_score": 4, "correct_count": 1, "max_correct_count": 1}
    wrong = score_answers([question], {"rebus": "стрит"})
    assert wrong == {"score": 0, "max_score": 4, "correct_count": 0, "max_correct_count": 1}


def test_quick_question_parser_supports_single_and_multiple_correct_answers():
    parsed = parse_quick_questions(
        """Какая комбинация старше?
- Стрит
* Флеш

Выберите красные масти
* Червы
* Бубны
- Пики"""
    )
    assert [item["question_type"] for item in parsed] == ["single_choice", "multi_choice"]
    assert parsed[0]["options"][1] == {"text": "Флеш", "is_correct": True}
    with pytest.raises(ValueError, match="звёздочкой"):
        parse_quick_questions("Вопрос\n- Первый\n- Второй")


def test_quiz_is_public_on_quiz_host_and_admin_results_are_private(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        root = client.get("/", headers={"host": "quiz.hijackpoker.ru"}, follow_redirects=False)
        assert root.status_code == 302
        assert root.headers["location"] == "/quiz"
        assert client.get("/quiz?campaign=default").status_code == 200
        assert 'data-action="back"' in client.get("/quiz?campaign=default").text
        assert client.get("/api/quiz/questions?campaign=summer").status_code == 200
        assert client.get("/admin/quiz-results", follow_redirects=False).status_code == 303
        assert client.get("/api/quiz/results").status_code == 401


def test_server_uses_one_deadline_and_allows_answer_revision(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        with transaction(settings.db_path) as conn:
            conn.execute("UPDATE quiz_campaigns SET quiz_time_limit_seconds=30 WHERE code='default'")

        metadata = client.get("/api/quiz/questions?campaign=default")
        assert metadata.status_code == 200
        assert metadata.json()["questions"] == []
        assert metadata.json()["questions_count"] == 4

        started = client.post("/api/quiz/start", json={"campaign": "default", "phone": "9991112233", "username": "timer_user"})
        assert started.status_code == 200
        first = started.json()
        assert [question["id"] for question in first["questions"]] == ["q1", "q2", "q3", "q4"]
        assert first["time_limit_seconds"] == 30
        assert "correct" not in first["questions"][0]["options"][0]
        assert first["deadline_at"]

        answered = client.post(
            "/api/quiz/answer",
            json={"attempt_token": first["attempt_token"], "question_id": "q1", "answer": "classic"},
        )
        assert answered.status_code == 200
        revised = client.post(
            "/api/quiz/answer",
            json={"attempt_token": first["attempt_token"], "question_id": "q1", "answer": "short_deck"},
        )
        assert revised.status_code == 200

        with transaction(settings.db_path) as conn:
            conn.execute(
                "UPDATE quiz_attempts SET attempt_deadline_at='2000-01-01T00:00:00+00:00' WHERE status='in_progress'"
            )
        expired = client.post(
            "/api/quiz/answer",
            json={"attempt_token": first["attempt_token"], "question_id": "q2", "answer": ["mon"]},
        )
        assert expired.status_code == 409
        finished = client.post("/api/quiz/finish", json={"attempt_token": first["attempt_token"]})
        assert finished.status_code == 200
        with connect(settings.db_path) as conn:
            stored = json.loads(conn.execute("SELECT answers_json FROM quiz_attempts").fetchone()[0])
        assert stored["q1"] == "short_deck"
        assert stored["q2"] == []


def test_quiz_cannot_be_submitted_before_server_attempt_is_completed(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        missing = client.post("/api/quiz/start", json={"campaign": "default"})
        assert missing.status_code == 422
        started = client.post("/api/quiz/start", json={"campaign": "default", "username": "identity_user"})
        assert started.status_code == 200
        premature = client.post(
            "/api/quiz/submit",
            json={"attempt_token": started.json()["attempt_token"]},
        )
        assert premature.status_code == 410


def test_submission_creates_new_client_and_blocks_repeat_after_success(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        first = submit(
            client,
            campaign="default",
            phone="+7 999 123-45-67",
            referrer_id="485",
            source="tg_post",
        )
        assert first.status_code == 200
        second = client.post("/api/quiz/start", json={"campaign": "default", "phone": "89991234567"})
        assert second.status_code == 409
    with connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM quiz_submissions").fetchone()[0] == 1
        row = conn.execute("SELECT * FROM quiz_submissions ORDER BY id LIMIT 1").fetchone()
        assert row["phone_local"] == "9991234567"
        assert row["quiz_referrer_id"] == "485"
        assert row["source"] == "tg_post"
        assert row["is_new_client"] == 1
        client_row = conn.execute("SELECT * FROM clients").fetchone()
        assert client_row["client_status"] == "new"
        assert client_row["acquisition_campaign_code"] == "default"


def test_existing_client_is_linked_without_overwriting_name(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        with transaction(settings.db_path) as conn:
            client_id, _ = upsert_client(conn, {"app_user_id": "42", "first_name": "Старое имя", "phone_raw": "9992223344"})
        response = submit(client, campaign="default", phone="+7 999 222-33-44")
        assert response.status_code == 200
    with connect(settings.db_path) as conn:
        row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        assert row["first_name"] == "Старое имя"
        assert row["nickname"] == "Vanya"
        assert row["source"] != "quiz"


def test_campaign_reward_code_is_redeemed_once(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        with transaction(settings.db_path) as conn:
            conn.execute(
                "UPDATE quiz_campaigns SET bonus_preference_code='free_reentry', bonus_amount=1, reward_delivery_mode='code' WHERE code='honor_more'"
            )
        first = submit(client, campaign="honor_more", phone="9993334455")
        assert first.status_code == 200
        assert first.json()["bonus_granted"] is False
        assert first.json()["reward_code"].startswith("HJ-")
        login(client)
        rewards_page = client.get("/admin/rewards")
        token = re.search(r'name="csrf_token" value="([^"]+)"', rewards_page.text).group(1)
        redeemed = client.post(
            "/api/rewards/redeem",
            data={"csrf_token": token, "code": first.json()["reward_code"]},
            follow_redirects=False,
        )
        assert redeemed.status_code == 303
        second = client.post("/api/quiz/start", json={"campaign": "honor_more", "phone": "79993334455"})
        assert second.status_code == 409
    with connect(settings.db_path) as conn:
        balance = conn.execute(
            """
            SELECT cp.balance_int FROM client_preferences cp
            JOIN preference_types pt ON pt.id=cp.preference_type_id
            WHERE pt.code='free_reentry'
            """
        ).fetchone()[0]
        assert balance == 1
        log = conn.execute("SELECT * FROM preference_log").fetchone()
        assert log["reason"] == "quiz_reward_redeemed"
        assert log["admin_name"] == "Test Admin"
        assert conn.execute("SELECT status FROM quiz_reward_codes").fetchone()[0] == "used"


def test_multiple_results_page_and_csv(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        for index in range(5):
            response = submit(client, campaign="summer", phone=f"99900000{index:02d}", username=f"user_{index}")
            assert response.status_code == 200
        login(client)
        page = client.get("/admin/quiz-results")
        assert page.status_code == 200
        assert "Результаты квиза" in page.text
        assert all(title in page.text for title in ("Телефон", "Username", "Набрано баллов", "Порог", "Бонус", "Дата прохождения", "Наименование", "Действие"))
        exported = client.get("/api/quiz/results.csv")
        assert exported.status_code == 200
        assert "submission_id" in exported.text
    with connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM quiz_submissions").fetchone()[0] == 5


def test_quiz_result_time_is_displayed_in_configured_timezone(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        response = submit(client, campaign="summer", phone="9990000011", username="timezone_user")
        assert response.status_code == 200
        with transaction(settings.db_path) as conn:
            submission_id = conn.execute(
                "SELECT id FROM quiz_submissions WHERE username='timezone_user'"
            ).fetchone()[0]
            conn.execute(
                "UPDATE quiz_submissions SET created_at='2026-07-29 09:42:36' WHERE id=?",
                (submission_id,),
            )
        login(client)
        results_page = client.get("/admin/quiz-results")
        detail_page = client.get(f"/admin/quiz-results/{submission_id}")

        assert "29.07.2026 12:42" in results_page.text
        assert "29.07.2026 12:42:36" in detail_page.text
        assert "2026-07-29 09:42:36" not in detail_page.text


def test_master_can_configure_quiz_campaign(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        login(client)
        page = client.get("/master")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        created = client.post(
            "/api/master/quiz-campaigns/create",
            data={
                "code": "weekend",
                "title": "Опрос выходного дня",
                "bonus_preference_code": "free_entry",
                "bonus_amount": 2,
                "quiz_time_limit_seconds": 180,
                "active_from": "2026-08-01T18:00",
                "active_until": "2026-08-01T23:00",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        assert client.get("/quiz?campaign=weekend").status_code in {200, 403, 410}
    with connect(settings.db_path) as conn:
        campaign = conn.execute("SELECT * FROM quiz_campaigns WHERE code='weekend'").fetchone()
        assert campaign["bonus_preference_code"] == "free_entry"
        assert campaign["bonus_amount"] == 2
        assert campaign["quiz_time_limit_seconds"] == 180
        assert campaign["active_from"] == "2026-08-01T18:00"
        assert campaign["active_until"] == "2026-08-01T23:00"


def test_master_separates_archives_and_restores_campaigns_safely(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        login(client)
        page = client.get("/master?tab=campaigns")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        assert 'data-campaign-tab="classic"' in page.text
        assert 'data-campaign-tab="daily_414"' in page.text
        assert 'data-campaign-tab="archived"' in page.text
        stylesheet = client.get("/static/css/app.css")
        assert stylesheet.status_code == 200
        assert "[data-campaign-kind][hidden]{display:none!important}" in stylesheet.text

        created = client.post(
            "/api/master/quiz-campaigns/create",
            data={
                "code": "archive_test",
                "title": "Тест архива",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        with connect(settings.db_path) as conn:
            campaign_id = conn.execute(
                "SELECT id FROM quiz_campaigns WHERE code='archive_test'"
            ).fetchone()[0]

        archived = client.post(
            f"/api/master/quiz-campaigns/{campaign_id}/archive",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert archived.status_code == 303
        assert client.get("/quiz?campaign=archive_test").status_code == 404
        archive_page = client.get("/master?tab=campaigns")
        assert "Тест архива" in archive_page.text
        assert "Восстановить" in archive_page.text
        assert ">Удалить</button>" in archive_page.text

        restored = client.post(
            f"/api/master/quiz-campaigns/{campaign_id}/restore",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert restored.status_code == 303
        with connect(settings.db_path) as conn:
            campaign = conn.execute(
                "SELECT is_active, archived_at FROM quiz_campaigns WHERE id=?",
                (campaign_id,),
            ).fetchone()
            assert tuple(campaign) == (0, None)

        enabled = client.post(
            f"/api/master/quiz-campaigns/{campaign_id}/toggle",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert enabled.status_code == 303
        with connect(settings.db_path) as conn:
            assert conn.execute(
                "SELECT is_active FROM quiz_campaigns WHERE id=?",
                (campaign_id,),
            ).fetchone()[0] == 1


def test_master_deletes_empty_campaigns_and_hides_preserved_history(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        login(client)
        page = client.get("/master?tab=campaigns")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        with transaction(settings.db_path) as conn:
            empty_id = conn.execute(
                """
                INSERT INTO quiz_campaigns(
                    code, title, is_active, archived_at
                ) VALUES ('empty_delete', 'Пустой тест', 0, CURRENT_TIMESTAMP)
                """
            ).lastrowid
            question_id = conn.execute(
                """
                INSERT INTO quiz_questions(
                    campaign_code, code, type, title
                ) VALUES ('empty_delete', 'q1', 'single_choice', 'Вопрос')
                """
            ).lastrowid
            conn.execute(
                """
                INSERT INTO quiz_options(
                    question_id, code, text, is_correct
                ) VALUES (?, 'a', 'Ответ', 1)
                """,
                (question_id,),
            )
            history_id = conn.execute(
                """
                INSERT INTO quiz_campaigns(
                    code, title, is_active, archived_at
                ) VALUES ('history_keep', 'Тест с историей', 0, CURRENT_TIMESTAMP)
                """
            ).lastrowid
            conn.execute(
                """
                INSERT INTO quiz_attempts(
                    campaign_code, token_hash, questions_snapshot_json, ip_hash
                ) VALUES ('history_keep', 'history-token', '[]', 'history-ip')
                """
            )
            system_id = conn.execute(
                """
                UPDATE quiz_campaigns
                SET is_active=0, archived_at=CURRENT_TIMESTAMP
                WHERE code='default'
                RETURNING id
                """
            ).fetchone()[0]

        archive_page = client.get("/master?tab=campaigns")
        assert archive_page.text.count(">Удалить</button>") >= 3

        deleted = client.post(
            f"/api/master/quiz-campaigns/{empty_id}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert deleted.status_code == 303
        history_deleted = client.post(
            f"/api/master/quiz-campaigns/{history_id}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert history_deleted.status_code == 303
        assert "error=" not in history_deleted.headers["location"]
        system_deleted = client.post(
            f"/api/master/quiz-campaigns/{system_id}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert system_deleted.status_code == 303
        assert "error=" not in system_deleted.headers["location"]

        master_page = client.get("/master?tab=campaigns")
        assert "<h3>Пустой тест</h3>" not in master_page.text
        assert "<h3>Тест с историей</h3>" not in master_page.text
        assert "<h3>Опрос Hi, Jack!</h3>" not in master_page.text

    with connect(settings.db_path) as conn:
        assert conn.execute(
            "SELECT 1 FROM quiz_campaigns WHERE id=?",
            (empty_id,),
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM quiz_questions WHERE campaign_code='empty_delete'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM quiz_options WHERE question_id=?",
            (question_id,),
        ).fetchone() is None
        history = conn.execute(
            "SELECT deleted_at FROM quiz_campaigns WHERE id=?",
            (history_id,),
        ).fetchone()
        system = conn.execute(
            "SELECT deleted_at FROM quiz_campaigns WHERE id=?",
            (system_id,),
        ).fetchone()
        assert history["deleted_at"]
        assert system["deleted_at"]
        assert conn.execute(
            "SELECT 1 FROM quiz_attempts WHERE campaign_code='history_keep'"
        ).fetchone()

    init_db(settings.db_path)
    with connect(settings.db_path) as conn:
        assert conn.execute(
            "SELECT deleted_at FROM quiz_campaigns WHERE id=?",
            (system_id,),
        ).fetchone()["deleted_at"]


def test_master_rejects_invalid_campaign_period_and_keeps_single_preview_tab(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        login(client)
        page = client.get("/master?tab=campaigns")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        assert 'target="hj-quiz-preview"' in page.text
        invalid = client.post(
            "/api/master/quiz-campaigns/create",
            data={
                "code": "invalid_period",
                "title": "Некорректный период",
                "active_from": "2026-08-02T20:00",
                "active_until": "2026-08-02T19:00",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert invalid.status_code == 303
        assert "error=" in invalid.headers["location"]
    with connect(settings.db_path) as conn:
        assert conn.execute("SELECT 1 FROM quiz_campaigns WHERE code='invalid_period'").fetchone() is None


def test_quiz_campaign_schedule_blocks_early_and_late_starts(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        with transaction(settings.db_path) as conn:
            conn.execute(
                "UPDATE quiz_campaigns SET active_from='2999-01-01T12:00', active_until=NULL WHERE code='default'"
            )
        early_page = client.get("/quiz?campaign=default")
        assert early_page.status_code == 200
        assert 'data-schedule-state="upcoming"' in early_page.text
        assert "До начала турнира осталось" in early_page.text
        assert client.post("/api/quiz/start", json={"campaign": "default"}).status_code == 403

        with transaction(settings.db_path) as conn:
            conn.execute(
                "UPDATE quiz_campaigns SET active_from=NULL, active_until='2000-01-01T12:00' WHERE code='default'"
            )
        late_page = client.get("/quiz?campaign=default")
        assert late_page.status_code == 200
        assert 'data-schedule-state="ended"' in late_page.text
        assert "Время участия в этом турнире закончилось" in late_page.text
        assert client.post("/api/quiz/start", json={"campaign": "default"}).status_code == 410

        with transaction(settings.db_path) as conn:
            conn.execute("UPDATE quiz_campaigns SET active_from=NULL, active_until=NULL WHERE code='default'")
        assert client.get("/quiz?campaign=default").status_code == 200


def test_master_can_create_complete_question_in_one_action(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        login(client)
        with connect(settings.db_path) as conn:
            campaign_id = conn.execute("SELECT id FROM quiz_campaigns WHERE code='default'").fetchone()[0]
        page = client.get(f"/master/quiz-builder/{campaign_id}")
        token = re.search(r'data-csrf-token="([^"]+)"', page.text).group(1)
        assert "Добавить несколько вопросов текстом" in page.text
        assert "Сохранить вопрос" in page.text
        created = client.post(
            f"/api/master/quiz-campaigns/{campaign_id}/questions/create-complete",
            json={
                "csrf_token": token,
                "title": "Сколько карт открывают на флопе?",
                "question_type": "single_choice",
                "points": 2,
                "time_limit_seconds": 9,
                "required": True,
                "publish": True,
                "options": [
                    {"text": "Две", "is_correct": False},
                    {"text": "Три", "is_correct": True},
                    {"text": "Четыре", "is_correct": False},
                ],
            },
        )
        assert created.status_code == 200
        assert created.json()["message"] == "Вопрос сохранён"
    with connect(settings.db_path) as conn:
        question = conn.execute(
            "SELECT * FROM quiz_questions WHERE title='Сколько карт открывают на флопе?'"
        ).fetchone()
        assert question["is_active"] == 1
        assert question["points"] == 2
        assert question["time_limit_seconds"] == 9
        options = conn.execute(
            "SELECT text, is_correct, position FROM quiz_options WHERE question_id=? ORDER BY position",
            (question["id"],),
        ).fetchall()
        assert [tuple(row) for row in options] == [
            ("Две", 0, 10), ("Три", 1, 20), ("Четыре", 0, 30),
        ]

    with client:
        page = client.get(f"/master/quiz-builder/{campaign_id}")
        token = re.search(r'data-csrf-token="([^"]+)"', page.text).group(1)
        updated = client.post(
            f"/api/master/quiz-questions/{question['id']}/update-complete",
            json={
                "csrf_token": token,
                "title": "Сколько общих карт открывают на флопе?",
                "question_type": "single_choice",
                "points": 3,
                "required": True,
                "options": [
                    {"text": "Три", "is_correct": True},
                    {"text": "Пять", "is_correct": False},
                ],
            },
        )
        assert updated.status_code == 200
        assert updated.json()["message"] == "Изменения сохранены"
    with connect(settings.db_path) as conn:
        changed = conn.execute("SELECT title, points, is_active FROM quiz_questions WHERE id=?", (question["id"],)).fetchone()
        assert tuple(changed) == ("Сколько общих карт открывают на флопе?", 3.0, 1)
        assert conn.execute("SELECT COUNT(*) FROM quiz_options WHERE question_id=?", (question["id"],)).fetchone()[0] == 2


def test_complete_question_update_preserves_option_identity_and_order(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        login(client)
        with connect(settings.db_path) as conn:
            campaign_id = conn.execute(
                "SELECT id FROM quiz_campaigns WHERE code='default'"
            ).fetchone()[0]
        page = client.get(f"/master/quiz-builder/{campaign_id}")
        token = re.search(r'data-csrf-token="([^"]+)"', page.text).group(1)
        created = client.post(
            f"/api/master/quiz-campaigns/{campaign_id}/questions/create-complete",
            json={
                "csrf_token": token,
                "title": "Стабильные варианты",
                "question_type": "single_choice",
                "options": [
                    {"text": "Вариант A", "is_correct": True},
                    {"text": "Вариант B", "is_correct": False},
                    {"text": "Вариант C", "is_correct": False},
                    {"text": "Вариант D", "is_correct": False},
                ],
            },
        )
        assert created.status_code == 200
        question_id = created.json()["question_id"]
        original_options = created.json()["options"]

        updated = client.post(
            f"/api/master/quiz-questions/{question_id}/update-complete",
            json={
                "csrf_token": token,
                "title": "Стабильные варианты после загрузки картинки",
                "question_type": "single_choice",
                "options": [
                    {
                        "option_id": option["db_id"],
                        "option_code": option["id"],
                        "text": f"{option['text']} обновлён",
                        "is_correct": index == 0,
                    }
                    for index, option in enumerate(original_options)
                ],
            },
        )
        assert updated.status_code == 200
        saved_options = updated.json()["options"]

    assert [option["db_id"] for option in saved_options] == [
        option["db_id"] for option in original_options
    ]
    assert [option["id"] for option in saved_options] == [
        option["id"] for option in original_options
    ]
    assert [option["position"] for option in saved_options] == [10, 20, 30, 40]


def test_master_can_score_rebus_with_normalized_text_answers(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        login(client)
        with connect(settings.db_path) as conn:
            campaign_id = conn.execute("SELECT id FROM quiz_campaigns WHERE code='default'").fetchone()[0]
        page = client.get(f"/master/quiz-builder/{campaign_id}")
        token = re.search(r'data-csrf-token="([^"]+)"', page.text).group(1)
        created = client.post(
            f"/api/master/quiz-campaigns/{campaign_id}/questions/create-complete",
            json={
                "csrf_token": token,
                "title": "Разгадайте ребус",
                "question_type": "text",
                "visual_type": "rebus",
                "image_path": "/quiz-media/default/rebus.webp",
                "points": 5,
                "required": True,
                "accepted_text_answers": "Флеш-рояль\nКоролевский флеш",
                "options": [],
            },
        )
        assert created.status_code == 200
        builder = client.get(f"/master/quiz-builder/{campaign_id}")
        assert "Флеш-рояль" in builder.text
        assert "Регистр, точки, запятые" in builder.text
        with transaction(settings.db_path) as conn:
            conn.execute(
                "UPDATE quiz_questions SET is_active=(title='Разгадайте ребус') WHERE campaign_code='default'"
            )
            conn.execute("UPDATE quiz_campaigns SET pass_score=5 WHERE code='default'")
        started = client.post(
            "/api/quiz/start",
            json={"campaign": "default", "phone": "9992223344", "username": "rebus_player"},
        )
        assert started.status_code == 200
        public_rebus = next(item for item in started.json()["questions"] if item["title"] == "Разгадайте ребус")
        assert "accepted_text_answers" not in public_rebus
        saved = client.post(
            "/api/quiz/answer",
            json={
                "attempt_token": started.json()["attempt_token"],
                "question_id": public_rebus["id"],
                "answer": "  ФЛЕШ-РОЯЛЬ... ",
            },
        )
        assert saved.status_code == 200
        finished = client.post(
            "/api/quiz/finish",
            json={"attempt_token": started.json()["attempt_token"]},
        )
        assert finished.status_code == 200
        assert finished.json()["score"] == 5
        assert finished.json()["passed"] is True

    with connect(settings.db_path) as conn:
        row = conn.execute(
            "SELECT points, accepted_text_answers_json FROM quiz_questions WHERE title='Разгадайте ребус'"
        ).fetchone()
        assert row["points"] == 5
        assert json.loads(row["accepted_text_answers_json"]) == ["Флеш-рояль", "Королевский флеш"]


def test_master_bulk_creation_is_atomic_and_detects_question_types(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        login(client)
        with connect(settings.db_path) as conn:
            campaign_id = conn.execute("SELECT id FROM quiz_campaigns WHERE code='honor_more'").fetchone()[0]
        page = client.get(f"/master/quiz-builder/{campaign_id}")
        token = re.search(r'data-csrf-token="([^"]+)"', page.text).group(1)
        created = client.post(
            f"/api/master/quiz-campaigns/{campaign_id}/questions/bulk-create",
            json={
                "csrf_token": token,
                "text": "Первая проверка\n* Да\n- Нет\n\nВторая проверка\n* Первый\n* Второй\n- Третий",
                "points": 1,
                "time_limit_seconds": 0,
                "publish": False,
            },
        )
        assert created.status_code == 200
        assert created.json()["created"] == 2
        with connect(settings.db_path) as conn:
            before_invalid = conn.execute(
                "SELECT COUNT(*) FROM quiz_questions WHERE campaign_code='honor_more'"
            ).fetchone()[0]
        invalid = client.post(
            f"/api/master/quiz-campaigns/{campaign_id}/questions/bulk-create",
            json={
                "csrf_token": token,
                "text": "Корректный блок\n* Да\n- Нет\n\nСломанный блок\n- Один\n- Два",
                "publish": True,
            },
        )
        assert invalid.status_code == 422
    with connect(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT title, type, is_active FROM quiz_questions WHERE title IN ('Первая проверка', 'Вторая проверка') ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("Первая проверка", "single_choice", 1),
            ("Вторая проверка", "multi_choice", 1),
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM quiz_questions WHERE campaign_code='honor_more'"
        ).fetchone()[0] == before_invalid


def test_master_can_duplicate_move_and_delete_question(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        login(client)
        with connect(settings.db_path) as conn:
            campaign_id = conn.execute("SELECT id FROM quiz_campaigns WHERE code='default'").fetchone()[0]
            source = conn.execute(
                "SELECT id FROM quiz_questions WHERE campaign_code='default' ORDER BY position, id LIMIT 1"
            ).fetchone()[0]
        page = client.get(f"/master/quiz-builder/{campaign_id}")
        token = re.search(r'data-csrf-token="([^"]+)"', page.text).group(1)
        duplicated = client.post(
            f"/api/master/quiz-questions/{source}/duplicate",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert duplicated.status_code == 303
        with connect(settings.db_path) as conn:
            copy = conn.execute(
                "SELECT id, is_active FROM quiz_questions WHERE title LIKE '%— копия' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert copy["is_active"] == 1
            assert conn.execute("SELECT COUNT(*) FROM quiz_options WHERE question_id=?", (copy["id"],)).fetchone()[0] >= 2
        moved = client.post(
            f"/api/master/quiz-questions/{copy['id']}/move",
            data={"csrf_token": token, "direction": "up"},
            follow_redirects=False,
        )
        assert moved.status_code == 303
        deleted = client.post(
            f"/api/master/quiz-questions/{copy['id']}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert deleted.status_code == 303
    with connect(settings.db_path) as conn:
        assert conn.execute("SELECT 1 FROM quiz_questions WHERE id=?", (copy["id"],)).fetchone() is None


def test_full_builder_scores_answers_and_grants_bonus_only_after_threshold(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        login(client)
        master_page = client.get("/master?tab=campaigns")
        token = re.search(r'name="csrf_token" value="([^"]+)"', master_page.text).group(1)
        created = client.post(
            "/api/master/quiz-campaigns/create",
            data={
                "code": "knowledge",
                "title": "Покерный тест",
                "bonus_preference_code": "free_entry",
                "bonus_amount": 1,
                "reward_delivery_mode": "code",
                "pass_score": 2,
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        with connect(settings.db_path) as conn:
            campaign_id = conn.execute("SELECT id FROM quiz_campaigns WHERE code='knowledge'").fetchone()[0]

        builder = client.get(f"/master/quiz-builder/{campaign_id}")
        assert builder.status_code == 200
        builder_token = re.search(r'data-csrf-token="([^"]+)"', builder.text).group(1)
        question_created = client.post(
            f"/api/master/quiz-campaigns/{campaign_id}/questions/create-complete",
            json={
                "csrf_token": builder_token,
                "title": "Какая комбинация старше?",
                "question_type": "single_choice",
                "required": True,
                "points": 2,
                "options": [
                    {"text": "Стрит", "is_correct": False},
                    {"text": "Флеш", "is_correct": True},
                ],
            },
        )
        assert question_created.status_code == 200
        with connect(settings.db_path) as conn:
            question = conn.execute("SELECT id, code FROM quiz_questions WHERE campaign_code='knowledge'").fetchone()

        public = client.get("/api/quiz/questions?campaign=knowledge")
        assert public.status_code == 200
        assert public.json()["questions"] == []
        assert public.json()["questions_count"] == 1
        started = client.post("/api/quiz/start", json={"campaign": "knowledge", "phone": "9990001122", "username": "preview_user"})
        assert started.status_code == 200
        public_question = started.json()["questions"][0]
        assert all("correct" not in option for option in public_question["options"])
        with connect(settings.db_path) as conn:
            options = conn.execute("SELECT code, is_correct FROM quiz_options WHERE question_id=?", (question["id"],)).fetchall()
            correct_code = next(row["code"] for row in options if row["is_correct"])
            wrong_code = next(row["code"] for row in options if not row["is_correct"])

        correct_result = submit(
            client,
            campaign="knowledge",
            phone="9994445566",
            name="Знаток",
            answer_by_question={question["code"]: correct_code},
        )
        assert correct_result.status_code == 200
        assert correct_result.json()["score"] == 2
        assert correct_result.json()["correct_count"] == 1
        assert correct_result.json()["passed"] is True
        assert correct_result.json()["bonus_granted"] is False
        assert correct_result.json()["outcome"] == "won"
        assert correct_result.json()["reward_code"].startswith("HJ-")

        wrong_result = submit(
            client,
            campaign="knowledge",
            phone="9994445577",
            name="Новичок",
            answer_by_question={question["code"]: wrong_code},
        )
        assert wrong_result.status_code == 200
        assert wrong_result.json()["score"] == 0
        assert wrong_result.json()["passed"] is False
        assert wrong_result.json()["bonus_granted"] is False
        assert wrong_result.json()["outcome"] == "not_won"
        assert wrong_result.json()["retry_allowed"] is True

        with transaction(settings.db_path) as conn:
            conn.execute("UPDATE quiz_questions SET title='Изменённый после прохождения вопрос' WHERE id=?", (question["id"],))
        results_page = client.get("/admin/quiz-results?campaign=knowledge")
        assert results_page.status_code == 200
        assert "Какая комбинация старше?" not in results_page.text
        assert "Покерный тест" in results_page.text
        with connect(settings.db_path) as conn:
            correct_submission_id = conn.execute(
                "SELECT id FROM quiz_submissions WHERE campaign_code='knowledge' ORDER BY id LIMIT 1"
            ).fetchone()[0]
        detail_page = client.get(f"/admin/quiz-results/{correct_submission_id}")
        assert detail_page.status_code == 200
        assert "Какая комбинация старше?" in detail_page.text
        assert "1 из 1" in detail_page.text

    with connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM quiz_reward_codes WHERE campaign_code='knowledge'").fetchone()[0] == 1
        submissions = conn.execute("SELECT score, passed, bonus_granted FROM quiz_submissions WHERE campaign_code='knowledge' ORDER BY id").fetchall()
        assert [tuple(row) for row in submissions] == [(2.0, 1, 0), (0.0, 0, 0)]
        assert conn.execute("SELECT COUNT(*) FROM quiz_submissions WHERE questions_snapshot_json IS NOT NULL").fetchone()[0] >= 2
