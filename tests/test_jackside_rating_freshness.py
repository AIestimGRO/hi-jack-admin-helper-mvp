from app.db import init_db, transaction
from app.jackside_rating_freshness import get_current_jackside_snapshot
from app.services.jackside_analytics import refresh_jackside_analytics


def test_rating_snapshot_refreshes_immediately_when_source_version_changes(tmp_path) -> None:
    db_path = tmp_path / "freshness.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        first = refresh_jackside_analytics(conn)
        first_generated = first["generated_at"]
        client_id = int(
            conn.execute(
                "INSERT INTO clients(first_name,source) VALUES ('Player','test')"
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO quiz_campaigns(code,title,campaign_type)
            VALUES ('jackside_fresh','Fresh','daily_414')
            """
        )
        conn.execute(
            """
            INSERT INTO quiz_submissions(
                campaign_code,campaign_version,client_id,phone_raw,phone_local,
                answers_json,max_score,correct_count,max_correct_count,passed,
                main_round_completed,created_at,ip_hash
            ) VALUES ('jackside_fresh',1,?,'','9990000000','{}',10,8,10,1,1,
                      CURRENT_TIMESTAMP,'test')
            """,
            (client_id,),
        )

        calls = []

        def refresh(conn, **kwargs):
            calls.append(1)
            return refresh_jackside_analytics(conn, **kwargs)

        current = get_current_jackside_snapshot(conn, refresh=refresh)

        assert calls == [1]
        assert current["source_version"] > first["source_version"]
        assert current["generated_at"] >= first_generated
        assert any(int(row["client_id"]) == client_id for row in current["all_time"])
