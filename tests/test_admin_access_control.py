from pathlib import Path

from app.admin_access_control import (
    ACCESS_BARTENDER,
    ACCESS_MASTER,
    ACCESS_QUIZ_MANAGER,
    access_path_allowed,
    effective_access_role,
    ensure_admin_access_schema,
)
from app.db import init_db, transaction
from app.staff_quiz_admin import _validate_campaign_values


ROOT = Path(__file__).resolve().parents[1]


def _admin(conn, *, username: str, role: str) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO admins(username,display_name,pin_hash,role)
            VALUES (?,?,?,?)
            """,
            (username, username, "test-hash", role),
        ).lastrowid
    )


def test_effective_access_roles_preserve_master_and_scope_staff(tmp_path) -> None:
    db_path = tmp_path / "roles.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_admin_access_schema(conn)
        master_id = _admin(conn, username="master2", role="master_admin")
        manager_id = _admin(conn, username="quizmanager", role="master_admin")
        bartender_id = _admin(conn, username="bartender", role="admin")
        legacy_admin_id = _admin(conn, username="legacyadmin", role="admin")
        conn.execute(
            "INSERT INTO admin_access_profiles(admin_id,access_role) VALUES (?,'quiz_manager')",
            (manager_id,),
        )
        conn.execute(
            "INSERT INTO admin_access_profiles(admin_id,access_role) VALUES (?,'bartender')",
            (bartender_id,),
        )

        assert effective_access_role(conn, admin_id=master_id) == ACCESS_MASTER
        assert effective_access_role(conn, admin_id=manager_id) == ACCESS_QUIZ_MANAGER
        assert effective_access_role(conn, admin_id=bartender_id) == ACCESS_BARTENDER
        assert effective_access_role(conn, admin_id=legacy_admin_id) == ACCESS_BARTENDER


def test_bartender_allowlist_is_operational_only() -> None:
    allowed = [
        ("/clients", "GET"),
        ("/clients/123", "GET"),
        ("/api/clients/123/qr", "GET"),
        ("/api/preferences/add", "POST"),
        ("/api/preferences/spend", "POST"),
        ("/api/preferences/set-discount", "POST"),
        ("/staff/redeem", "GET"),
        ("/staff/redeem", "POST"),
        ("/manifest.webmanifest", "GET"),
        ("/favicon.ico", "GET"),
        ("/service-worker.js", "GET"),
        ("/pwa/icon-192.png", "GET"),
    ]
    denied = [
        ("/", "GET"),
        ("/clients/import", "GET"),
        ("/clients/123/comment", "POST"),
        ("/admin/vault", "GET"),
        ("/api/vault/catalog/create", "POST"),
        ("/admin/quiz-results", "GET"),
        ("/staff/quizzes", "GET"),
        ("/staff-users", "GET"),
        ("/staff-access", "GET"),
        ("/logs", "GET"),
        ("/master", "GET"),
        ("/master/economy", "GET"),
    ]
    for path, method in allowed:
        assert access_path_allowed(ACCESS_BARTENDER, path=path, method=method)
    for path, method in denied:
        assert not access_path_allowed(ACCESS_BARTENDER, path=path, method=method)


def test_quiz_manager_can_run_content_and_rewards_but_not_master_system() -> None:
    allowed = [
        ("/", "GET"),
        ("/clients", "GET"),
        ("/clients/42", "GET"),
        ("/admin/quiz-results", "GET"),
        ("/master/quiz-builder/7", "GET"),
        ("/api/master/quiz-campaigns/7/questions", "POST"),
        ("/master/jackside-issues", "GET"),
        ("/api/master/jackside-issues/create", "POST"),
        ("/staff/quizzes", "GET"),
        ("/api/staff/quizzes/create", "POST"),
        ("/admin/vault", "GET"),
        ("/api/vault/catalog/create", "POST"),
        ("/admin/rewards", "GET"),
        ("/staff-users", "GET"),
        ("/api/staff-users/12/reset-password", "POST"),
        ("/staff-access", "GET"),
        ("/api/staff-access/create", "POST"),
        ("/logs", "GET"),
    ]
    denied = [
        ("/master", "GET"),
        ("/master/economy", "GET"),
        ("/master/hijack-rating", "GET"),
        ("/master/member-accounts", "GET"),
        ("/master/legal-documents", "GET"),
        ("/master/club-links", "GET"),
        ("/master/engagement-icons", "GET"),
        ("/clients/import", "GET"),
        ("/api/clients/42/comment", "POST"),
        ("/api/clients/42/jackcoin/credit", "POST"),
        ("/api/clients/42/quiz/test/extra-attempt", "POST"),
        ("/api/master/admins", "POST"),
        ("/api/master/hijack-rating/transfer-owner", "POST"),
        ("/api/master/economy/update", "POST"),
    ]
    for path, method in allowed:
        assert access_path_allowed(ACCESS_QUIZ_MANAGER, path=path, method=method)
    for path, method in denied:
        assert not access_path_allowed(ACCESS_QUIZ_MANAGER, path=path, method=method)


def test_master_access_is_unrestricted() -> None:
    for path, method in (
        ("/master", "GET"),
        ("/master/legal-documents", "GET"),
        ("/api/master/admins", "POST"),
        ("/clients/import", "GET"),
    ):
        assert access_path_allowed(ACCESS_MASTER, path=path, method=method)


def test_quiz_manager_campaign_validation_keeps_safe_limits() -> None:
    values = _validate_campaign_values(
        title="  Test quiz  ",
        pass_score=7,
        question_time_limit_seconds=20,
        quiz_time_limit_seconds=120,
        max_attempts=2,
        bonus_amount=1,
    )
    assert values == ("Test quiz", 7, 20, 120, 2, 1)


def test_scoped_navigation_does_not_send_manager_to_master_workspaces() -> None:
    base = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    dashboard = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    client = (ROOT / "app/templates/client_detail.html").read_text(encoding="utf-8")
    script = (ROOT / "app/static/js/prelaunch-admin.js").read_text(encoding="utf-8")

    assert "data-admin-access-role" in base
    assert "'/master/jackside' if is_master else '/staff/quizzes'" in base
    assert "'/master/reports' if is_master else '/admin/quiz-results'" in base
    assert "href=\"/staff-access\"" in base
    assert "access_role == 'quiz_manager'" in dashboard
    assert "{% if access_role == 'master' %}" in client
    assert "document.body.dataset.adminAccessRole === 'quiz_manager'" in script
    assert "link.href = '/staff/quizzes'" in script
