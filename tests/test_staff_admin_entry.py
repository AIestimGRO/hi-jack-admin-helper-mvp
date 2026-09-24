from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app.admin_access_control import (
    ACCESS_BARTENDER,
    ACCESS_MASTER,
    ACCESS_QUIZ_MANAGER,
    ensure_admin_access_schema,
)
from app.config import Settings
from app.db import init_db, transaction
from app.staff_admin_entry import install_staff_admin_entry, scoped_admin_landing


ROOT = Path(__file__).resolve().parents[1]


def _seed_admins(settings: Settings) -> tuple[int, int, int]:
    init_db(settings.db_path)
    with transaction(settings.db_path) as conn:
        ensure_admin_access_schema(conn)
        master_id = int(
            conn.execute(
                "INSERT INTO admins(username,display_name,pin_hash,role) VALUES (?,?,?,?)",
                ("entrymaster", "Master", "test", "master_admin"),
            ).lastrowid
        )
        manager_id = int(
            conn.execute(
                "INSERT INTO admins(username,display_name,pin_hash,role) VALUES (?,?,?,?)",
                ("entrymanager", "Manager", "test", "master_admin"),
            ).lastrowid
        )
        bartender_id = int(
            conn.execute(
                "INSERT INTO admins(username,display_name,pin_hash,role) VALUES (?,?,?,?)",
                ("entrybartender", "Bartender", "test", "admin"),
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO admin_access_profiles(admin_id,access_role) VALUES (?,'quiz_manager')",
            (manager_id,),
        )
        conn.execute(
            "INSERT INTO admin_access_profiles(admin_id,access_role) VALUES (?,'bartender')",
            (bartender_id,),
        )
    return master_id, manager_id, bartender_id


def _make_entry_app(tmp_path: Path) -> tuple[TestClient, dict[str, int]]:
    settings = Settings(
        db_path=tmp_path / "staff-entry.sqlite3",
        secret_key="staff-entry-secret",
        admin_pin="1234",
        secure_cookie=False,
    )
    master_id, manager_id, bartender_id = _seed_admins(settings)
    app = FastAPI()
    app.state.settings = settings
    install_staff_admin_entry(app)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        https_only=False,
    )

    @app.get("/")
    async def root() -> PlainTextResponse:
        return PlainTextResponse("master-root")

    @app.get("/staff/quizzes")
    async def staff_quizzes() -> PlainTextResponse:
        return PlainTextResponse("quiz-manager-home")

    @app.get("/clients")
    async def clients() -> PlainTextResponse:
        return PlainTextResponse("bartender-home")

    @app.get("/login/{role}")
    async def login_as(request: Request, role: str) -> RedirectResponse:
        admin_ids = {
            "master": master_id,
            "manager": manager_id,
            "bartender": bartender_id,
        }
        request.session.clear()
        request.session.update(
            {
                "authenticated": True,
                "admin_id": admin_ids[role],
            }
        )
        return RedirectResponse("/", status_code=303)

    return TestClient(app), {
        "master": master_id,
        "manager": manager_id,
        "bartender": bartender_id,
    }


def test_scoped_admin_landing_is_role_specific() -> None:
    assert scoped_admin_landing(ACCESS_MASTER) is None
    assert scoped_admin_landing(ACCESS_QUIZ_MANAGER) == "/staff/quizzes"
    assert scoped_admin_landing(ACCESS_BARTENDER) == "/clients"


def test_scoped_staff_login_and_stale_master_route_land_safely(tmp_path: Path) -> None:
    client, _ = _make_entry_app(tmp_path)
    with client:
        manager = client.get("/login/manager", follow_redirects=True)
        assert manager.status_code == 200
        assert manager.text == "quiz-manager-home"
        stale_manager = client.get("/master", follow_redirects=False)
        assert stale_manager.status_code == 303
        assert stale_manager.headers["location"] == "/staff/quizzes"

        bartender = client.get("/login/bartender", follow_redirects=True)
        assert bartender.status_code == 200
        assert bartender.text == "bartender-home"
        stale_bartender = client.get("/master", follow_redirects=False)
        assert stale_bartender.status_code == 303
        assert stale_bartender.headers["location"] == "/clients"

        master = client.get("/login/master", follow_redirects=True)
        assert master.status_code == 200
        assert master.text == "master-root"


def test_scoped_staff_navigation_exposes_redeem_without_widening_master_ui() -> None:
    base = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")

    assert "{% if is_bartender or is_manager %}" in base
    assert 'href="/staff/redeem"' in base
    assert "'/admin/vault' if is_master else '/staff/redeem'" in base
    assert "'Store' if is_master else 'Погасить'" in base
    assert "install_staff_admin_entry" in main
