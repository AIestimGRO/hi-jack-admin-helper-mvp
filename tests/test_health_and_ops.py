from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import SCHEMA_VERSION, connect, init_db
from app.main import create_app
from app.services.ops_log import log_event


def make_client(tmp_path: Path) -> tuple[TestClient, Settings]:
    settings = Settings(
        admin_pin="2468",
        admin_name="Test Admin",
        secret_key="test-secret-key-that-is-longer-than-32-characters",
        db_path=tmp_path / "app.sqlite3",
        secure_cookie=False,
    )
    return TestClient(create_app(settings)), settings


def test_health_returns_schema_and_db_flags(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["app"] == "ok"
    assert payload["database"] == "ok"
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["build_version"]
    assert payload["db_readable"] is True
    assert payload["db_writable"] is False


def test_connect_enables_wal(tmp_path: Path) -> None:
    db_path = tmp_path / "wal.sqlite3"
    init_db(db_path)
    with connect(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


def test_log_event_does_not_raise() -> None:
    log_event(
        "health_probe",
        status="ok",
        duration_ms=12,
        issue_id=1,
        submission_id=2,
        final_table_id=3,
        member_id=4,
    )
    log_event("quiz_start", status="error", error_code="http_409")


def test_health_does_not_write_and_deep_probe_is_explicit(tmp_path: Path) -> None:
    client, settings = make_client(tmp_path)
    with client:
        with connect(settings.db_path) as conn:
            before = conn.execute(
                "SELECT checked_at FROM health_probes WHERE id=1"
            ).fetchone()
        response = client.get("/health")
        ready = client.get("/health/ready")
        with connect(settings.db_path) as conn:
            after_read_checks = conn.execute(
                "SELECT checked_at FROM health_probes WHERE id=1"
            ).fetchone()
        deep = client.post("/health/deep")
    assert response.status_code == 200
    assert ready.status_code == 200
    assert before is None
    assert after_read_checks is None
    assert deep.status_code == 200
    assert deep.json()["db_writable"] is True
    with connect(settings.db_path) as conn:
        after_deep = conn.execute(
            "SELECT checked_at FROM health_probes WHERE id=1"
        ).fetchone()
    assert after_deep is not None


def test_wal_persists_after_reopening_without_reset(tmp_path: Path) -> None:
    db_path = tmp_path / "wal-reopen.sqlite3"
    init_db(db_path)
    with connect(db_path) as first:
        first_mode = first.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(first_mode).lower() == "wal"
    with connect(db_path) as second:
        second_mode = second.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(second_mode).lower() == "wal"
        assert int(second.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        assert int(second.execute("PRAGMA busy_timeout").fetchone()[0]) == 30000
