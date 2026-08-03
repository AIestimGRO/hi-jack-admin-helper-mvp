from app.db import init_db, transaction
from app.services.jackside_rating import jackside_leaderboard


def _submission(conn, client_id: int, correct: int, total: int, created_at: str) -> None:
    conn.execute(
        """
        INSERT INTO quiz_submissions(
            campaign_code, client_id, phone_raw, phone_local, answers_json,
            ip_hash, correct_count, max_correct_count, created_at
        ) VALUES ('rating', ?, '', '', '{}', ?, ?, ?, ?)
        """,
        (client_id, f"rating-{client_id}-{created_at}", correct, total, created_at),
    )


def _client(conn, name: str) -> int:
    return int(
        conn.execute(
            "INSERT INTO clients(first_name, source) VALUES (?, 'test')",
            (name,),
        ).lastrowid
    )


def test_jackside_rating_rewards_confirmed_accuracy_and_regular_play(tmp_path) -> None:
    db_path = tmp_path / "jackside-rating.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        steady_player = _client(conn, "Стабильный игрок")
        three_perfect_games = _client(conn, "Три идеальные игры")
        one_perfect_game = _client(conn, "Одна идеальная игра")
        conn.execute(
            "INSERT INTO clients(first_name, source) VALUES ('Без квизов', 'test')"
        )
        for day in range(1, 9):
            _submission(
                conn,
                steady_player,
                9,
                10,
                f"2026-08-{day:02d} 10:00:00",
            )
        for day in range(1, 4):
            _submission(
                conn,
                three_perfect_games,
                10,
                10,
                f"2026-08-{day:02d} 11:00:00",
            )
        _submission(conn, one_perfect_game, 10, 10, "2026-08-08 12:00:00")

        rating = jackside_leaderboard(conn, as_of="2026-08-09 00:00:00")

    assert [row["client_id"] for row in rating] == [
        steady_player,
        three_perfect_games,
        one_perfect_game,
    ]
    assert [row["place"] for row in rating] == [1, 2, None]
    assert rating[0]["accuracy"] == 90
    assert rating[0]["completed_count"] == 8
    assert rating[0]["active_days"] == 8
    assert rating[0]["rating_score"] > rating[1]["rating_score"]
    assert rating[1]["accuracy"] == 100
    assert rating[2]["status"] == "calibration"
    assert rating[2]["calibration_games_left"] == 2
    assert rating[2]["calibration_answers_left"] == 20


def test_jackside_rating_uses_only_thirty_days_and_keeps_calibration_visible(
    tmp_path,
) -> None:
    db_path = tmp_path / "jackside-window.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        returning_player = _client(conn, "Вернувшийся игрок")
        for day in range(1, 5):
            _submission(
                conn,
                returning_player,
                10,
                10,
                f"2026-06-{day:02d} 10:00:00",
            )
        _submission(conn, returning_player, 8, 10, "2026-08-08 10:00:00")

        rating = jackside_leaderboard(conn, as_of="2026-08-09 00:00:00")

    assert len(rating) == 1
    assert rating[0]["completed_count"] == 1
    assert rating[0]["question_total"] == 10
    assert rating[0]["accuracy"] == 80
    assert rating[0]["place"] is None


def test_jackside_rating_breaks_equal_scores_by_active_days(tmp_path) -> None:
    db_path = tmp_path / "jackside-active-days.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        regular_player = _client(conn, "Регулярный игрок")
        single_day_player = _client(conn, "Один активный день")
        for day in range(1, 4):
            _submission(
                conn,
                regular_player,
                9,
                10,
                f"2026-08-{day:02d} 10:00:00",
            )
            _submission(
                conn,
                single_day_player,
                9,
                10,
                f"2026-08-03 {10 + day}:00:00",
            )

        rating = jackside_leaderboard(conn, as_of="2026-08-09 00:00:00")

    assert [row["client_id"] for row in rating] == [
        regular_player,
        single_day_player,
    ]
    assert rating[0]["rating_score"] == rating[1]["rating_score"]
    assert rating[0]["active_days"] == 3
    assert rating[1]["active_days"] == 1
