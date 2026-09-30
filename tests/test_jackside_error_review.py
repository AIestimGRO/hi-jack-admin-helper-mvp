from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import connect, transaction
from app.main import create_app
from app.services.auth import hash_pin
from app.services.member_accounts import MEMBER_COOKIE_NAME, issue_session


def make_client(tmp_path: Path) -> tuple[TestClient, Settings]:
    settings = Settings(
        admin_pin="2468",
        admin_name="Test Admin",
        secret_key="error-review-test-secret-key-that-is-longer-than-32-characters",
        db_path=tmp_path / "error-review.sqlite3",
        secure_cookie=False,
        public_base_url="https://club.example.test",
        quiz_public_base_url="https://quiz.example.test",
        member_portal_enabled=True,
    )
    test_client = TestClient(create_app(settings), base_url=settings.public_base_url)
    # TestClient lifespan is not entered here. Seed the master explicitly so the
    # admin HTTP checks exercise the real login and CSRF flow deterministically.
    with transaction(settings.db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM admins WHERE username=? COLLATE NOCASE",
            (settings.master_login,),
        ).fetchone()
        encoded = hash_pin(settings.admin_pin)
        if existing:
            conn.execute(
                """
                UPDATE admins
                SET pin_hash=?, display_name=?, role='master_admin', is_active=1
                WHERE id=?
                """,
                (encoded, settings.admin_name, int(existing["id"])),
            )
        else:
            conn.execute(
                """
                INSERT INTO admins(username,display_name,pin_hash,role,is_active)
                VALUES (?,?,?,'master_admin',1)
                """,
                (settings.master_login, settings.admin_name, encoded),
            )
    return test_client, settings


def csrf_from(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def login_master(client: TestClient) -> None:
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


def seed_member(settings: Settings, client: TestClient, *, suffix: str = "1") -> int:
    with transaction(settings.db_path) as conn:
        client_id = int(
            conn.execute(
                """
                INSERT INTO clients(
                    first_name,phone_raw,phone_full,phone_local,source
                ) VALUES (?,?,?,?, 'test')
                """,
                (
                    f"Player {suffix}",
                    f"+7999000000{suffix}",
                    f"+7999000000{suffix}",
                    f"999000000{suffix}",
                ),
            ).lastrowid
        )
        account_id = int(
            conn.execute(
                """
                INSERT INTO member_accounts(
                    client_id,email,email_normalized,password_hash,email_verified_at
                ) VALUES (?,?,?,?,CURRENT_TIMESTAMP)
                """,
                (
                    client_id,
                    f"player{suffix}@example.test",
                    f"player{suffix}@example.test",
                    "unused-test-hash",
                ),
            ).lastrowid
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


def seed_campaign_and_questions(
    settings: Settings,
    *,
    code: str,
    price_jc: int | None = None,
) -> tuple[int, list[dict]]:
    with transaction(settings.db_path) as conn:
        campaign_id = int(
            conn.execute(
                "INSERT INTO quiz_campaigns(code,title,campaign_type) VALUES (?,?,'daily_414')",
                (code, "JACKSIDE Review Test"),
            ).lastrowid
        )
        if price_jc is not None:
            conn.execute(
                """
                INSERT INTO jackside_error_review_settings(campaign_code,price_jc)
                VALUES (?,?)
                """,
                (code, price_jc),
            )
        q1_id = int(
            conn.execute(
                """
                INSERT INTO quiz_questions(
                    campaign_code,code,type,title,game_round,is_active,position
                ) VALUES (?,'q1','single_choice','Первый вопрос','main',1,10)
                """,
                (code,),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO quiz_options(question_id,code,text,is_correct,position)
            VALUES (?, 'a', 'Неверный вариант', 0, 10),
                   (?, 'b', 'Верный вариант', 1, 20)
            """,
            (q1_id, q1_id),
        )
        q2_id = int(
            conn.execute(
                """
                INSERT INTO quiz_questions(
                    campaign_code,code,type,title,game_round,is_active,position
                ) VALUES (?,'q2','multi_choice','Второй вопрос','main',1,20)
                """,
                (code,),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO quiz_options(question_id,code,text,is_correct,position)
            VALUES (?, 'x', 'Правильный X', 1, 10),
                   (?, 'y', 'Правильный Y', 1, 20),
                   (?, 'z', 'Лишний Z', 0, 30)
            """,
            (q2_id, q2_id, q2_id),
        )
        conn.execute(
            "INSERT INTO quiz_question_explanations(question_id,explanation) VALUES (?,?)",
            (q1_id, "Объяснение A для первого вопроса"),
        )
        conn.execute(
            "INSERT INTO quiz_question_explanations(question_id,explanation) VALUES (?,?)",
            (q2_id, "Объяснение A для второго вопроса"),
        )

    questions = [
        {
            "id": "q1",
            "type": "single_choice",
            "title": "Первый вопрос",
            "visual_type": "standard",
            "options": [
                {"id": "a", "text": "Неверный вариант", "correct": False},
                {"id": "b", "text": "Верный вариант", "correct": True},
            ],
        },
        {
            "id": "q2",
            "type": "multi_choice",
            "title": "Второй вопрос",
            "visual_type": "standard",
            "options": [
                {"id": "x", "text": "Правильный X", "correct": True},
                {"id": "y", "text": "Правильный Y", "correct": True},
                {"id": "z", "text": "Лишний Z", "correct": False},
            ],
        },
    ]
    return campaign_id, questions


def insert_submission_and_award(
    settings: Settings,
    *,
    client_id: int,
    campaign_code: str,
    questions: list[dict],
    answers: dict,
    correct_count: int,
    max_correct_count: int,
    award_jc: int,
    main_prize_eligible: int = 0,
) -> int:
    with transaction(settings.db_path) as conn:
        submission_id = int(
            conn.execute(
                """
                INSERT INTO quiz_submissions(
                    campaign_code,client_id,phone_raw,phone_local,
                    answers_json,questions_snapshot_json,
                    score,max_score,correct_count,max_correct_count,
                    main_prize_eligible,jackcoin_awarded,main_round_completed,
                    timed_out,ip_hash
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,0,'test-ip')
                """,
                (
                    campaign_code,
                    client_id,
                    "+79990000001",
                    "9990000001",
                    json.dumps(answers, ensure_ascii=False),
                    json.dumps(questions, ensure_ascii=False),
                    correct_count,
                    max_correct_count,
                    correct_count,
                    max_correct_count,
                    main_prize_eligible,
                    award_jc,
                ),
            ).lastrowid
        )
        if award_jc:
            conn.execute(
                """
                INSERT INTO jackcoin_ledger(
                    client_id,amount,operation_type,source_type,source_id,
                    idempotency_key,comment
                ) VALUES (?,?,'earn','daily_414',?,?,'same quiz award')
                """,
                (
                    client_id,
                    award_jc,
                    str(submission_id),
                    f"test:daily-award:{submission_id}",
                ),
            )
    return submission_id


def test_same_quiz_jackcoin_can_immediately_buy_review_and_purchase_is_idempotent(
    tmp_path: Path,
) -> None:
    client, settings = make_client(tmp_path)
    client_id = seed_member(settings, client)
    _, questions = seed_campaign_and_questions(settings, code="jackside_review_1")
    submission_id = insert_submission_and_award(
        settings,
        client_id=client_id,
        campaign_code="jackside_review_1",
        questions=questions,
        answers={"q1": "a", "q2": ["x", "z"]},
        correct_count=0,
        max_correct_count=2,
        award_jc=30,
    )

    locked = client.get(
        f"/api/quiz/error-review?campaign=jackside_review_1&submission_id={submission_id}"
    )
    assert locked.status_code == 402

    status = client.get("/api/quiz/error-review/status?campaign=jackside_review_1")
    assert status.status_code == 200
    assert status.json() == {
        "eligible": True,
        "purchased": False,
        "campaign": "jackside_review_1",
        "submission_id": submission_id,
        "price_jc": 30,
        "balance_jc": 30,
        "wrong_count": 2,
        "can_purchase": True,
        "reason": "ok",
    }

    purchase = client.post(
        "/api/quiz/error-review/purchase",
        json={"campaign": "jackside_review_1", "submission_id": submission_id},
    )
    assert purchase.status_code == 200
    assert purchase.json()["balance_jc"] == 0
    assert purchase.json()["already_purchased"] is False

    repeated = client.post(
        "/api/quiz/error-review/purchase",
        json={"campaign": "jackside_review_1", "submission_id": submission_id},
    )
    assert repeated.status_code == 200
    assert repeated.json()["balance_jc"] == 0
    assert repeated.json()["already_purchased"] is True

    review = client.get(
        f"/api/quiz/error-review?campaign=jackside_review_1&submission_id={submission_id}"
    )
    assert review.status_code == 200
    payload = review.json()
    assert payload["wrong_count"] == 2
    assert payload["questions"][0]["explanation"] == "Объяснение A для первого вопроса"
    assert payload["questions"][0]["options"] == [
        {
            "id": "a",
            "text": "Неверный вариант",
            "selected": True,
            "correct": False,
        },
        {
            "id": "b",
            "text": "Верный вариант",
            "selected": False,
            "correct": True,
        },
    ]
    assert [
        (item["id"], item["selected"], item["correct"])
        for item in payload["questions"][1]["options"]
    ] == [
        ("x", True, True),
        ("y", False, True),
        ("z", True, False),
    ]

    with transaction(settings.db_path) as conn:
        conn.execute(
            """
            UPDATE quiz_question_explanations
            SET explanation='Объяснение B после завершения'
            WHERE question_id=(SELECT id FROM quiz_questions WHERE campaign_code=? AND code='q1')
            """,
            ("jackside_review_1",),
        )
    review_again = client.get(
        f"/api/quiz/error-review?campaign=jackside_review_1&submission_id={submission_id}"
    )
    assert review_again.json()["questions"][0]["explanation"] == "Объяснение A для первого вопроса"

    with connect(settings.db_path) as conn:
        spend_rows = conn.execute(
            """
            SELECT amount,operation_type,source_type,source_id
            FROM jackcoin_ledger
            WHERE idempotency_key=?
            """,
            (f"quiz:error-review:{submission_id}",),
        ).fetchall()
        assert len(spend_rows) == 1
        assert int(spend_rows[0]["amount"]) == -30
        assert spend_rows[0]["operation_type"] == "spend"
        assert spend_rows[0]["source_type"] == "quiz_error_review"
        assert spend_rows[0]["source_id"] == str(submission_id)


def test_price_is_snapshotted_and_existing_plus_new_jackcoin_are_combined(
    tmp_path: Path,
) -> None:
    client, settings = make_client(tmp_path)
    client_id = seed_member(settings, client, suffix="2")
    _, questions = seed_campaign_and_questions(
        settings,
        code="jackside_review_2",
        price_jc=35,
    )
    with transaction(settings.db_path) as conn:
        conn.execute(
            """
            INSERT INTO jackcoin_ledger(
                client_id,amount,operation_type,source_type,source_id,idempotency_key,comment
            ) VALUES (?,15,'earn','test','old','test:old-balance','old balance')
            """,
            (client_id,),
        )
    submission_id = insert_submission_and_award(
        settings,
        client_id=client_id,
        campaign_code="jackside_review_2",
        questions=questions,
        answers={"q1": "a", "q2": ["x", "z"]},
        correct_count=0,
        max_correct_count=2,
        award_jc=20,
    )
    with transaction(settings.db_path) as conn:
        conn.execute(
            "UPDATE jackside_error_review_settings SET price_jc=99 WHERE campaign_code=?",
            ("jackside_review_2",),
        )

    status = client.get("/api/quiz/error-review/status?campaign=jackside_review_2").json()
    assert status["price_jc"] == 35
    assert status["balance_jc"] == 35
    assert status["can_purchase"] is True

    purchase = client.post(
        "/api/quiz/error-review/purchase",
        json={"campaign": "jackside_review_2", "submission_id": submission_id},
    )
    assert purchase.status_code == 200
    assert purchase.json()["price_jc"] == 35
    assert purchase.json()["balance_jc"] == 0


def test_insufficient_balance_finalist_and_perfect_result_are_not_charged(
    tmp_path: Path,
) -> None:
    client, settings = make_client(tmp_path)
    client_id = seed_member(settings, client, suffix="3")
    _, questions = seed_campaign_and_questions(
        settings,
        code="jackside_review_3",
        price_jc=50,
    )
    submission_id = insert_submission_and_award(
        settings,
        client_id=client_id,
        campaign_code="jackside_review_3",
        questions=questions,
        answers={"q1": "a", "q2": ["x", "z"]},
        correct_count=0,
        max_correct_count=2,
        award_jc=30,
    )
    purchase = client.post(
        "/api/quiz/error-review/purchase",
        json={"campaign": "jackside_review_3", "submission_id": submission_id},
    )
    assert purchase.status_code == 409
    with connect(settings.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM jackcoin_ledger WHERE source_type='quiz_error_review'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT purchased_at FROM jackside_error_review_snapshots WHERE submission_id=?",
            (submission_id,),
        ).fetchone()["purchased_at"] is None

    finalist_id = insert_submission_and_award(
        settings,
        client_id=client_id,
        campaign_code="jackside_review_3",
        questions=questions,
        answers={"q1": "a", "q2": ["x", "z"]},
        correct_count=0,
        max_correct_count=2,
        award_jc=0,
        main_prize_eligible=1,
    )
    perfect_id = insert_submission_and_award(
        settings,
        client_id=client_id,
        campaign_code="jackside_review_3",
        questions=questions,
        answers={"q1": "b", "q2": ["x", "y"]},
        correct_count=2,
        max_correct_count=2,
        award_jc=0,
    )
    with connect(settings.db_path) as conn:
        finalist = conn.execute(
            "SELECT eligible FROM jackside_error_review_snapshots WHERE submission_id=?",
            (finalist_id,),
        ).fetchone()
        perfect = conn.execute(
            "SELECT eligible FROM jackside_error_review_snapshots WHERE submission_id=?",
            (perfect_id,),
        ).fetchone()
        assert int(finalist["eligible"]) == 0
        assert int(perfect["eligible"]) == 0


def test_admin_builder_exposes_configurable_price_and_question_explanation(
    tmp_path: Path,
) -> None:
    client, settings = make_client(tmp_path)
    campaign_id, _ = seed_campaign_and_questions(settings, code="jackside_review_admin")
    login_master(client)

    builder = client.get(f"/master/quiz-builder/{campaign_id}")
    assert builder.status_code == 200
    assert "/static/js/jackside-error-review-admin.js" in builder.text
    csrf = csrf_from(builder)

    config = client.get(
        f"/api/master/quiz-campaigns/{campaign_id}/error-review-config"
    )
    assert config.status_code == 200
    assert config.json()["price_jc"] == 30
    q1 = next(item for item in config.json()["questions"] if item["title"] == "Первый вопрос")

    price = client.post(
        f"/api/master/quiz-campaigns/{campaign_id}/error-review-price",
        json={"csrf_token": csrf, "price_jc": 44},
    )
    assert price.status_code == 200
    assert price.json()["price_jc"] == 44

    explanation = client.post(
        f"/api/master/quiz-questions/{q1['id']}/error-review-explanation",
        json={"csrf_token": csrf, "explanation": "Новый комментарий мастера"},
    )
    assert explanation.status_code == 200

    refreshed = client.get(
        f"/api/master/quiz-campaigns/{campaign_id}/error-review-config"
    ).json()
    assert refreshed["price_jc"] == 44
    updated_q1 = next(item for item in refreshed["questions"] if item["id"] == q1["id"])
    assert updated_q1["explanation"] == "Новый комментарий мастера"

    # The JACKSIDE page requires a member session. Authenticate one before
    # asserting the member-side review asset instead of following the login redirect.
    seed_member(settings, client, suffix="9")
    quiz_page = client.get("/quiz?campaign=jackside_review_admin")
    assert quiz_page.status_code == 200
    assert 'data-campaign-type="daily_414"' in quiz_page.text
    assert "/static/js/jackside-error-review.js" in quiz_page.text
