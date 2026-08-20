from datetime import date, datetime, timezone

from app.db import connect, init_db, transaction
from app.prelaunch_economy_compat import ensure_prelaunch_economy_compat
from app.prelaunch_experience import ensure_prelaunch_schema
from app.services.jackside_issues import create_issue


MAIN_JACKCOIN = 140


def _seed_finalist(conn, *, campaign_code: str, client_id: int) -> tuple[int, int]:
    submission_id = int(
        conn.execute(
            """
            INSERT INTO quiz_submissions(
                campaign_code,campaign_version,client_id,phone_raw,phone_local,
                answers_json,max_correct_count,main_round_completed,jackcoin_awarded,ip_hash
            ) VALUES (?,1,?,'9000000000','9000000000','{}',10,1,?,'test')
            """,
            (campaign_code, client_id, MAIN_JACKCOIN),
        ).lastrowid
    )
    table_id = int(
        conn.execute(
            """
            INSERT INTO daily_414_final_tables(
                campaign_code,campaign_version,starts_at,questions_snapshot_json
            ) VALUES (?,1,'2026-08-20T15:19:14+00:00','[]')
            """,
            (campaign_code,),
        ).lastrowid
    )
    finalist_id = int(
        conn.execute(
            """
            INSERT INTO daily_414_finalists(
                final_table_id,submission_id,client_id,seed
            ) VALUES (?,?,?,1)
            """,
            (table_id, submission_id, client_id),
        ).lastrowid
    )
    return table_id, finalist_id


def _correct_answer(conn, *, table_id: int, finalist_id: int, question_index: int) -> None:
    conn.execute(
        """
        INSERT INTO daily_414_final_answers(
            final_table_id,finalist_id,question_index,question_code,
            answer_json,is_correct,response_time_ms,answered_at
        ) VALUES (?,?,?,?,?,1,1200,'2026-08-20T15:19:20+00:00')
        """,
        (
            table_id,
            finalist_id,
            question_index,
            f"final_{question_index + 1}",
            '"A"',
        ),
    )


def test_winner_gets_414_jc_total_for_the_whole_issue(tmp_path) -> None:
    db_path = tmp_path / "superfinal.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_prelaunch_schema(conn)
        ensure_prelaunch_economy_compat(conn)
        client_id = int(
            conn.execute(
                "INSERT INTO clients(first_name,source) VALUES ('Winner','test')"
            ).lastrowid
        )
        issue = create_issue(
            conn,
            issue_date_value=date(2026, 8, 20),
            starts_at=datetime(2026, 8, 20, 15, 14, tzinfo=timezone.utc),
        )
        table_id, finalist_id = _seed_finalist(
            conn, campaign_code=str(issue["campaign_code"]), client_id=client_id
        )
        _correct_answer(
            conn,
            table_id=table_id,
            finalist_id=finalist_id,
            question_index=0,
        )
        conn.execute(
            "UPDATE daily_414_finalists SET status='winner' WHERE id=?",
            (finalist_id,),
        )

    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT source_type,amount FROM jackcoin_ledger
            WHERE client_id=?
              AND source_type IN ('jackside_final_correct','jackside_final_win')
            ORDER BY id
            """,
            (client_id,),
        ).fetchall()
        assert [(row["source_type"], int(row["amount"])) for row in rows] == [
            ("jackside_final_correct", 50),
            ("jackside_final_win", 224),
        ]
        assert MAIN_JACKCOIN + sum(int(row["amount"]) for row in rows) == 414


def test_winner_adjustment_can_reduce_issue_total_to_414(tmp_path) -> None:
    db_path = tmp_path / "superfinal-many-correct.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_prelaunch_schema(conn)
        ensure_prelaunch_economy_compat(conn)
        client_id = int(
            conn.execute(
                "INSERT INTO clients(first_name,source) VALUES ('Winner','test')"
            ).lastrowid
        )
        issue = create_issue(
            conn,
            issue_date_value=date(2026, 8, 21),
            starts_at=datetime(2026, 8, 21, 15, 14, tzinfo=timezone.utc),
        )
        table_id, finalist_id = _seed_finalist(
            conn, campaign_code=str(issue["campaign_code"]), client_id=client_id
        )
        for question_index in range(6):
            _correct_answer(
                conn,
                table_id=table_id,
                finalist_id=finalist_id,
                question_index=question_index,
            )
        conn.execute(
            "UPDATE daily_414_finalists SET status='winner' WHERE id=?",
            (finalist_id,),
        )

    with connect(db_path) as conn:
        final_correct = int(
            conn.execute(
                """
                SELECT COALESCE(SUM(amount),0) FROM jackcoin_ledger
                WHERE client_id=? AND source_type='jackside_final_correct'
                """,
                (client_id,),
            ).fetchone()[0]
        )
        win_row = conn.execute(
            """
            SELECT amount,operation_type FROM jackcoin_ledger
            WHERE client_id=? AND source_type='jackside_final_win'
            """,
            (client_id,),
        ).fetchone()
        assert final_correct == 300
        assert int(win_row["amount"]) == -26
        assert win_row["operation_type"] == "adjust"
        assert MAIN_JACKCOIN + final_correct + int(win_row["amount"]) == 414


def test_legacy_daily_final_does_not_receive_launch_final_rewards(tmp_path) -> None:
    db_path = tmp_path / "legacy-final.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_prelaunch_schema(conn)
        ensure_prelaunch_economy_compat(conn)
        client_id = int(
            conn.execute(
                "INSERT INTO clients(first_name,source) VALUES ('Legacy','test')"
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO quiz_campaigns(code,title,campaign_type)
            VALUES ('daily_test','Legacy daily','daily_414')
            """
        )
        table_id, finalist_id = _seed_finalist(
            conn, campaign_code="daily_test", client_id=client_id
        )
        _correct_answer(
            conn,
            table_id=table_id,
            finalist_id=finalist_id,
            question_index=0,
        )
        conn.execute(
            "UPDATE daily_414_finalists SET status='winner' WHERE id=?",
            (finalist_id,),
        )
        assert int(
            conn.execute(
                """
                SELECT COUNT(*) FROM jackcoin_ledger
                WHERE client_id=?
                  AND source_type IN ('jackside_final_correct','jackside_final_win')
                """,
                (client_id,),
            ).fetchone()[0]
        ) == 0
