from pathlib import Path


MEMBER_TESTS = Path("tests/test_member_accounts.py")
QUIZ_TESTS = Path("tests/test_quiz_v2.py")

MEMBER_MARKER = "\n\n\ndef test_daily_414_start_ignores_shared_ip_classic_attempt_volume("
CLASSIC_MARKER = "\n\n\ndef test_classic_quiz_keeps_shared_ip_hourly_attempt_limit("

DAILY_TEST = '''


def test_daily_414_start_ignores_shared_ip_attempt_volume(
    tmp_path: Path,
) -> None:
    from app.services.quiz import ip_fingerprint

    client, settings = make_member_client(tmp_path)
    with client:
        client_id = seed_daily_member(client, settings)
        seed_daily_campaign(settings)
        shared_ip_hash = ip_fingerprint(settings.secret_key, "testclient")
        with transaction(settings.db_path) as conn:
            for index in range(10):
                conn.execute(
                    """
                    INSERT INTO quiz_attempts(
                        campaign_code, client_id, token_hash,
                        questions_snapshot_json, status, ip_hash
                    ) VALUES ('default', ?, ?, '[]', 'submitted', ?)
                    """,
                    (client_id, f"shared-ip-daily-{index}", shared_ip_hash),
                )

        jackside = client.post(
            "/api/quiz/start",
            json={"campaign": "daily_test"},
        )
        assert jackside.status_code == 200
        assert jackside.json()["campaign_type"] == "daily_414"
'''

CLASSIC_TEST = '''


def test_classic_quiz_keeps_shared_ip_hourly_attempt_limit(tmp_path):
    from app.services.quiz import ip_fingerprint

    client, settings = make_client(tmp_path)
    with client:
        shared_ip_hash = ip_fingerprint(settings.secret_key, "testclient")
        with transaction(settings.db_path) as conn:
            for index in range(10):
                conn.execute(
                    """
                    INSERT INTO quiz_attempts(
                        campaign_code, token_hash, questions_snapshot_json,
                        status, ip_hash
                    ) VALUES ('default', ?, '[]', 'submitted', ?)
                    """,
                    (f"shared-ip-classic-{index}", shared_ip_hash),
                )

        blocked = client.post(
            "/api/quiz/start",
            json={"campaign": "default", "phone": "9011999999"},
        )
        assert blocked.status_code == 429
        assert blocked.json()["error"] == "Слишком много попыток. Попробуйте позже"
'''


def main() -> None:
    member_text = MEMBER_TESTS.read_text(encoding="utf-8")
    member_index = member_text.find(MEMBER_MARKER)
    if member_index < 0:
        raise SystemExit("old member regression tests were not found")
    member_text = member_text[:member_index].rstrip() + DAILY_TEST
    MEMBER_TESTS.write_text(member_text, encoding="utf-8")

    quiz_text = QUIZ_TESTS.read_text(encoding="utf-8")
    if CLASSIC_MARKER in quiz_text:
        raise SystemExit("classic shared-IP regression test already exists")
    QUIZ_TESTS.write_text(quiz_text.rstrip() + CLASSIC_TEST, encoding="utf-8")


if __name__ == "__main__":
    main()
