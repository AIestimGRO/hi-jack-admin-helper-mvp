import io
import re
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.db import transaction
from app.main import create_app


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


def _seed_legacy_friday(settings: Settings) -> int:
    with transaction(settings.db_path) as conn:
        campaign_id = int(
            conn.execute(
                """
                INSERT INTO quiz_campaigns(
                    code,title,campaign_type,is_active,active_from,active_until
                ) VALUES (
                    'friday-old','Пятница','daily_414',0,
                    '2026-08-07T18:14:00+03:00','2026-08-07T23:59:59+03:00'
                )
                """
            ).lastrowid
        )
        for code, title, game_round, position in (
            ("q1", "Основной вопрос", "main", 10),
            ("f1", "Финальный вопрос", "final", 100),
        ):
            question_id = int(
                conn.execute(
                    """
                    INSERT INTO quiz_questions(
                        campaign_code,code,type,title,game_round,required,
                        points,position,is_active
                    ) VALUES ('friday-old',?,'single_choice',?,?,1,1,?,1)
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


def test_master_clients_is_primary_workspace_with_business_metrics(tmp_path: Path) -> None:
    client, settings = make_client(tmp_path)
    with client:
        login_master(client)
        with transaction(settings.db_path) as conn:
            client_id = int(
                conn.execute(
                    """
                    INSERT INTO clients(first_name,nickname,phone_local,source)
                    VALUES ('Alex','Shark','9991112233','test')
                    """
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO jackcoin_ledger(
                    client_id,amount,operation_type,source_type,source_id,
                    idempotency_key,comment
                ) VALUES (?,150,'earn','test','1','ia:test:earn','test')
                """,
                (client_id,),
            )
            conn.execute(
                """
                INSERT INTO jackcoin_ledger(
                    client_id,amount,operation_type,source_type,source_id,
                    idempotency_key,comment
                ) VALUES (?,-50,'spend','test','2','ia:test:spend','test')
                """,
                (client_id,),
            )
        page = client.get("/master/clients")
        assert page.status_code == 200
        assert "Клиенты" in page.text
        assert "HI, JACK! рейтинг" in page.text
        assert "Баланс" in page.text
        assert "100 JC" in page.text
        assert "заработано 150" in page.text
        assert "Последняя операция JC" in page.text
        assert "data-client-scan-start" in page.text
        assert "Сканер" in page.text
        assert "href=\"/clients/import\"" not in page.text
        assert "Основное" not in page.text
        assert "Выпуски JACKSIDE" not in page.text


def test_jackside_workspace_renders_legacy_source_without_async_loading(tmp_path: Path) -> None:
    client, settings = make_client(tmp_path)
    with client:
        login_master(client)
        campaign_id = _seed_legacy_friday(settings)
        page = client.get("/master/jackside")
        assert page.status_code == 200
        assert "Пятница" in page.text
        assert f'value="legacy:{campaign_id}"' in page.text
        assert "Новый выпуск из этого квиза" in page.text
        assert "Загрузка..." not in page.text


def test_jackside_facade_copies_legacy_without_touching_source(tmp_path: Path) -> None:
    client, settings = make_client(tmp_path)
    with client:
        login_master(client)
        campaign_id = _seed_legacy_friday(settings)
        page = client.get("/master/jackside")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = client.post(
            "/api/master/jackside/create-release",
            data={
                "csrf_token": token,
                "issue_date": "2026-08-13",
                "starts_at": "2026-08-13T18:14",
                "title": "JACKSIDE TEST",
                "source": f"legacy:{campaign_id}",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/master/jackside?")
        with transaction(settings.db_path) as conn:
            source = conn.execute(
                "SELECT title,code FROM quiz_campaigns WHERE id=?", (campaign_id,)
            ).fetchone()
            issue = conn.execute(
                "SELECT * FROM jackside_issues WHERE issue_date=?",
                (date(2026, 8, 13).isoformat(),),
            ).fetchone()
            target = conn.execute(
                "SELECT * FROM quiz_campaigns WHERE code=?",
                (issue["campaign_code"],),
            ).fetchone()
            source_questions = int(
                conn.execute(
                    "SELECT COUNT(*) FROM quiz_questions WHERE campaign_code='friday-old'"
                ).fetchone()[0]
            )
        assert source["title"] == "Пятница"
        assert source["code"] == "friday-old"
        assert source_questions == 2
        assert issue["title"] == "JACKSIDE TEST"
        assert int(issue["main_question_count"]) == 1
        assert int(issue["final_question_count"]) == 1
        assert target["title"] == "JACKSIDE TEST"


def test_reports_workspace_combines_results_and_analytics(tmp_path: Path) -> None:
    client, _settings = make_client(tmp_path)
    with client:
        login_master(client)
        page = client.get("/master/reports")
        assert page.status_code == 200
        assert "Результаты и аналитика" in page.text
        assert "Обзор" in page.text
        assert "Результаты игроков" in page.text
        assert "По выпускам" in page.text
        assert "Completion rate" in page.text


def test_legacy_admin_urls_remain_registered(tmp_path: Path) -> None:
    client, _settings = make_client(tmp_path)
    with client:
        paths = {route.path for route in client.app.routes}
        for path in {
            "/master/jackside-issues",
            "/admin/quiz-results",
            "/master/referrals",
            "/master/engagement-icons",
            "/master/member-accounts",
            "/master/economy",
            "/master/hijack-rating",
        }:
            assert path in paths

def test_engagement_workspace_links_and_round_trips_custom_title_icon(tmp_path: Path) -> None:
    client, settings = make_client(tmp_path)
    with client:
        login_master(client)

        workspace = client.get("/master?tab=engagement")
        assert workspace.status_code == 200
        assert 'href="/master/engagement-icons"' in workspace.text
        assert "Управлять иконками" in workspace.text

        manager = client.get("/master/engagement-icons")
        assert manager.status_code == 200
        assert "Иконки званий и достижений" in manager.text
        token = re.search(r'name="csrf_token" value="([^"]+)"', manager.text).group(1)

        with transaction(settings.db_path) as conn:
            title_id = int(
                conn.execute(
                    "SELECT id FROM title_definitions WHERE is_enabled=1 ORDER BY id LIMIT 1"
                ).fetchone()[0]
            )

        image = Image.new("RGBA", (96, 64), (20, 180, 140, 255))
        payload = io.BytesIO()
        image.save(payload, format="PNG")

        upload = client.post(
            f"/api/master/engagement-icons/title/{title_id}",
            data={"csrf_token": token},
            files={"icon": ("title-icon.png", payload.getvalue(), "image/png")},
            follow_redirects=False,
        )
        assert upload.status_code == 303
        assert upload.headers["location"].startswith("/master/engagement-icons?ok=")

        with transaction(settings.db_path) as conn:
            icon_path = str(
                conn.execute(
                    "SELECT icon_path FROM title_definitions WHERE id=?", (title_id,)
                ).fetchone()[0]
                or ""
            )
        assert icon_path.startswith("/reward-media/engagement-icons/")
        stored_file = settings.db_path.parent / "reward-media" / "engagement-icons" / Path(icon_path).name
        assert stored_file.is_file()

        remove = client.post(
            f"/api/master/engagement-icons/title/{title_id}/remove",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert remove.status_code == 303
        assert remove.headers["location"].startswith("/master/engagement-icons?ok=")

        with transaction(settings.db_path) as conn:
            cleared = conn.execute(
                "SELECT icon_path FROM title_definitions WHERE id=?", (title_id,)
            ).fetchone()[0]
        assert cleared is None
        assert not stored_file.exists()

