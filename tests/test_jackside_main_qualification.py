from datetime import date, datetime, timedelta

from app.db import init_db, transaction
from app.services.daily_414 import (
    award_daily_jackcoin,
    daily_main_round_completed,
    final_table_candidate_eligible,
)


def _questions(count: int = 10) -> list[dict]:
    return [
        {
            "id": f"q{index}",
            "type": "single_choice",
            "points": 1,
            "options": [
                {"id": "yes", "correct": True},
                {"id": "no", "correct": False},
            ],
        }
        for index in range(1, count + 1)
    ]


def _answers(count: int = 10, *, wrong: set[int] | None = None) -> dict[str, str]:
    wrong = wrong or set()
    return {
        f"q{index}": "no" if index in wrong else "yes"
        for index in range(1, count + 1)
    }


def _campaign(code: str) -> dict[str, str]:
    return {
        "code": code,
        "active_from": "2026-08-21T18:14:00",
    }


def test_imperfect_completed_jackside_round_is_not_final_eligible() -> None:
    start = datetime(2026, 8, 21, 18, 14)
    questions = _questions()
    answers = _answers(wrong={4})

    completed = daily_main_round_completed(
        timed_out=False,
        questions=questions,
        answers=answers,
    )
    eligible = final_table_candidate_eligible(
        _campaign("jackside_20260821_test"),
        started_at=start,
        finished_at=start + timedelta(minutes=3),
        timed_out=False,
        main_round_completed=completed,
    )

    assert completed is True
    assert eligible is False


def test_perfect_completed_jackside_round_is_final_eligible() -> None:
    start = datetime(2026, 8, 21, 18, 14)
    questions = _questions()
    answers = _answers()

    completed = daily_main_round_completed(
        timed_out=False,
        questions=questions,
        answers=answers,
    )
    eligible = final_table_candidate_eligible(
        _campaign("jackside_20260821_test"),
        started_at=start,
        finished_at=start + timedelta(minutes=3),
        timed_out=False,
        main_round_completed=completed,
    )

    assert completed is True
    assert eligible is True


def test_legacy_daily_final_eligibility_is_unchanged() -> None:
    start = datetime(2026, 8, 21, 18, 14)
    questions = _questions()
    answers = _answers(wrong={2})

    completed = daily_main_round_completed(
        timed_out=False,
        questions=questions,
        answers=answers,
    )
    eligible = final_table_candidate_eligible(
        _campaign("daily_legacy"),
        started_at=start,
        finished_at=start + timedelta(minutes=3),
        timed_out=False,
        main_round_completed=completed,
    )

    assert completed is True
    assert eligible is True


def test_imperfect_completed_round_keeps_earned_jackcoin(tmp_path) -> None:
    db_path = tmp_path / "jackside-earned-jc.sqlite3"
    init_db(db_path)

    with transaction(db_path) as conn:
        client_id = int(
            conn.execute(
                "INSERT INTO clients(first_name, source) VALUES ('Test', 'test')"
            ).lastrowid
        )
        award = award_daily_jackcoin(
            conn,
            client_id=client_id,
            submission_id=9001,
            issue_day=date(2026, 8, 21),
            correct_count=6,
            max_correct_count=10,
        )

    assert award["answers"] == 30
    assert award["completion"] == 10
    assert award["perfect"] == 0
    assert award["total"] == 40
