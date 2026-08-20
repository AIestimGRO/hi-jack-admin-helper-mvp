from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.db import init_db, transaction
from app.jackside_critical_hotfix import (
    reconcile_expired_jackside_final,
    refresh_jackside_issue_question_counts,
    rewrite_jackside_quiz_html,
)
from app.services.daily_414_final import ensure_final_table, reconcile_final_table
from app.services.jackside_issues import copy_issue, create_issue


ROOT = Path(__file__).resolve().parents[1]


def _seed_candidate(conn, *, campaign_code: str, number: int = 1) -> tuple[int, int]:
    client_id = int(
        conn.execute(
            "INSERT INTO clients(first_name, source) VALUES (?, 'test')",
            (f"Player {number}",),
        ).lastrowid
    )
    submission_id = int(
        conn.execute(
            """
            INSERT INTO quiz_submissions(
                campaign_code, campaign_version, client_id, phone_raw,
                phone_local, answers_json, correct_count, max_correct_count,
                completion_time_ms, main_prize_eligible, main_round_completed,
                ip_hash
            ) VALUES (?, 1, ?, ?, ?, '{}', 1, 1, 1000, 1, 1, ?)
            """,
            (
                campaign_code,
                client_id,
                str(number),
                str(number),
                f"ip-{number}",
            ),
        ).lastrowid
    )
    return client_id, submission_id


def test_issue_source_counts_refresh_from_actual_questions(tmp_path) -> None:
    db_path = tmp_path / "source-counts.sqlite3"
    init_db(db_path)
    starts = datetime(2026, 8, 19, 15, 14, tzinfo=timezone.utc)
    with transaction(db_path) as conn:
        issue = create_issue(
            conn,
            issue_date_value=date(2026, 8, 19),
            starts_at=starts,
        )
        code = str(issue["campaign_code"])
        conn.execute(
            """
            INSERT INTO quiz_questions(
                campaign_code, code, type, title, game_round, position, is_active
            ) VALUES
                (?, 'm1', 'text', 'Main', 'main', 10, 1),
                (?, 'f1', 'text', 'Final', 'final', 20, 1)
            """,
            (code, code),
        )
        stale = conn.execute(
            "SELECT main_question_count,final_question_count FROM jackside_issues WHERE id=?",
            (issue["id"],),
        ).fetchone()
        assert (stale["main_question_count"], stale["final_question_count"]) == (0, 0)

        changed = refresh_jackside_issue_question_counts(conn)
        refreshed = conn.execute(
            "SELECT main_question_count,final_question_count FROM jackside_issues WHERE id=?",
            (issue["id"],),
        ).fetchone()

    assert changed == 1
    assert (refreshed["main_question_count"], refreshed["final_question_count"]) == (1, 1)


def test_copy_draft_issue_preserves_sections_images_options_and_rounds(tmp_path) -> None:
    db_path = tmp_path / "draft-copy.sqlite3"
    init_db(db_path)
    starts = datetime(2026, 8, 19, 15, 14, tzinfo=timezone.utc)
    with transaction(db_path) as conn:
        source = create_issue(
            conn,
            issue_date_value=date(2026, 8, 19),
            starts_at=starts,
        )
        source_code = str(source["campaign_code"])
        section_id = int(
            conn.execute(
                """
                INSERT INTO quiz_sections(
                    campaign_code,title,theme,background_image,position
                ) VALUES (?, 'Finalka', 'photo', '/quiz-media/final-bg.webp', 10)
                """,
                (source_code,),
            ).lastrowid
        )
        main_id = int(
            conn.execute(
                """
                INSERT INTO quiz_questions(
                    campaign_code,code,type,title,visual_type,image_path,
                    section_id,game_round,position,is_active
                ) VALUES (?, 'm1', 'single_choice', 'Main', 'photo',
                          '/quiz-media/main.webp', NULL, 'main', 10, 1)
                """,
                (source_code,),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO quiz_options(question_id,code,text,is_correct,position)
            VALUES (?, 'yes', 'Yes', 1, 10), (?, 'no', 'No', 0, 20)
            """,
            (main_id, main_id),
        )
        conn.execute(
            """
            INSERT INTO quiz_questions(
                campaign_code,code,type,title,visual_type,image_path,
                section_id,game_round,position,is_active
            ) VALUES (?, 'f1', 'text', 'Final', 'photo',
                      '/quiz-media/final-question.webp', ?, 'final', 20, 1)
            """,
            (source_code, section_id),
        )

        assert int(source["main_question_count"] or 0) == 0
        assert int(source["final_question_count"] or 0) == 0
        copied = copy_issue(
            conn,
            source_issue_id=int(source["id"]),
            issue_date_value=date(2026, 8, 20),
            starts_at=starts + timedelta(days=1),
        )
        copied_code = str(copied["campaign_code"])
        questions = conn.execute(
            """
            SELECT qq.*,qs.background_image AS section_background
            FROM quiz_questions qq
            LEFT JOIN quiz_sections qs ON qs.id=qq.section_id
            WHERE qq.campaign_code=? ORDER BY qq.position,qq.id
            """,
            (copied_code,),
        ).fetchall()
        option_count = conn.execute(
            """
            SELECT COUNT(*) FROM quiz_options qo
            JOIN quiz_questions qq ON qq.id=qo.question_id
            WHERE qq.campaign_code=?
            """,
            (copied_code,),
        ).fetchone()[0]

    assert copied["status"] == "draft"
    assert copied["main_question_count"] == 1
    assert copied["final_question_count"] == 1
    assert [row["game_round"] for row in questions] == ["main", "final"]
    assert questions[0]["image_path"] == "/quiz-media/main.webp"
    assert questions[1]["image_path"] == "/quiz-media/final-question.webp"
    assert questions[1]["section_background"] == "/quiz-media/final-bg.webp"
    assert option_count == 2


@pytest.mark.parametrize(
    ("answer_correct", "expected_outcome", "expected_finalist_status"),
    [
        (True, "single_winner", "winner"),
        (False, "no_winner", "eliminated"),
        (None, "no_winner", "eliminated"),
    ],
)
def test_solo_jackside_final_always_resolves_after_deadline(
    tmp_path,
    answer_correct: bool | None,
    expected_outcome: str,
    expected_finalist_status: str,
) -> None:
    db_path = tmp_path / f"solo-{answer_correct}.sqlite3"
    init_db(db_path)
    start = datetime(2026, 8, 19, 18, 23, 14, tzinfo=timezone.utc)
    campaign_code = "jackside_20260819"
    with transaction(db_path) as conn:
        _seed_candidate(conn, campaign_code=campaign_code)
        table = ensure_final_table(
            conn,
            campaign_code=campaign_code,
            campaign_version=1,
            starts_at=start,
            questions=[{"id": "f1"}],
            question_time_seconds=30,
        )
        live = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start,
        )
        assert live["status"] == "live"
        finalist = conn.execute(
            "SELECT * FROM daily_414_finalists WHERE final_table_id=?",
            (table["id"],),
        ).fetchone()
        assert finalist is not None
        if answer_correct is not None:
            conn.execute(
                """
                INSERT INTO daily_414_final_answers(
                    final_table_id,finalist_id,question_index,question_code,
                    answer_json,is_correct,response_time_ms,answered_at
                ) VALUES (?, ?, 0, 'f1', '"answer"', ?, 500, ?)
                """,
                (
                    table["id"],
                    finalist["id"],
                    int(answer_correct),
                    (start + timedelta(seconds=1)).isoformat(),
                ),
            )
        completed = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start + timedelta(seconds=30),
        )
        final_status = conn.execute(
            "SELECT status FROM daily_414_finalists WHERE id=?",
            (finalist["id"],),
        ).fetchone()[0]

    assert completed["status"] == "completed"
    assert completed["outcome"] == expected_outcome
    assert final_status == expected_finalist_status


def test_expired_multiplayer_final_reconciles_without_active_campaign_lookup(tmp_path) -> None:
    db_path = tmp_path / "expired-multi.sqlite3"
    init_db(db_path)
    start = datetime(2026, 8, 20, 8, 0, 14, tzinfo=timezone.utc)
    campaign_code = "jackside_20260820_1055"
    with transaction(db_path) as conn:
        for number in range(1, 5):
            _seed_candidate(conn, campaign_code=campaign_code, number=number)
        table = ensure_final_table(
            conn,
            campaign_code=campaign_code,
            campaign_version=1,
            starts_at=start,
            questions=[{"id": "f1", "time_limit_seconds": 44}],
            question_time_seconds=44,
            prize_type="jackcoin",
            prize_jackcoin_amount=500,
        )
        live = reconcile_final_table(
            conn,
            final_table_id=int(table["id"]),
            now=start,
        )
        assert live["status"] == "live"
        finalists = conn.execute(
            "SELECT * FROM daily_414_finalists WHERE final_table_id=? ORDER BY seed",
            (table["id"],),
        ).fetchall()
        for index, finalist in enumerate(finalists[:2], start=1):
            conn.execute(
                """
                INSERT INTO daily_414_final_answers(
                    final_table_id,finalist_id,question_index,question_code,
                    answer_json,is_correct,response_time_ms,answered_at
                ) VALUES (?, ?, 0, 'f1', '"wrong"', 0, ?, ?)
                """,
                (
                    table["id"],
                    finalist["id"],
                    20000 + index * 1000,
                    (start + timedelta(seconds=20 + index)).isoformat(),
                ),
            )
        changed = reconcile_expired_jackside_final(
            conn,
            campaign_code=campaign_code,
            now=start + timedelta(seconds=45),
        )
        completed = conn.execute(
            "SELECT * FROM daily_414_final_tables WHERE id=?",
            (table["id"],),
        ).fetchone()
        statuses = [
            row[0]
            for row in conn.execute(
                "SELECT status FROM daily_414_finalists WHERE final_table_id=? ORDER BY seed",
                (table["id"],),
            ).fetchall()
        ]

    assert changed is True
    assert completed["status"] == "completed"
    assert completed["outcome"] == "no_winner"
    assert statuses == ["eliminated", "eliminated", "eliminated", "eliminated"]


def test_jackside_section_background_is_not_promoted_to_campaign_background() -> None:
    source = (
        '<html><body><main id="quiz-app" data-campaign-type="daily_414" '
        'data-campaign-background="/quiz-media/final.jpg"></main></body></html>'
    )
    rewritten = rewrite_jackside_quiz_html(source)
    assert 'data-campaign-background=""' in rewritten
    assert "/static/js/jackside-critical-hotfix.js" in rewritten
    assert "/quiz-media/final.jpg" not in rewritten


def test_classic_quiz_html_is_not_rewritten() -> None:
    source = (
        '<html><body><main id="quiz-app" data-campaign-type="classic" '
        'data-campaign-background="/quiz-media/classic.jpg"></main></body></html>'
    )
    assert rewrite_jackside_quiz_html(source) == source


def test_final_watchdog_tracks_server_deadline_and_section_background() -> None:
    source = (ROOT / "app/static/js/jackside-critical-hotfix.js").read_text(
        encoding="utf-8"
    )
    assert "/api/quiz/final-table/status" in source
    assert "question_deadline_at" in source
    assert "question?.section?.background_image" in source
    assert "visibilitychange" in source
    assert "response.status === 404" in source
    assert "Подводим итог" in source
