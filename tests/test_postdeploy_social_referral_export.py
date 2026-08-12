import io
import json
import re
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import transaction
from app.main import create_app
from app.services.jackside_engagement import ensure_jackside_referral_code
from app.services.member_accounts import MEMBER_COOKIE_NAME, issue_session
from app.services.quiz import parse_quick_questions


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


def member_cookie(settings: Settings, *, email: str = "member@example.com") -> str:
    with transaction(settings.db_path) as conn:
        client_id = int(
            conn.execute(
                "INSERT INTO clients(first_name,phone_local,source) VALUES ('Member','9991112233','test')"
            ).lastrowid
        )
        account_id = int(
            conn.execute(
                """
                INSERT INTO member_accounts(
                    client_id,email,email_normalized,password_hash,email_verified_at
                ) VALUES (?,?,?,?,CURRENT_TIMESTAMP)
                """,
                (client_id, email, email.lower(), "test-hash"),
            ).lastrowid
        )
        row = conn.execute(
            "SELECT session_version FROM member_accounts WHERE id=?",
            (account_id,),
        ).fetchone()
        return issue_session(
            conn,
            secret_key=settings.secret_key,
            account_id=account_id,
            session_version=int(row["session_version"]),
            days=7,
            ip_hash="test",
            user_agent="pytest",
        )


def test_member_social_links_page_has_complete_member_context(tmp_path: Path) -> None:
    client, settings = make_client(tmp_path)
    with client:
        token = member_cookie(settings)
        client.cookies.set(MEMBER_COOKIE_NAME, token)
        with transaction(settings.db_path) as conn:
            conn.execute(
                """
                UPDATE club_social_links
                SET url='https://example.com/club',is_active=1
                WHERE code='telegram'
                """
            )

        page = client.get("/account/links")
        assert page.status_code == 200
        assert "Hi, Jack в сети" in page.text
        assert "https://example.com/club" in page.text


def test_referral_link_sends_new_visitor_to_registration(tmp_path: Path) -> None:
    client, settings = make_client(tmp_path)
    with client:
        with transaction(settings.db_path) as conn:
            referrer_id = int(
                conn.execute(
                    "INSERT INTO clients(first_name,phone_local,source) VALUES ('Referrer','9992223344','test')"
                ).lastrowid
            )
            code_row = ensure_jackside_referral_code(conn, referrer_id)
            code = str(code_row["code"])

        response = client.get(f"/jackside/ref/{code}", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/account/register"


def test_quiz_export_zip_contains_structure_answers_and_images(tmp_path: Path) -> None:
    client, settings = make_client(tmp_path)
    with client:
        login_master(client)
        media_dir = Path(settings.db_path).parent / "quiz-media" / "export-demo"
        media_dir.mkdir(parents=True, exist_ok=True)
        (media_dir / "question.png").write_bytes(b"question-image")
        (media_dir / "section.webp").write_bytes(b"section-image")

        with transaction(settings.db_path) as conn:
            campaign_id = int(
                conn.execute(
                    """
                    INSERT INTO quiz_campaigns(code,title,campaign_type)
                    VALUES ('export-demo','Export Demo','classic')
                    """
                ).lastrowid
            )
            section_id = int(
                conn.execute(
                    """
                    INSERT INTO quiz_sections(
                        campaign_code,title,theme,background_image,position
                    ) VALUES ('export-demo','Photos','photo','/quiz-media/export-demo/section.webp',10)
                    """
                ).lastrowid
            )
            question_id = int(
                conn.execute(
                    """
                    INSERT INTO quiz_questions(
                        campaign_code,code,type,title,visual_type,image_path,section_id,
                        game_round,required,points,position,is_active
                    ) VALUES (
                        'export-demo','q1','single_choice','Question?','photo',
                        '/quiz-media/export-demo/question.png',?,'main',1,3,10,1
                    )
                    """,
                    (section_id,),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO quiz_options(question_id,code,text,is_correct,position,is_active)
                VALUES (?, 'a', 'Wrong', 0, 10, 1),
                       (?, 'b', 'Correct', 1, 20, 1)
                """,
                (question_id, question_id),
            )

        response = client.get(f"/api/master/quiz-campaigns/{campaign_id}/export.zip")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/zip")

        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = set(archive.namelist())
            assert "quiz.json" in names
            assert "README.txt" in names
            assert "images/question.png" in names
            assert "images/section.webp" in names
            payload = json.loads(archive.read("quiz.json"))

        assert payload["format"] == "hi-jack-quiz-export"
        assert payload["campaign"]["code"] == "export-demo"
        assert payload["sections"][0]["background_image_archive"] == "images/section.webp"
        assert payload["questions"][0]["image_archive"] == "images/question.png"
        assert payload["questions"][0]["options"][1]["correct"] is True


def test_quiz_export_txt_is_directly_accepted_by_bulk_parser(tmp_path: Path) -> None:
    client, settings = make_client(tmp_path)
    with client:
        login_master(client)
        with transaction(settings.db_path) as conn:
            campaign_id = int(
                conn.execute(
                    """
                    INSERT INTO quiz_campaigns(code,title,campaign_type)
                    VALUES ('txt-demo','TXT Demo','classic')
                    """
                ).lastrowid
            )
            first_id = int(
                conn.execute(
                    """
                    INSERT INTO quiz_questions(
                        campaign_code,code,type,title,game_round,required,points,position,is_active
                    ) VALUES ('txt-demo','q1','single_choice','Столица Франции?','main',1,1,10,1)
                    """
                ).lastrowid
            )
            second_id = int(
                conn.execute(
                    """
                    INSERT INTO quiz_questions(
                        campaign_code,code,type,title,game_round,required,points,position,is_active
                    ) VALUES ('txt-demo','q2','multi_choice','Выберите простые числа','main',1,1,20,1)
                    """
                ).lastrowid
            )
            conn.executemany(
                """
                INSERT INTO quiz_options(question_id,code,text,is_correct,position,is_active)
                VALUES (?,?,?,?,?,1)
                """,
                [
                    (first_id, "a", "Лондон", 0, 10),
                    (first_id, "b", "Париж", 1, 20),
                    (first_id, "c", "Берлин", 0, 30),
                    (second_id, "a", "2", 1, 10),
                    (second_id, "b", "3", 1, 20),
                    (second_id, "c", "4", 0, 30),
                ],
            )

        response = client.get(f"/api/master/quiz-campaigns/{campaign_id}/export.txt")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "questions.txt" in response.headers["content-disposition"]

        text = response.text.lstrip("\ufeff")
        assert "Столица Франции?\n-Лондон\n*Париж\n-Берлин" in text
        assert "Выберите простые числа\n*2\n*3\n-4" in text
        assert "@photo" not in text
        assert "/quiz-media/" not in text

        parsed = parse_quick_questions(text)
        assert len(parsed) == 2
        assert parsed[0]["question_type"] == "single_choice"
        assert parsed[0]["options"][1]["is_correct"] is True
        assert parsed[1]["question_type"] == "multi_choice"
        assert sum(int(item["is_correct"]) for item in parsed[1]["options"]) == 2
