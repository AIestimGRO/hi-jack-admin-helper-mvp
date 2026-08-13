from datetime import date, datetime, timezone

from app.db import init_db, transaction
from app.legacy_jackside_copy import (
    copy_legacy_campaign_to_issue,
    list_legacy_daily_campaigns,
)


def _seed_legacy_daily(conn, *, code: str = "friday_legacy") -> int:
    campaign_id = int(
        conn.execute(
            """
            INSERT INTO quiz_campaigns(
                code, title, campaign_type, is_active, active_from, active_until,
                final_question_time_seconds, final_prize_type,
                final_prize_jackcoin_amount
            ) VALUES (?, 'Пятница', 'daily_414', 0,
                      '2026-08-07T18:14:00+03:00',
                      '2026-08-07T23:59:59+03:00',
                      25, 'jackcoin', 777)
            """,
            (code,),
        ).lastrowid
    )
    section_id = int(
        conn.execute(
            """
            INSERT INTO quiz_sections(campaign_code, title, theme, position)
            VALUES (?, 'Фото-раунд', 'photo', 10)
            """,
            (code,),
        ).lastrowid
    )
    main_id = int(
        conn.execute(
            """
            INSERT INTO quiz_questions(
                campaign_code, code, type, title, visual_type, image_path,
                section_id, game_round, required, points, position, is_active
            ) VALUES (?, 'q1', 'single_choice', 'Первый вопрос', 'photo',
                      '/quiz-media/friday/q1.jpg', ?, 'main', 1, 1, 10, 1)
            """,
            (code, section_id),
        ).lastrowid
    )
    conn.executemany(
        """
        INSERT INTO quiz_options(question_id, code, text, is_correct, position)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(main_id, "a", "Неверно", 0, 10), (main_id, "b", "Верно", 1, 20)],
    )
    final_id = int(
        conn.execute(
            """
            INSERT INTO quiz_questions(
                campaign_code, code, type, title, game_round,
                required, points, time_limit_seconds, position, is_active
            ) VALUES (?, 'final1', 'single_choice', 'Финальный вопрос', 'final',
                      1, 1, 25, 100, 1)
            """,
            (code,),
        ).lastrowid
    )
    conn.executemany(
        """
        INSERT INTO quiz_options(question_id, code, text, is_correct, position)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(final_id, "a", "Мимо", 0, 10), (final_id, "b", "Точно", 1, 20)],
    )
    return campaign_id


def test_legacy_daily_campaign_is_offered_as_copy_source(tmp_path) -> None:
    db_path = tmp_path / "legacy-source.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        campaign_id = _seed_legacy_daily(conn)
        conn.execute(
            "INSERT INTO quiz_campaigns(code, title, campaign_type) VALUES ('classic_old', 'Обычный квиз', 'classic')"
        )
        rows = list_legacy_daily_campaigns(conn)

    assert [int(row["id"]) for row in rows] == [campaign_id]
    assert rows[0]["title"] == "Пятница"
    assert int(rows[0]["main_question_count"]) == 1
    assert int(rows[0]["final_question_count"]) == 1


def test_copy_legacy_daily_creates_new_issue_without_mutating_source(tmp_path) -> None:
    db_path = tmp_path / "legacy-copy.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        campaign_id = _seed_legacy_daily(conn)
        source_before = conn.execute(
            "SELECT * FROM quiz_campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
        source_question_ids_before = [
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM quiz_questions WHERE campaign_code=? ORDER BY id",
                (source_before["code"],),
            ).fetchall()
        ]

        issue = copy_legacy_campaign_to_issue(
            conn,
            source_campaign_id=campaign_id,
            issue_date_value=date(2026, 8, 13),
            starts_at=datetime(2026, 8, 13, 15, 14, tzinfo=timezone.utc),
            admin_id=None,
        )

        source_after = conn.execute(
            "SELECT * FROM quiz_campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
        target_campaign = conn.execute(
            "SELECT * FROM quiz_campaigns WHERE code=?",
            (issue["campaign_code"],),
        ).fetchone()
        copied_questions = conn.execute(
            """
            SELECT code, title, image_path, game_round, time_limit_seconds
            FROM quiz_questions
            WHERE campaign_code=?
            ORDER BY position, id
            """,
            (issue["campaign_code"],),
        ).fetchall()
        copied_options = conn.execute(
            """
            SELECT qo.text, qo.is_correct
            FROM quiz_options qo
            JOIN quiz_questions qq ON qq.id=qo.question_id
            WHERE qq.campaign_code=?
            ORDER BY qq.position, qo.position, qo.id
            """,
            (issue["campaign_code"],),
        ).fetchall()
        source_question_ids_after = [
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM quiz_questions WHERE campaign_code=? ORDER BY id",
                (source_before["code"],),
            ).fetchall()
        ]
        remaining_sources = list_legacy_daily_campaigns(conn)

    assert issue["campaign_code"] == "jackside_20260813"
    assert int(issue["main_question_count"]) == 1
    assert int(issue["final_question_count"]) == 1
    assert target_campaign is not None
    assert target_campaign["campaign_type"] == "daily_414"
    assert target_campaign["final_prize_type"] == "none"
    assert int(target_campaign["final_prize_jackcoin_amount"]) == 0
    assert int(target_campaign["final_question_time_seconds"]) == 25
    assert [(row["code"], row["game_round"]) for row in copied_questions] == [
        ("q1", "main"),
        ("final1", "final"),
    ]
    assert copied_questions[0]["image_path"] == "/quiz-media/friday/q1.jpg"
    assert [(row["text"], int(row["is_correct"])) for row in copied_options] == [
        ("Неверно", 0),
        ("Верно", 1),
        ("Мимо", 0),
        ("Точно", 1),
    ]
    assert dict(source_after) == dict(source_before)
    assert source_question_ids_after == source_question_ids_before
    assert campaign_id in [int(row["id"]) for row in remaining_sources]
