from pathlib import Path
import sqlite3

from scripts.telegram_relink_legacy_ids import repair_legacy_telegram_ids


def _seed(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE clients(
            id INTEGER PRIMARY KEY,
            telegram_user_id TEXT,
            updated_at TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO clients(id, telegram_user_id) VALUES (?, ?)",
        [
            (1, "123456789"),
            (2, "7927873562162456566"),
            (3, None),
        ],
    )
    conn.commit()
    conn.close()


def test_dry_run_does_not_change_database(tmp_path):
    db_path = tmp_path / "club_tools.sqlite3"
    _seed(db_path)

    result = repair_legacy_telegram_ids(db_path, apply=False)

    assert result == {"scanned": 2, "invalid": 1, "cleared": 0}
    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT telegram_user_id FROM clients WHERE id=2"
    ).fetchone()[0] == "7927873562162456566"
    conn.close()


def test_apply_archives_and_clears_only_invalid_ids(tmp_path):
    db_path = tmp_path / "club_tools.sqlite3"
    _seed(db_path)

    result = repair_legacy_telegram_ids(db_path, apply=True)

    assert result == {"scanned": 2, "invalid": 1, "cleared": 1}
    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT telegram_user_id FROM clients WHERE id=1"
    ).fetchone()[0] == "123456789"
    assert conn.execute(
        "SELECT telegram_user_id FROM clients WHERE id=2"
    ).fetchone()[0] is None
    assert conn.execute(
        """
        SELECT legacy_telegram_user_id
        FROM telegram_legacy_identity_archive
        WHERE client_id=2
        """
    ).fetchone()[0] == "7927873562162456566"
    conn.close()


def test_apply_is_idempotent(tmp_path):
    db_path = tmp_path / "club_tools.sqlite3"
    _seed(db_path)

    first = repair_legacy_telegram_ids(db_path, apply=True)
    second = repair_legacy_telegram_ids(db_path, apply=True)

    assert first["cleared"] == 1
    assert second == {"scanned": 1, "invalid": 0, "cleared": 0}
