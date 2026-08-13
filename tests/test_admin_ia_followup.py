import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import transaction
from app.main import create_app


ROOT = Path(__file__).resolve().parents[1]


def make_client(tmp_path: Path) -> tuple[TestClient, Settings]:
    settings = Settings(
        admin_pin="2468",
        admin_name="Test Admin",
        secret_key="test-secret-key-that-is-longer-than-32-characters",
        db_path=tmp_path / "app.sqlite3",
        secure_cookie=False,
    )
    return TestClient(create_app(settings)), settings


def login_master(client: TestClient) -> None:
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/login",
        data={"username": "master", "pin": "2468", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303


def seed_complete_legacy(settings: Settings) -> int:
    with transaction(settings.db_path) as conn:
        campaign_id = int(
            conn.execute(
                """
                INSERT INTO quiz_campaigns(
                    code,title,campaign_type,is_active,active_from,active_until
                ) VALUES (
                    'friday-complete','Пятница','daily_414',0,
                    '2026-08-07T18:14:00','2026-08-07T23:59:59'
                )
                """
            ).lastrowid
        )
        specs = [
            (f"q{index}", f"Основной {index}", "main", index * 10)
            for index in range(1, 11)
        ]
        specs.extend(
            (f"f{index}", f"Финальный {index}", "final", 1000 + index * 10)
            for index in range(1, 5)
        )
        for code, title, game_round, position in specs:
            question_id = int(
                conn.execute(
                    """
                    INSERT INTO quiz_questions(
                        campaign_code,code,type,title,game_round,required,
                        points,position,is_active
                    ) VALUES ('friday-complete',?,'single_choice',?,?,1,1,?,1)
                    """,
                    (code, title, game_round, position),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO quiz_options(question_id,code,text,is_correct,position)
                VALUES (?,'a','Неверно',0,10),(?,'b','Верно',1,20)
                """,
                (question_id, question_id),
            )
        return campaign_id


def test_client_subnavigation_persists_across_client_workspaces(tmp_path: Path) -> None:
    client, _settings = make_client(tmp_path)
    with client:
        login_master(client)
        for path in ("/master/clients", "/master/member-accounts", "/master/referrals"):
            response = client.get(path)
            assert response.status_code == 200
            assert 'aria-label="Разделы клиентов"' in response.text
            assert 'href="/master/clients"' in response.text
            assert 'href="/master/member-accounts"' in response.text
            assert 'href="/master/referrals"' in response.text


def test_jackside_workspace_has_no_cross_section_local_tabs() -> None:
    template = (ROOT / "app/templates/admin_jackside_workspace.html").read_text(
        encoding="utf-8"
    )
    assert 'href="/master/reports"' not in template
    assert 'href="/master/economy"' not in template
    assert "Расширенный режим" in template


def test_complete_legacy_copy_is_scheduled_and_keeps_moscow_wall_time(
    tmp_path: Path,
) -> None:
    client, settings = make_client(tmp_path)
    with client:
        login_master(client)
        campaign_id = seed_complete_legacy(settings)
        page = client.get("/master/jackside")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

        response = client.post(
            "/api/master/jackside/create-release",
            data={
                "csrf_token": token,
                "issue_date": "2026-08-14",
                "starts_at": "2026-08-14T12:30",
                "title": "JACKSIDE COPY",
                "source": f"legacy:{campaign_id}",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/master/jackside?ok=")

        with transaction(settings.db_path) as conn:
            issue = conn.execute(
                "SELECT * FROM jackside_issues WHERE issue_date='2026-08-14'"
            ).fetchone()
            campaign = conn.execute(
                "SELECT * FROM quiz_campaigns WHERE code=?",
                (issue["campaign_code"],),
            ).fetchone()

        assert issue["status"] == "scheduled"
        assert int(issue["main_question_count"]) == 10
        assert int(issue["final_question_count"]) == 4
        assert int(campaign["is_active"]) == 1
        assert campaign["active_from"] == "2026-08-14T12:30:00"

        workspace = client.get("/master/jackside")
        assert workspace.status_code == 200
        assert "Старт: 14.08.2026 12:30" in workspace.text
        assert "Старт: 14.08.2026 15:30" not in workspace.text
