import json

from app.db import init_db, transaction
from app.legal_registration import ensure_legal_registration_schema
from app.prelaunch_profile_sharing import (
    PROFILE_SHARING_CATEGORY,
    _rating_categories_with_registered_profile,
    ensure_profile_sharing_schema,
)


def test_registered_profile_sharing_is_separate_and_opt_in(tmp_path) -> None:
    db_path = tmp_path / "profile-sharing.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_legal_registration_schema(conn)
        ensure_profile_sharing_schema(conn)
        client_id = int(
            conn.execute(
                "INSERT INTO clients(nickname,source) VALUES ('VisibleNick','test')"
            ).lastrowid
        )
        account_id = int(
            conn.execute(
                """
                INSERT INTO member_accounts(
                    client_id,email,email_normalized,password_hash,email_verified_at
                ) VALUES (?,?,?,?,CURRENT_TIMESTAMP)
                """,
                (client_id, "player@example.com", "player@example.com", "test-hash"),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO member_public_rating_consent_state(
                account_id,document_version,categories_json,granted
            ) VALUES (?,?,?,1)
            """,
            (account_id, "2026-08-11", json.dumps({"nickname": True})),
        )

        before = _rating_categories_with_registered_profile(conn, account_id)
        assert before == {"nickname": True}
        assert PROFILE_SHARING_CATEGORY not in before

        conn.execute(
            """
            INSERT INTO member_profile_sharing(account_id,share_game_profile)
            VALUES (?,1)
            """,
            (account_id,),
        )
        after = _rating_categories_with_registered_profile(conn, account_id)
        assert after["nickname"] is True
        assert after[PROFILE_SHARING_CATEGORY] is True


def test_profile_sharing_defaults_to_private(tmp_path) -> None:
    db_path = tmp_path / "profile-sharing-private.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        ensure_legal_registration_schema(conn)
        ensure_profile_sharing_schema(conn)
        client_id = int(
            conn.execute(
                "INSERT INTO clients(nickname,source) VALUES ('PrivateNick','test')"
            ).lastrowid
        )
        account_id = int(
            conn.execute(
                """
                INSERT INTO member_accounts(
                    client_id,email,email_normalized,password_hash,email_verified_at
                ) VALUES (?,?,?,?,CURRENT_TIMESTAMP)
                """,
                (client_id, "private@example.com", "private@example.com", "test-hash"),
            ).lastrowid
        )
        assert _rating_categories_with_registered_profile(conn, account_id) == {}
        row = conn.execute(
            "SELECT share_game_profile FROM member_profile_sharing WHERE account_id=?",
            (account_id,),
        ).fetchone()
        assert row is None
