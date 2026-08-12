from pathlib import Path

import pytest

from app.db import init_db, transaction
from app.referral_registration_integrity import (
    PENDING_REFERRAL_KEY,
    bind_referral,
    manual_link_referral,
)
from app.services.jackside_engagement import ensure_jackside_referral_code


ROOT = Path(__file__).resolve().parents[1]


def _client(conn, name: str, *, status: str = "existing") -> int:
    return int(
        conn.execute(
            "INSERT INTO clients(first_name,client_status,source) VALUES (?,?, 'test')",
            (name, status),
        ).lastrowid
    )


def test_pending_referral_can_bind_after_registration_without_live_issue(tmp_path) -> None:
    db_path = tmp_path / "pending-referral.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        referrer = _client(conn, "Owner")
        invited = _client(conn, "New member")
        code = ensure_jackside_referral_code(conn, referrer)

        result = bind_referral(
            conn,
            invited_client_id=invited,
            referral_code=str(code["code"]),
        )

        assert result["status"] == "fixed"
        row = conn.execute(
            "SELECT referrer_client_id,source_campaign_code FROM referral_qualification_progress WHERE invited_client_id=?",
            (invited,),
        ).fetchone()
        assert int(row["referrer_client_id"]) == referrer
        assert str(row["source_campaign_code"]) == "jackside"


def test_master_can_repair_lost_registration_once(tmp_path) -> None:
    db_path = tmp_path / "manual-referral.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        referrer = _client(conn, "Owner")
        invited = _client(conn, "Lost referral")

        result = manual_link_referral(
            conn,
            referrer_client_id=referrer,
            invited_client_id=invited,
        )
        repeated = manual_link_referral(
            conn,
            referrer_client_id=referrer,
            invited_client_id=invited,
        )

        assert result["status"] == "fixed"
        assert repeated["status"] == "already_fixed"
        assert int(
            conn.execute(
                "SELECT COUNT(*) FROM referral_qualification_progress WHERE invited_client_id=?",
                (invited,),
            ).fetchone()[0]
        ) == 1


def test_master_repair_never_overwrites_another_referrer(tmp_path) -> None:
    db_path = tmp_path / "locked-referral.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        first = _client(conn, "First")
        second = _client(conn, "Second")
        invited = _client(conn, "Invited")
        manual_link_referral(
            conn,
            referrer_client_id=first,
            invited_client_id=invited,
        )

        with pytest.raises(ValueError, match="другой реферер"):
            manual_link_referral(
                conn,
                referrer_client_id=second,
                invited_client_id=invited,
            )

        owner = conn.execute(
            "SELECT referrer_client_id FROM referral_qualification_progress WHERE invited_client_id=?",
            (invited,),
        ).fetchone()[0]
        assert int(owner) == first


def test_deleted_accounts_cannot_be_manually_linked(tmp_path) -> None:
    db_path = tmp_path / "deleted-referral.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        referrer = _client(conn, "Owner")
        deleted = _client(conn, "Deleted", status="deleted")
        with pytest.raises(ValueError, match="приглашённого"):
            manual_link_referral(
                conn,
                referrer_client_id=referrer,
                invited_client_id=deleted,
            )


def test_referral_entry_keeps_code_in_signed_session_until_member_exists() -> None:
    source = (ROOT / "app/referral_registration_integrity.py").read_text(encoding="utf-8")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    base = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")

    assert 'request.session[PENDING_REFERRAL_KEY] = clean_code' in source
    assert "pending_referral_binding_middleware" in source
    assert "_current_member(request, required=False)" in source
    assert 'request.session.pop(PENDING_REFERRAL_KEY, None)' in source
    assert PENDING_REFERRAL_KEY == "pending_jackside_referral_code"
    assert "install_referral_registration_integrity" in main
    assert 'href="/master/referrals"' in base
