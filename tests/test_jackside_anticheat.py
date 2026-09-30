from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import connect, init_db, transaction
from app.jackside_anticheat import (
    ANTI_CHEAT_DEBOUNCE_MS,
    ANTI_CHEAT_SKIP_SENTINEL,
    apply_visibility_skip,
)
from app.main import create_app
from app.services.daily_414 import daily_main_round_completed
from app.services.quiz import attempt_token_hash, score_answers


ROOT = Path(__file__).resolve().parents[1]


def _questions() -> list[dict]:
    return [
        {
            "id": "q1",
            "campaign": "jackside_anticheat",
            "type": "single_choice",
            "title": "Первый вопрос",
            "required": True,
            "points": 1,
            "options": [
                {"id": "a", "text": "Верно A", "correct": True},
                {"id": "b", "text": "Неверно B", "correct": False},
            ],
        },
        {
            "id": "q2",
            "campaign": "jackside_anticheat",
            "type": "single_choice",
            "title": "Второй вопрос",
            "required": True,
            "points": 1,
            "options": [
                {"id": "a", "text": "Неверно A", "correct": False},
                {"id": "b", "text": "Верно B", "correct": True},
            ],
        },
    ]


def _seed_attempt(
    db_path: Path,
    *,
    secret_key: str = "anticheat-secret",
    campaign_type: str = "daily_414",
    answers: dict | None = None,
    current_index: int = 0,
    token: str = "x" * 43,
) -> tuple[str, str]:
    init_db(db_path)
    campaign_code = "jackside_anticheat" if campaign_type == "daily_414" else "regular_anticheat"
    questions = _questions()
    for question in questions:
        question["campaign"] = campaign_code
    token_hash = attempt_token_hash(secret_key, token)
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO quiz_campaigns(code,title,campaign_type) VALUES (?,?,?)",
            (campaign_code, "Anti-cheat test", campaign_type),
        )
        conn.execute(
            """
            INSERT INTO quiz_attempts(
                campaign_code,token_hash,questions_snapshot_json,answers_json,
                current_index,status,ip_hash
            ) VALUES (?,?,?,?,?,'in_progress','test-ip')
            """,
            (
                campaign_code,
                token_hash,
                json.dumps(questions, ensure_ascii=False),
                json.dumps(answers or {}, ensure_ascii=False),
                current_index,
            ),
        )
    return token, token_hash


def test_visibility_under_one_second_does_not_skip_question(tmp_path: Path) -> None:
    db_path = tmp_path / "anticheat.sqlite3"
    _, token_hash = _seed_attempt(db_path)

    with transaction(db_path) as conn:
        result = apply_visibility_skip(
            conn,
            token_hash=token_hash,
            question_id="q1",
            hidden_ms=ANTI_CHEAT_DEBOUNCE_MS - 1,
        )

    assert result == {
        "skipped": False,
        "reason": "debounce_not_reached",
        "debounce_ms": 1000,
    }
    with connect(db_path) as conn:
        attempt = conn.execute("SELECT answers_json,current_index FROM quiz_attempts").fetchone()
    assert json.loads(attempt["answers_json"]) == {}
    assert int(attempt["current_index"]) == 0


def test_visibility_at_one_second_marks_wrong_and_advances_once(tmp_path: Path) -> None:
    db_path = tmp_path / "anticheat.sqlite3"
    _, token_hash = _seed_attempt(db_path)

    with transaction(db_path) as conn:
        first = apply_visibility_skip(
            conn,
            token_hash=token_hash,
            question_id="q1",
            hidden_ms=ANTI_CHEAT_DEBOUNCE_MS,
        )
    assert first["skipped"] is True
    assert first["already_applied"] is False
    assert first["current_index"] == 1
    assert first["finish_required"] is False

    with transaction(db_path) as conn:
        second = apply_visibility_skip(
            conn,
            token_hash=token_hash,
            question_id="q1",
            hidden_ms=5_000,
        )
    assert second["skipped"] is True
    assert second["already_applied"] is True
    assert second["reason"] == "already_skipped"

    with connect(db_path) as conn:
        attempt = conn.execute("SELECT answers_json,current_index FROM quiz_attempts").fetchone()
    answers = json.loads(attempt["answers_json"])
    assert answers == {"q1": ANTI_CHEAT_SKIP_SENTINEL}
    assert int(attempt["current_index"]) == 1


def test_visibility_skip_is_nonempty_but_scores_as_wrong(tmp_path: Path) -> None:
    db_path = tmp_path / "anticheat.sqlite3"
    _, token_hash = _seed_attempt(db_path)
    with transaction(db_path) as conn:
        apply_visibility_skip(
            conn,
            token_hash=token_hash,
            question_id="q1",
            hidden_ms=1_250,
        )
    with connect(db_path) as conn:
        attempt = conn.execute("SELECT answers_json FROM quiz_attempts").fetchone()
    answers = json.loads(attempt["answers_json"])
    answers["q2"] = "b"

    questions = _questions()
    assert daily_main_round_completed(
        timed_out=False,
        questions=questions,
        answers=answers,
    ) is True
    scoring = score_answers(questions, answers)
    assert scoring["correct_count"] == 1
    assert scoring["max_correct_count"] == 2
    assert scoring["score"] == 1
    assert scoring["max_score"] == 2


def test_visibility_skip_never_overwrites_real_answer(tmp_path: Path) -> None:
    db_path = tmp_path / "anticheat.sqlite3"
    _, token_hash = _seed_attempt(db_path, answers={"q1": "a"})

    with transaction(db_path) as conn:
        result = apply_visibility_skip(
            conn,
            token_hash=token_hash,
            question_id="q1",
            hidden_ms=2_000,
        )
    assert result["skipped"] is False
    assert result["reason"] == "already_answered"

    with connect(db_path) as conn:
        attempt = conn.execute("SELECT answers_json FROM quiz_attempts").fetchone()
    assert json.loads(attempt["answers_json"])["q1"] == "a"


def test_visibility_skip_is_jackside_only(tmp_path: Path) -> None:
    db_path = tmp_path / "anticheat.sqlite3"
    _, token_hash = _seed_attempt(db_path, campaign_type="standard")

    with transaction(db_path) as conn:
        with pytest.raises(ValueError, match="anti_cheat_not_enabled"):
            apply_visibility_skip(
                conn,
                token_hash=token_hash,
                question_id="q1",
                hidden_ms=2_000,
            )


def test_last_question_requests_canonical_finish(tmp_path: Path) -> None:
    db_path = tmp_path / "anticheat.sqlite3"
    _, token_hash = _seed_attempt(
        db_path,
        answers={"q1": "a"},
        current_index=1,
    )

    with transaction(db_path) as conn:
        result = apply_visibility_skip(
            conn,
            token_hash=token_hash,
            question_id="q2",
            hidden_ms=1_001,
        )
    assert result["skipped"] is True
    assert result["last_question"] is True
    assert result["finish_required"] is True

    with connect(db_path) as conn:
        attempt = conn.execute("SELECT answers_json FROM quiz_attempts").fetchone()
    assert json.loads(attempt["answers_json"])["q2"] == ANTI_CHEAT_SKIP_SENTINEL


def test_http_skip_endpoint_advances_daily_attempt(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "http-anticheat.sqlite3",
        secret_key="http-anticheat-secret-key-long-enough-for-tests",
        admin_pin="2468",
        secure_cookie=False,
    )
    client = TestClient(create_app(settings))
    token = "t" * 43
    _, token_hash = _seed_attempt(
        settings.db_path,
        secret_key=settings.secret_key,
        token=token,
    )

    response = client.post(
        "/api/quiz/anti-cheat/skip",
        json={
            "attempt_token": token,
            "question_id": "q1",
            "hidden_ms": 1_100,
        },
    )
    assert response.status_code == 200
    assert response.json()["skipped"] is True
    assert response.json()["current_index"] == 1

    with connect(settings.db_path) as conn:
        attempt = conn.execute(
            "SELECT answers_json,current_index FROM quiz_attempts WHERE token_hash=?",
            (token_hash,),
        ).fetchone()
    assert json.loads(attempt["answers_json"])["q1"] == ANTI_CHEAT_SKIP_SENTINEL
    assert int(attempt["current_index"]) == 1


def test_browser_guard_uses_real_elapsed_one_second_and_keepalive() -> None:
    source = (ROOT / "app/static/js/jackside-anticheat.js").read_text(encoding="utf-8")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")

    assert "app.dataset.campaignType !== 'daily_414'" in source
    assert "const ANTI_CHEAT_DEBOUNCE_MS = 1000" in source
    assert "document.addEventListener('visibilitychange'" in source
    assert "Date.now() - Number(pending.hiddenAt" in source
    assert "sessionStorage.setItem(pendingKey" in source
    assert "'/api/quiz/anti-cheat/skip'" in source
    assert "keepalive" in source
    assert "Вы покинули страницу более чем на 1 секунду" in source
    assert "install_jackside_anticheat(application)" in main
