import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from app.db import connect, init_db, transaction
from app.services.jackside_issues import create_issue, register_issue_participant


def test_shared_clock_sets_absolute_attempt_deadline_and_global_completion(tmp_path) -> None:
    db_path = tmp_path / "shared-clock.sqlite3"
    init_db(db_path)
    starts = datetime(2026, 8, 10, 15, 14, tzinfo=timezone.utc)

    with transaction(db_path) as conn:
        issue = create_issue(
            conn,
            issue_date_value=date(2026, 8, 10),
            starts_at=starts,
        )
        client_id = int(
            conn.execute(
                "INSERT INTO clients(first_name, source) VALUES ('Late player', 'test')"
            ).lastrowid
        )
        register_issue_participant(
            conn,
            issue_id=int(issue["id"]),
            client_id=client_id,
            account_id=None,
        )

        attempt_id = int(
            conn.execute(
                """
                INSERT INTO quiz_attempts(
                    campaign_code, campaign_version, client_id, token_hash,
                    questions_snapshot_json, question_started_at,
                    attempt_deadline_at, ip_hash
                ) VALUES (?, 1, ?, 'shared-clock-token', '[]', ?, ?, 'ip')
                """,
                (
                    issue["campaign_code"],
                    client_id,
                    "2026-08-10T15:15:00.000+00:00",
                    "2026-08-10T15:19:14.000+00:00",
                ),
            ).lastrowid
        )
        attempt = conn.execute(
            "SELECT attempt_deadline_at FROM quiz_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        assert attempt["attempt_deadline_at"] == "2026-08-10T15:18:14.000+00:00"

        submission_id = int(
            conn.execute(
                """
                INSERT INTO quiz_submissions(
                    attempt_id, campaign_code, campaign_version, client_id,
                    phone_raw, phone_local, answers_json, correct_count,
                    max_correct_count, completion_time_ms,
                    main_prize_eligible, ip_hash
                ) VALUES (?, ?, 1, ?, '1', '1', '{}', 10, 10, 120000, 1, 'ip')
                """,
                (attempt_id, issue["campaign_code"], client_id),
            ).lastrowid
        )
        submission = conn.execute(
            "SELECT completion_time_ms FROM quiz_submissions WHERE id=?",
            (submission_id,),
        ).fetchone()

    # Player joined one minute late and solved in two minutes: official time is
    # three minutes from the shared issue start.
    assert abs(int(submission["completion_time_ms"]) - 180000) <= 2


def test_shared_clock_rules_migration_is_safe_and_idempotent(tmp_path) -> None:
    db_path = tmp_path / "shared-clock-rules.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        issue = create_issue(
            conn,
            issue_date_value=date(2026, 8, 11),
            starts_at=datetime(2026, 8, 11, 15, 14, tzinfo=timezone.utc),
        )
        assert issue["rules_version"] == "1.0"

    script = Path(__file__).resolve().parents[1] / "scripts" / "migrate_jackside_rules_shared_clock.py"
    first = subprocess.run(
        [sys.executable, str(script), str(db_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0, first.stderr or first.stdout

    with connect(db_path) as conn:
        active = conn.execute(
            "SELECT * FROM jackside_rules_versions WHERE is_active=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        migrated_issue = conn.execute(
            "SELECT * FROM jackside_issues WHERE id=?", (issue["id"],)
        ).fetchone()
        assert active["version"] == "1.1"
        assert "общий таймер" in active["content"]
        assert "первым правильно ответил" in active["content"]
        assert migrated_issue["rules_version"] == "1.1"

    second = subprocess.run(
        [sys.executable, str(script), str(db_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert second.returncode == 0, second.stderr or second.stdout
    assert "already active" in second.stdout
