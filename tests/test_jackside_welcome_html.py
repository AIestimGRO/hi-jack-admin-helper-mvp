"""HTML welcome copy for JACKSIDE daily_414 (no browser)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import init_db, transaction
from app.main import create_app
from app.services.daily_414 import DAILY_414_TIME_LIMIT_SECONDS
from app.services.member_accounts import MEMBER_COOKIE_NAME, hash_password, issue_session


def test_daily_414_welcome_points_hide_classic_meta(tmp_path: Path) -> None:
    settings = Settings(
        admin_pin="2468",
        admin_name="Test Admin",
        secret_key="welcome-test-secret-key-that-is-longer-than-32-characters",
        db_path=tmp_path / "welcome.sqlite3",
        secure_cookie=False,
        member_portal_enabled=True,
    )
    init_db(settings.db_path)
    app = create_app(settings)
    local_now = datetime.now(ZoneInfo(settings.timezone_name)).replace(tzinfo=None)
    active_from = (local_now - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S")
    with transaction(settings.db_path) as conn:
        conn.execute(
            """
            INSERT INTO quiz_campaigns(
                code, title, campaign_type, quiz_time_limit_seconds,
                max_attempts, verification_required, current_version, is_active,
                active_from
            ) VALUES ('welcome_daily', 'Welcome Daily', 'daily_414', ?, 1, 1, 1, 1, ?)
            """,
            (DAILY_414_TIME_LIMIT_SECONDS, active_from),
        )
        for index in range(1, 11):
            question_id = int(
                conn.execute(
                    """
                    INSERT INTO quiz_questions(
                        campaign_code, code, type, title, required, points,
                        position, is_active, game_round
                    ) VALUES ('welcome_daily', ?, 'single_choice', ?, 1, 1, ?, 1, 'main')
                    """,
                    (f"m{index}", f"Q {index}", index * 10),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO quiz_options(
                    question_id, code, text, is_correct, position
                ) VALUES (?, ?, 'Верно', 1, 10), (?, ?, 'Неверно', 0, 20)
                """,
                (question_id, f"m{index}_yes", question_id, f"m{index}_no"),
            )
        client_id = int(
            conn.execute(
                """
                INSERT INTO clients(
                    first_name, phone_raw, phone_full, phone_local, source
                ) VALUES ('Welcome', '+7 900 111-22-33', '79001112233',
                          '9001112233', 'test')
                """
            ).lastrowid
        )
        account_id = int(
            conn.execute(
                """
                INSERT INTO member_accounts(
                    client_id, email, email_normalized, password_hash,
                    email_verified_at
                ) VALUES (?, 'welcome@test.local', 'welcome@test.local', ?,
                          CURRENT_TIMESTAMP)
                """,
                (client_id, hash_password("abcdef")),
            ).lastrowid
        )
        for document in conn.execute(
            "SELECT * FROM legal_documents WHERE is_active=1"
        ).fetchall():
            conn.execute(
                """
                INSERT INTO member_consents(
                    account_id, document_id, document_code, document_version,
                    ip_hash, user_agent
                ) VALUES (?, ?, ?, ?, 'test-ip', 'pytest')
                """,
                (account_id, document["id"], document["code"], document["version"]),
            )
        token = issue_session(
            conn,
            secret_key=settings.secret_key,
            account_id=account_id,
            session_version=1,
            days=1,
            ip_hash="test-ip",
            user_agent="pytest",
        )

    client = TestClient(app)
    with client:
        client.cookies.set(MEMBER_COOKIE_NAME, token)
        page = client.get("/quiz?campaign=welcome_daily")
        assert page.status_code == 200
        assert "Данный квиз прошли" in page.text
        assert 'class="jackside-welcome-points"' in page.text
        assert 'class="quiz-welcome-meta" hidden' in page.text
        assert 'data-content="welcome-text" hidden' in page.text
