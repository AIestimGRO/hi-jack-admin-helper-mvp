"""Minimal Playwright coverage for JACKSIDE welcome copy."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Page, expect

from app.config import BASE_DIR, Settings
from app.db import connect, init_db, transaction
from app.services.auth import bootstrap_master
from app.services.daily_414 import DAILY_414_TIME_LIMIT_SECONDS
from app.services.member_accounts import MEMBER_COOKIE_NAME, hash_password, issue_session
from app.services.quiz import seed_questions_from_json

CAMPAIGN_CODE = "e2e_daily_414"
ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_http(url: str, *, timeout_s: float = 25.0) -> None:
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout_s
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:
                if response.status < 500:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"Server did not become ready: {url}") from last_error


def _seed_e2e_world(settings: Settings, *, start_delay_seconds: int = -30) -> str:
    local_now = datetime.now(ZoneInfo(settings.timezone_name)).replace(tzinfo=None)
    active_from = (local_now + timedelta(seconds=start_delay_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    password_hash = hash_password("e2epass1")
    with transaction(settings.db_path) as conn:
        bootstrap_master(
            conn,
            username=settings.master_login,
            display_name=settings.admin_name,
            pin=settings.admin_pin,
        )
        seed_questions_from_json(conn, BASE_DIR / "data" / "quiz_questions.json")
        conn.execute(
            """
            INSERT INTO quiz_campaigns(
                code, title, campaign_type, quiz_time_limit_seconds,
                max_attempts, verification_required, current_version, is_active,
                active_from, welcome_kicker, welcome_text, start_button_text,
                jackcoin_per_correct, jackcoin_completion_bonus, jackcoin_perfect_bonus,
                final_prize_type, final_prize_jackcoin_amount
            ) VALUES (
                ?, 'JACKSIDE E2E', 'daily_414', ?, 1, 1, 1, 1,
                ?, 'JACKSIDE 4:14', '10 вопросов. Одна попытка.',
                'Начать', 5, 10, 20, 'jackcoin', 500
            )
            """,
            (CAMPAIGN_CODE, DAILY_414_TIME_LIMIT_SECONDS, active_from),
        )
        for index in range(1, 11):
            question_id = int(
                conn.execute(
                    """
                    INSERT INTO quiz_questions(
                        campaign_code, code, type, title, required, points,
                        position, is_active, game_round
                    ) VALUES (?, ?, 'single_choice', ?, 1, 1, ?, 1, 'main')
                    """,
                    (CAMPAIGN_CODE, f"m{index}", f"Вопрос {index}", index * 10),
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
        for index in range(1, 3):
            final_id = int(
                conn.execute(
                    """
                    INSERT INTO quiz_questions(
                        campaign_code, code, type, title, game_round,
                        required, points, position, is_active
                    ) VALUES (?, ?, 'single_choice', ?, 'final', 1, 1, ?, 1)
                    """,
                    (CAMPAIGN_CODE, f"f{index}", f"Финал {index}", index * 10),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO quiz_options(
                    question_id, code, text, is_correct, position
                ) VALUES (?, ?, 'Верно', 1, 10), (?, ?, 'Неверно', 0, 20)
                """,
                (final_id, f"f{index}_yes", final_id, f"f{index}_no"),
            )

        client_id = int(
            conn.execute(
                """
                INSERT INTO clients(
                    first_name, phone_raw, phone_full, phone_local, source
                ) VALUES ('E2E', '+7 900 000-00-01', '79000000001',
                          '9000000001', 'e2e')
                """
            ).lastrowid
        )
        account_id = int(
            conn.execute(
                """
                INSERT INTO member_accounts(
                    client_id, email, email_normalized, password_hash,
                    email_verified_at
                ) VALUES (?, 'e2e@load.test', 'e2e@load.test', ?, CURRENT_TIMESTAMP)
                """,
                (client_id, password_hash),
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
                ) VALUES (?, ?, ?, ?, 'e2e-ip', 'playwright')
                """,
                (
                    account_id,
                    document["id"],
                    document["code"],
                    document["version"],
                ),
            )
        return issue_session(
            conn,
            secret_key=settings.secret_key,
            account_id=account_id,
            session_version=1,
            days=1,
            ip_hash="e2e-ip",
            user_agent="playwright",
        )


@pytest.fixture()
def jackside_server(tmp_path: Path, request):
    db_path = tmp_path / "e2e.sqlite3"
    secret = "e2e-secret-key-that-is-longer-than-32-characters"
    settings = Settings(
        admin_pin="2468",
        admin_name="E2E Admin",
        secret_key=secret,
        db_path=db_path,
        secure_cookie=False,
        member_portal_enabled=True,
        public_base_url="http://127.0.0.1",
        quiz_public_base_url="http://127.0.0.1",
    )
    init_db(db_path)
    start_delay_seconds = int(getattr(request, "param", -30))
    member_token = _seed_e2e_world(
        settings, start_delay_seconds=start_delay_seconds
    )
    port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "HJC_ADMIN_PIN": "2468",
            "HJC_ADMIN_NAME": "E2E Admin",
            "HJC_SECRET_KEY": secret,
            "HJC_DB_PATH": str(db_path),
            "HJC_SECURE_COOKIE": "0",
            "HJC_MEMBER_PORTAL_ENABLED": "1",
            "HJC_HOST": "127.0.0.1",
            "HJC_PORT": str(port),
            "HJC_PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
            "HJC_QUIZ_PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
            "PYTHONPATH": str(ROOT),
            "PYTHONUNBUFFERED": "1",
        }
    )
    log_path = tmp_path / "uvicorn.log"
    log_file = open(log_path, "w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os\n"
                "from app.config import Settings\n"
                "from app.main import create_app\n"
                "import uvicorn\n"
                "settings = Settings(\n"
                "    admin_pin=os.environ['HJC_ADMIN_PIN'],\n"
                "    admin_name=os.environ.get('HJC_ADMIN_NAME', 'E2E'),\n"
                "    secret_key=os.environ['HJC_SECRET_KEY'],\n"
                "    db_path=os.environ['HJC_DB_PATH'],\n"
                "    secure_cookie=False,\n"
                "    member_portal_enabled=True,\n"
                "    public_base_url=os.environ['HJC_PUBLIC_BASE_URL'],\n"
                "    quiz_public_base_url=os.environ['HJC_QUIZ_PUBLIC_BASE_URL'],\n"
                ")\n"
                "uvicorn.run(create_app(settings), host='127.0.0.1', "
                f"port={port}, log_level='warning')\n"
            ),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        try:
            _wait_http(f"{base_url}/account/login")
        except Exception:
            log_file.flush()
            print(log_path.read_text(encoding="utf-8", errors="replace")[:4000])
            raise
        yield {
            "base_url": base_url,
            "campaign": CAMPAIGN_CODE,
            "member_token": member_token,
            "log_path": log_path,
            "db_path": db_path,
            "start_delay_seconds": start_delay_seconds,
        }
    finally:
        process.kill()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        log_file.close()


def test_jackside_welcome_points_are_shown_without_classic_meta(
    page: Page, jackside_server: dict
) -> None:
    base_url = jackside_server["base_url"]
    campaign = jackside_server["campaign"]
    page.context.add_cookies(
        [
            {
                "name": MEMBER_COOKIE_NAME,
                "value": jackside_server["member_token"],
                "url": base_url,
            }
        ]
    )
    page.goto(
        f"{base_url}/quiz?campaign={campaign}", wait_until="domcontentloaded"
    )
    points = page.locator(".jackside-welcome-points")
    try:
        # JS moves welcome from loading → active; wait for points list.
        expect(points).to_be_visible(timeout=20_000)
        expect(points).to_contain_text("Данный квиз прошли")
        expect(page.locator(".quiz-screen[data-screen='welcome'] .quiz-welcome-meta")).to_be_hidden()
        expect(
            page.locator(
                ".quiz-screen[data-screen='welcome'] .quiz-lead[data-content='welcome-text']"
            )
        ).to_be_hidden()
    except Exception:
        print("URL:", page.url)
        print("BODY:", page.content()[:2500])
        log_path = Path(jackside_server["log_path"])
        if log_path.exists():
            print(
                "UVICORN:",
                log_path.read_text(encoding="utf-8", errors="replace")[:2500],
            )
        raise


def _countdown_seconds(page: Page, selector: str) -> int:
    root = page.locator(selector)
    days = int(root.locator("[data-countdown-days]").inner_text())
    hours = int(root.locator("[data-countdown-hours]").inner_text())
    minutes = int(root.locator("[data-countdown-minutes]").inner_text())
    seconds = int(root.locator("[data-countdown-seconds]").inner_text())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


@pytest.mark.parametrize("jackside_server", [24], indirect=True)
def test_upcoming_jackside_countdown_survives_reload_and_starts_once(
    page: Page, jackside_server: dict
) -> None:
    base_url = jackside_server["base_url"]
    campaign = jackside_server["campaign"]
    page.context.add_cookies(
        [
            {
                "name": MEMBER_COOKIE_NAME,
                "value": jackside_server["member_token"],
                "url": base_url,
            }
        ]
    )
    page.add_init_script(
        """
        (() => {
          const realNow = Date.now.bind(Date);
          Date.now = () => realNow() + (12 * 60 * 60 * 1000);
        })();
        """
    )
    page.goto(
        f"{base_url}/account?tab=quizzes", wait_until="domcontentloaded"
    )
    account_countdown = page.locator("[data-member-countdown]").first
    expect(account_countdown).to_be_visible(timeout=20_000)
    expect(account_countdown).not_to_have_text("Можно играть")

    page.goto(
        f"{base_url}/quiz?campaign={campaign}", wait_until="domcontentloaded"
    )

    welcome = page.locator("[data-countdown-welcome]")
    expect(welcome).to_be_visible(timeout=20_000)
    expect(page.locator(".jackside-welcome-points")).to_be_visible()
    expect(page.locator(".jackside-welcome-points")).to_contain_text(
        "Один стол на весь клуб"
    )
    expect(page.locator(".jackside-welcome-points")).to_contain_text("JACKCOIN")

    first_value = _countdown_seconds(page, "[data-countdown-welcome]")
    assert 0 < first_value <= 30
    page.wait_for_timeout(1300)
    second_value = _countdown_seconds(page, "[data-countdown-welcome]")
    assert 0 <= second_value < first_value

    with connect(jackside_server["db_path"]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM quiz_attempts WHERE campaign_code=?",
            (campaign,),
        ).fetchone()[0] == 0

    context = page.context
    page.close()
    page = context.new_page()
    page.goto(f"{base_url}/quiz?campaign={campaign}", wait_until="domcontentloaded")
    expect(page.locator("[data-countdown-welcome]")).to_be_visible(timeout=20_000)
    reloaded_value = _countdown_seconds(page, "[data-countdown-welcome]")
    assert 0 < reloaded_value < first_value

    page.locator("[data-action='identify']").click()
    expect(page.locator("[data-screen='daily-prize']")).to_be_visible()
    page.locator("[data-action='daily-prize-next']").click()
    expect(page.locator("[data-screen='daily-jackcoin']")).to_be_visible()
    page.locator("[data-action='daily-start']").click()
    expect(page.locator("[data-screen='countdown']")).to_be_visible()

    with connect(jackside_server["db_path"]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM quiz_attempts WHERE campaign_code=?",
            (campaign,),
        ).fetchone()[0] == 0

    expect(page.locator("[data-screen='question']")).to_be_visible(timeout=25_000)
    with connect(jackside_server["db_path"]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM quiz_attempts WHERE campaign_code=?",
            (campaign,),
        ).fetchone()[0] == 1


def test_classic_upcoming_countdown_is_unchanged(
    page: Page, jackside_server: dict
) -> None:
    base_url = jackside_server["base_url"]
    local_now = datetime.now(ZoneInfo("Europe/Moscow")).replace(tzinfo=None)
    with transaction(jackside_server["db_path"]) as conn:
        conn.execute(
            """
            INSERT INTO quiz_campaigns(
                code, title, campaign_type, is_active, active_from, active_until
            ) VALUES ('classic_upcoming', 'Classic upcoming', 'classic', 1, ?, ?)
            """,
            (
                (local_now + timedelta(seconds=20)).strftime(
                    "%Y-%m-%dT%H:%M:%S"
                ),
                (local_now + timedelta(hours=1)).strftime(
                    "%Y-%m-%dT%H:%M:%S"
                ),
            ),
        )

    page.goto(
        f"{base_url}/quiz?campaign=classic_upcoming",
        wait_until="domcontentloaded",
    )
    countdown = page.locator("[data-screen='countdown']")
    expect(countdown).to_be_visible(timeout=20_000)
    first_value = _countdown_seconds(page, "[data-screen='countdown']")
    assert 0 < first_value <= 25
    page.wait_for_timeout(1300)
    assert _countdown_seconds(page, "[data-screen='countdown']") < first_value
