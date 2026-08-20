from pathlib import Path

from app.db import init_db, transaction
from app.jackside_final_outcome_only import _final_answer_result


ROOT = Path(__file__).resolve().parents[1]


def _seed_eliminated_finalist(conn, *, is_correct: bool) -> tuple[int, object]:
    client_id = int(
        conn.execute(
            "INSERT INTO clients(first_name,source) VALUES ('Finalist','test')"
        ).lastrowid
    )
    submission_id = int(
        conn.execute(
            """
            INSERT INTO quiz_submissions(
                campaign_code,campaign_version,client_id,phone_raw,phone_local,
                answers_json,max_correct_count,main_round_completed,ip_hash
            ) VALUES ('jackside_20260820',1,?,'9000000000','9000000000','{}',10,1,'test')
            """,
            (client_id,),
        ).lastrowid
    )
    table_id = int(
        conn.execute(
            """
            INSERT INTO daily_414_final_tables(
                campaign_code,campaign_version,starts_at,questions_snapshot_json,status,outcome
            ) VALUES (
                'jackside_20260820',1,'2026-08-20T15:19:14+00:00','[]',
                'completed','single_winner'
            )
            """
        ).lastrowid
    )
    finalist_id = int(
        conn.execute(
            """
            INSERT INTO daily_414_finalists(
                final_table_id,submission_id,client_id,seed,status,eliminated_question_index
            ) VALUES (?,?,?,1,'eliminated',0)
            """,
            (table_id, submission_id, client_id),
        ).lastrowid
    )
    answer_id = int(
        conn.execute(
            """
            INSERT INTO daily_414_final_answers(
                final_table_id,finalist_id,question_index,question_code,
                answer_json,is_correct,response_time_ms,answered_at
            ) VALUES (?,?,0,'final_1','\"A\"',?,2500,'2026-08-20T15:19:20+00:00')
            """,
            (table_id, finalist_id, int(is_correct)),
        ).lastrowid
    )
    if is_correct:
        conn.execute(
            """
            INSERT INTO jackcoin_ledger(
                client_id,amount,operation_type,source_type,source_id,idempotency_key,comment
            ) VALUES (?,50,'earn','jackside_final_correct',?,'test:correct','test')
            """,
            (client_id, str(answer_id)),
        )
    finalist = conn.execute(
        "SELECT * FROM daily_414_finalists WHERE id=?",
        (finalist_id,),
    ).fetchone()
    return table_id, finalist


def test_final_answer_result_distinguishes_correct_but_slower(tmp_path) -> None:
    db_path = tmp_path / "correct-slower.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        table_id, finalist = _seed_eliminated_finalist(conn, is_correct=True)
        answer_correct, awarded = _final_answer_result(
            conn,
            table_id=table_id,
            finalist=finalist,
        )
    assert answer_correct is True
    assert awarded == 50


def test_final_answer_result_distinguishes_wrong_answer(tmp_path) -> None:
    db_path = tmp_path / "wrong.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        table_id, finalist = _seed_eliminated_finalist(conn, is_correct=False)
        answer_correct, awarded = _final_answer_result(
            conn,
            table_id=table_id,
            finalist=finalist,
        )
    assert answer_correct is False
    assert awarded == 0


def test_existing_final_watchdog_refines_both_outcome_messages() -> None:
    source = (ROOT / "app/static/js/jackside-critical-hotfix.js").read_text(
        encoding="utf-8"
    )
    assert "/api/jackside/final-outcome" in source
    assert "correct_not_first" in source
    assert "Ответ верный, но не первым" in source
    assert "Ответ неверный" in source
