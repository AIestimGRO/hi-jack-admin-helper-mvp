from pathlib import Path


MAIN_PATH = Path("app/main_impl.py")
TESTS_PATH = Path("tests/test_member_accounts.py")

OLD = '''                recent_attempts = conn.execute(
                    "SELECT COUNT(*) FROM quiz_attempts WHERE ip_hash=? AND created_at >= datetime('now', '-1 hour')",
                    (ip_hash,),
                ).fetchone()[0]
                if recent_attempts >= 10:
                    raise HTTPException(status_code=429, detail="Слишком много попыток. Попробуйте позже")
'''

NEW = '''                if campaign_row["campaign_type"] != "daily_414":
                    recent_attempts = conn.execute(
                        "SELECT COUNT(*) FROM quiz_attempts WHERE ip_hash=? AND created_at >= datetime('now', '-1 hour')",
                        (ip_hash,),
                    ).fetchone()[0]
                    if recent_attempts >= 10:
                        raise HTTPException(status_code=429, detail="Слишком много попыток. Попробуйте позже")
'''

TESTS = '''


def test_daily_414_start_ignores_shared_ip_classic_attempt_volume(
    tmp_path: Path,
) -> None:
    client, settings = make_member_client(tmp_path)
    with client:
        seed_daily_member(client, settings)
        seed_daily_campaign(settings)

        for index in range(10):
            started = client.post(
                "/api/quiz/start",
                json={
                    "campaign": "default",
                    "phone": f"900100{index:04d}",
                },
            )
            assert started.status_code == 200

        jackside = client.post(
            "/api/quiz/start",
            json={"campaign": "daily_test"},
        )
        assert jackside.status_code == 200
        assert jackside.json()["campaign_type"] == "daily_414"


def test_classic_quiz_keeps_shared_ip_hourly_attempt_limit(
    tmp_path: Path,
) -> None:
    client, _settings = make_member_client(tmp_path)
    with client:
        for index in range(10):
            started = client.post(
                "/api/quiz/start",
                json={
                    "campaign": "default",
                    "phone": f"901100{index:04d}",
                },
            )
            assert started.status_code == 200

        blocked = client.post(
            "/api/quiz/start",
            json={"campaign": "default", "phone": "9011999999"},
        )
        assert blocked.status_code == 429
        assert blocked.json()["error"] == "Слишком много попыток. Попробуйте позже"
'''


def main() -> None:
    main_text = MAIN_PATH.read_text(encoding="utf-8")
    count = main_text.count(OLD)
    if count != 1:
        raise SystemExit(f"expected exactly one shared-IP limiter block, found {count}")
    MAIN_PATH.write_text(main_text.replace(OLD, NEW), encoding="utf-8")

    tests_text = TESTS_PATH.read_text(encoding="utf-8")
    marker = "def test_daily_414_start_ignores_shared_ip_classic_attempt_volume("
    if marker in tests_text:
        raise SystemExit("regression test already exists")
    TESTS_PATH.write_text(tests_text + TESTS, encoding="utf-8")


if __name__ == "__main__":
    main()
