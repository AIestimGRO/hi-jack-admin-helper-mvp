from pathlib import Path

from app.db import init_db, transaction
from app.referral_tree_visibility import referral_tree_without_deleted


def _client(conn, phone: str, nickname: str) -> int:
    cursor = conn.execute(
        "INSERT INTO clients(phone_raw,phone_full,phone_local,nickname,source) VALUES (?,?,?,?,?)",
        (f"+7{phone}", f"+7{phone}", phone, nickname, "test"),
    )
    return int(cursor.lastrowid)


def _link(conn, referrer_id: int, invited_id: int) -> None:
    conn.execute(
        """
        INSERT INTO referral_qualification_progress(
            referrer_client_id,invited_client_id,distinct_completed_days
        ) VALUES (?,?,?)
        """,
        (referrer_id, invited_id, 1),
    )


def test_deleted_referral_and_its_branch_are_hidden(tmp_path: Path) -> None:
    db_path = tmp_path / "referral-tree.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        root = _client(conn, "9991000001", "Root")
        active = _client(conn, "9991000002", "Active")
        deleted = _client(conn, "9991000003", "Deleted")
        below_deleted = _client(conn, "9991000004", "Below")

        _link(conn, root, active)
        _link(conn, active, deleted)
        _link(conn, deleted, below_deleted)
        conn.execute(
            "UPDATE clients SET client_status='deleted' WHERE id=?",
            (deleted,),
        )

        tree = referral_tree_without_deleted(conn, root_client_id=root)

        assert tree["direct"] == 1
        assert tree["total"] == 1
        assert tree["max_depth"] == 1
        assert tree["root"]["children"][0]["display_name"] == "Active"
        assert tree["root"]["children"][0]["children"] == []


def test_deleted_direct_referral_is_not_counted(tmp_path: Path) -> None:
    db_path = tmp_path / "referral-tree-direct.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        root = _client(conn, "9992000001", "Root")
        deleted = _client(conn, "9992000002", "Deleted")
        _link(conn, root, deleted)
        conn.execute(
            "UPDATE clients SET client_status='deleted' WHERE id=?",
            (deleted,),
        )

        tree = referral_tree_without_deleted(conn, root_client_id=root)

        assert tree["root"]["children"] == []
        assert tree["direct"] == 0
        assert tree["total"] == 0
        assert tree["max_depth"] == 0
