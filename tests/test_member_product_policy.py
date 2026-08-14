from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import app.referral_status_policy as referral_policy


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_copy_does_not_inherit_old_prize() -> None:
    source = (ROOT / "app/legacy_jackside_copy.py").read_text(encoding="utf-8")
    assert 'final_prize_type=str(source["final_prize_type"]' not in source
    assert 'final_prize_jackcoin_amount=int(source["final_prize_jackcoin_amount"]' not in source
    assert 'final_prize_catalog_reward_id=source["final_prize_catalog_reward_id"]' not in source


def test_jackside_copy_and_lobby_match_current_rules() -> None:
    template = (ROOT / "app/templates/quiz.html").read_text(encoding="utf-8")
    assert "414 JACKCOIN" in template
    assert "Начисление победителю сразу после завершения финала" in template
    assert template.count("Вернуться в приложение") >= 2
    assert "Отбор открыт первые 5 минут" not in template
    assert "Топ-10: сначала точность, затем скорость" not in template
    assert "Игра проходит каждый день в 18:14" not in template
    assert "Старт — по расписанию текущего выпуска" in template


def test_member_polish_is_restrained_and_old_referral_bonus_ui_is_hidden() -> None:
    css = (ROOT / "app/static/css/prelaunch-ui-hotfix.css").read_text(
        encoding="utf-8"
    )
    assert ".ia-referral-economy{display:none!important}" in css
    assert "--app-accent:#0b88b2" in css
    assert "--app-ocean:#095c57" in css
    assert "--app-gold:#b49a5c" in css
    assert "backdrop-filter:blur(15px)" in css
    assert "linear-gradient(135deg,#095c57 0%,#075867 48%,#005b7d 100%)" in css
    assert "#3ac3b0" not in css
    assert "#2aaa97" not in css


def test_referral_status_policy_is_bound_before_main_handlers() -> None:
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    apply_pos = source.index("apply_referral_status_policy()")
    main_impl_pos = source.index("from app.main_impl import *")
    assert apply_pos < main_impl_pos


def test_three_day_referral_status_does_not_create_material_reward(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE referral_qualification_progress(
            id INTEGER PRIMARY KEY,
            referrer_client_id INTEGER NOT NULL,
            invited_client_id INTEGER NOT NULL UNIQUE,
            distinct_completed_days INTEGER NOT NULL DEFAULT 0,
            first_completed_date TEXT,
            second_completed_date TEXT,
            qualified_date TEXT,
            qualified_at TEXT,
            referrer_reward_id INTEGER,
            invited_reward_id INTEGER,
            updated_at TEXT
        );
        INSERT INTO referral_qualification_progress(
            id,referrer_client_id,invited_client_id
        ) VALUES (1,10,20);
        """
    )

    monkeypatch.setattr(
        referral_policy.engagement,
        "_completed_issue_dates",
        lambda *_args, **_kwargs: [
            date(2026, 8, 11),
            date(2026, 8, 12),
            date(2026, 8, 13),
        ],
    )
    notifications: list[dict] = []
    monkeypatch.setattr(
        referral_policy.engagement,
        "_notify",
        lambda _conn, **kwargs: notifications.append(kwargs),
    )
    monkeypatch.setattr(
        referral_policy.engagement,
        "refresh_member_engagement",
        lambda *_args, **_kwargs: None,
    )

    row = referral_policy.update_referral_activity_status(
        conn,
        invited_client_id=20,
        submission_id=777,
        timezone_name="Europe/Moscow",
    )
    assert row is not None
    assert row["distinct_completed_days"] == 3
    assert row["qualified_date"] == "2026-08-13"
    assert row["qualified_at"] is not None
    assert row["referrer_reward_id"] is None
    assert row["invited_reward_id"] is None
    assert len(notifications) == 2
    conn.close()
