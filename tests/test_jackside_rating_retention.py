from app.db import init_db, transaction
from app.services.quiz_retention import cleanup_quiz_data


def _client(conn, name: str) -> int:
    return int(
        conn.execute(
            "INSERT INTO clients(first_name,source) VALUES (?,'test')",
            (name,),
        ).lastrowid
    )


def _campaign(conn, code: str, campaign_type: str) -> None:
    conn.execute(
        "INSERT INTO quiz_campaigns(code,title,campaign_type) VALUES (?,?,?)",
        (code, code, campaign_type),
    )


def _submission(conn, *, client_id: int, code: str, created_at: str) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO quiz_submissions(
                campaign_code,campaign_version,client_id,phone_raw,phone_local,
                answers_json,max_score,correct_count,max_correct_count,passed,
                main_round_completed,created_at,ip_hash
            ) VALUES (?,1,?,'','9990000000','{}',10,8,10,1,1,?,'test')
            """,
            (code, client_id, created_at),
        ).lastrowid
    )


def test_generic_cleanup_preserves_old_jackside_submissions(tmp_path) -> None:
    db_path = tmp_path / "retention.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        player = _client(conn, "Player")
        _campaign(conn, "jackside_old", "daily_414")
        _campaign(conn, "classic_old", "classic")
        jackside_id = _submission(
            conn,
            client_id=player,
            code="jackside_old",
            created_at="2020-01-01T12:00:00+00:00",
        )
        classic_id = _submission(
            conn,
            client_id=player,
            code="classic_old",
            created_at="2020-01-01T12:00:00+00:00",
        )

        result = cleanup_quiz_data(
            conn,
            detail_days=7,
            reward_days=14,
            action_log_days=31,
            force=True,
        )

        assert result["submissions"] == 1
        assert conn.execute(
            "SELECT 1 FROM quiz_submissions WHERE id=?",
            (jackside_id,),
        ).fetchone()
        assert not conn.execute(
            "SELECT 1 FROM quiz_submissions WHERE id=?",
            (classic_id,),
        ).fetchone()


def test_cleanup_preserves_jackside_finalist_fk_chain(tmp_path) -> None:
    db_path = tmp_path / "retention-final.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        player = _client(conn, "Finalist")
        _campaign(conn, "jackside_final_old", "daily_414")
        submission_id = _submission(
            conn,
            client_id=player,
            code="jackside_final_old",
            created_at="2020-01-01T12:00:00+00:00",
        )
        final_table_id = int(
            conn.execute(
                """
                INSERT INTO daily_414_final_tables(
                    campaign_code,campaign_version,starts_at,questions_snapshot_json
                ) VALUES ('jackside_final_old',1,'2020-01-01T12:05:00+00:00','[]')
                """
            ).lastrowid
        )
        finalist_id = int(
            conn.execute(
                """
                INSERT INTO daily_414_finalists(
                    final_table_id,submission_id,client_id,seed
                ) VALUES (?,?,?,1)
                """,
                (final_table_id, submission_id, player),
            ).lastrowid
        )

        cleanup_quiz_data(
            conn,
            detail_days=7,
            reward_days=14,
            action_log_days=31,
            force=True,
        )

        assert conn.execute(
            "SELECT 1 FROM quiz_submissions WHERE id=?",
            (submission_id,),
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM daily_414_finalists WHERE id=?",
            (finalist_id,),
        ).fetchone()
