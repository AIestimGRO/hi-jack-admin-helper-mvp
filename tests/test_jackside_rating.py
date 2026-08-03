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


def test_jackside_rating_uses_accuracy_then_volume_and_games(tmp_path) -> None:
    db_path = tmp_path / "jackside-rating.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        player_90 = int(
            conn.execute(
                "INSERT INTO clients(first_name, source) VALUES ('Девяносто', 'test')"
            ).lastrowid
        )
        player_one = int(
            conn.execute(
                "INSERT INTO clients(first_name, source) VALUES ('Один ответ', 'test')"
            ).lastrowid
        )
        player_many = int(
            conn.execute(
                "INSERT INTO clients(first_name, source) VALUES ('Много ответов', 'test')"
            ).lastrowid
        )
        player_games = int(
            conn.execute(
                "INSERT INTO clients(first_name, source) VALUES ('Больше игр', 'test')"
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO clients(first_name, source) VALUES ('Без квизов', 'test')"
        )
        _submission(conn, player_90, 9, 10, "2026-08-01 10:00:00")
        _submission(conn, player_one, 1, 1, "2026-08-01 11:00:00")
        _submission(conn, player_many, 10, 10, "2026-08-01 12:00:00")
        _submission(conn, player_games, 5, 5, "2026-08-01 13:00:00")
        _submission(conn, player_games, 5, 5, "2026-08-02 13:00:00")

        rating = jackside_leaderboard(conn)

    assert [row["client_id"] for row in rating] == [
        player_games,
        player_many,
        player_one,
        player_90,
    ]
    assert [row["place"] for row in rating] == [1, 2, 3, 4]
    assert rating[0]["accuracy"] == 100
    assert rating[-1]["accuracy"] == 90
