from __future__ import annotations

import json
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.db import connect, transaction
from app.product_shell import _check_csrf, _current_member, _require_master
from app.profile_experience import title_collection_payload
from app.services.jackside_issues import create_issue, ensure_issue_campaign


ECONOMY_DEFAULTS: tuple[tuple[str, str, str, int, int], ...] = (
    ("jackside_completion", "JACKSIDE", "Завершение основной игры", 10, 10),
    ("jackside_correct", "JACKSIDE", "Каждый правильный ответ", 10, 20),
    ("jackside_perfect", "JACKSIDE", "Дополнительно за 10/10", 30, 30),
    ("jackside_final_correct", "JACKSIDE", "Правильный ответ в суперфинале", 50, 40),
    ("jackside_final_win", "JACKSIDE", "Победа в суперфинале", 414, 50),
    ("streak_7", "Серии", "Серия 7 дней · Systematic", 70, 110),
    ("streak_15", "Серии", "Серия 15 дней · EVEN", 200, 120),
    ("streak_30", "Серии", "Серия 30 дней · REGULAR", 500, 130),
    ("streak_100", "Серии", "Серия 100 дней · XATIKO", 2000, 140),
    ("hijack_participation", "HI JACK", "Участие в обычном турнире HI JACK", 50, 210),
    ("ref_jackside_first_l1", "Рефералы JACKSIDE", "Первый JACKSIDE · L1", 150, 310),
    ("ref_jackside_first_l2", "Рефералы JACKSIDE", "Первый JACKSIDE · L2", 50, 320),
    ("ref_jackside_first_l3", "Рефералы JACKSIDE", "Первый JACKSIDE · L3", 20, 330),
    ("ref_jackside_repeat_l1", "Рефералы JACKSIDE", "Повторный JACKSIDE · L1", 15, 340),
    ("ref_jackside_repeat_l2", "Рефералы JACKSIDE", "Повторный JACKSIDE · L2", 5, 350),
    ("ref_jackside_repeat_l3", "Рефералы JACKSIDE", "Повторный JACKSIDE · L3", 2, 360),
    ("ref_hijack_first_l1", "Рефералы HI JACK", "Первый HI JACK · L1", 500, 410),
    ("ref_hijack_first_l2", "Рефералы HI JACK", "Первый HI JACK · L2", 150, 420),
    ("ref_hijack_first_l3", "Рефералы HI JACK", "Первый HI JACK · L3", 50, 430),
    ("ref_hijack_repeat_l1", "Рефералы HI JACK", "Повторный HI JACK · L1", 50, 440),
    ("ref_hijack_repeat_l2", "Рефералы HI JACK", "Повторный HI JACK · L2", 15, 450),
    ("ref_hijack_repeat_l3", "Рефералы HI JACK", "Повторный HI JACK · L3", 5, 460),
)

SOCIAL_LINK_DEFAULTS: tuple[tuple[str, str, str, str, int], ...] = (
    ("telegram", "Telegram-канал", "Новости клуба, турниры и результаты", "telegram", 10),
    ("miniapp", "Hi, Jack Mini App", "Турниры, регистрация и клубные функции", "miniapp", 20),
    ("yandex_maps", "Яндекс Карты", "Адрес клуба, маршрут и отзывы", "maps", 30),
)

PUBLIC_PROFILE_CATEGORY = "participation_stats"
_ALLOWED_TOURNAMENT_TYPES = frozenset({"regular", "final"})


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        str(row[1]) == column for row in conn.execute(f"PRAGMA table_info({table})")
    )


def _amount_sql(key: str, *, entity_type: str | None = None, entity_id_sql: str | None = None) -> str:
    fallback = (
        f"(SELECT amount FROM jackcoin_economy_settings WHERE setting_key='{key}')"
    )
    if entity_type and entity_id_sql:
        return (
            "COALESCE((SELECT amount FROM jackcoin_economy_snapshots "
            f"WHERE entity_type='{entity_type}' AND entity_id={entity_id_sql} "
            f"AND setting_key='{key}'), {fallback}, 0)"
        )
    return f"COALESCE({fallback}, 0)"


def _jackside_referral_trigger_sql(level: int) -> str:
    joins = [
        "referral_qualification_progress r1",
    ]
    if level >= 2:
        joins.append(
            "JOIN referral_qualification_progress r2 ON r2.invited_client_id=r1.referrer_client_id"
        )
    if level >= 3:
        joins.append(
            "JOIN referral_qualification_progress r3 ON r3.invited_client_id=r2.referrer_client_id"
        )
    ref_alias = f"r{level}"
    first_key = f"ref_jackside_first_l{level}"
    repeat_key = f"ref_jackside_repeat_l{level}"
    previous = (
        "EXISTS (SELECT 1 FROM quiz_submissions old_qs "
        "JOIN quiz_campaigns old_qc ON old_qc.code=old_qs.campaign_code "
        "AND old_qc.campaign_type='daily_414' "
        "WHERE old_qs.client_id=NEW.client_id AND old_qs.id<>NEW.id "
        "AND IFNULL(old_qs.main_round_completed,1)=1 AND old_qs.max_correct_count>0)"
    )
    first_amount = _amount_sql(first_key, entity_type="jackside", entity_id_sql="NEW.campaign_code")
    repeat_amount = _amount_sql(repeat_key, entity_type="jackside", entity_id_sql="NEW.campaign_code")
    amount = f"CASE WHEN {previous} THEN {repeat_amount} ELSE {first_amount} END"
    return f"""
        INSERT OR IGNORE INTO jackcoin_ledger(
            client_id, amount, operation_type, source_type, source_id,
            idempotency_key, comment
        )
        SELECT {ref_alias}.referrer_client_id,
               {amount},
               'earn', 'referral_jackside_l{level}', CAST(NEW.id AS TEXT),
               'ref:jackside:' || NEW.id || ':l{level}:' || {ref_alias}.referrer_client_id,
               'Реферальная сеть JACKSIDE · L{level}'
        FROM {' '.join(joins)}
        WHERE r1.invited_client_id=NEW.client_id
          AND ({amount}) > 0;
    """


def _hijack_referral_trigger_sql(level: int) -> str:
    joins = ["referral_qualification_progress r1"]
    if level >= 2:
        joins.append(
            "JOIN referral_qualification_progress r2 ON r2.invited_client_id=r1.referrer_client_id"
        )
    if level >= 3:
        joins.append(
            "JOIN referral_qualification_progress r3 ON r3.invited_client_id=r2.referrer_client_id"
        )
    ref_alias = f"r{level}"
    first_key = f"ref_hijack_first_l{level}"
    repeat_key = f"ref_hijack_repeat_l{level}"
    previous = (
        "EXISTS (SELECT 1 FROM hi_jack_rating_entries old_e "
        "WHERE old_e.client_id=NEW.client_id AND old_e.import_id<>NEW.import_id)"
    )
    entity_id = "CAST(NEW.import_id AS TEXT)"
    first_amount = _amount_sql(first_key, entity_type="hijack", entity_id_sql=entity_id)
    repeat_amount = _amount_sql(repeat_key, entity_type="hijack", entity_id_sql=entity_id)
    amount = f"CASE WHEN {previous} THEN {repeat_amount} ELSE {first_amount} END"
    return f"""
        INSERT OR IGNORE INTO jackcoin_ledger(
            client_id, amount, operation_type, source_type, source_id,
            idempotency_key, comment
        )
        SELECT {ref_alias}.referrer_client_id,
               {amount},
               'earn', 'referral_hijack_l{level}', CAST(NEW.import_id AS TEXT),
               'ref:hijack:' || NEW.import_id || ':' || NEW.client_id || ':l{level}:' || {ref_alias}.referrer_client_id,
               'Реферальная сеть HI JACK · L{level}'
        FROM {' '.join(joins)}
        WHERE r1.invited_client_id=NEW.client_id
          AND ({amount}) > 0;
    """


def ensure_prelaunch_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jackcoin_economy_settings (
            setting_key TEXT PRIMARY KEY,
            section TEXT NOT NULL,
            title TEXT NOT NULL,
            amount INTEGER NOT NULL DEFAULT 0 CHECK(amount >= 0),
            position INTEGER NOT NULL DEFAULT 100,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS jackcoin_economy_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setting_key TEXT NOT NULL,
            old_amount INTEGER NOT NULL,
            new_amount INTEGER NOT NULL,
            admin_id INTEGER REFERENCES admins(id) ON DELETE SET NULL,
            admin_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_jackcoin_economy_audit_created
            ON jackcoin_economy_audit(created_at DESC, id DESC);

        CREATE TABLE IF NOT EXISTS jackcoin_economy_snapshots (
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            setting_key TEXT NOT NULL,
            amount INTEGER NOT NULL CHECK(amount >= 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(entity_type, entity_id, setting_key)
        );
        CREATE INDEX IF NOT EXISTS ix_jackcoin_economy_snapshots_entity
            ON jackcoin_economy_snapshots(entity_type, entity_id);

        CREATE TABLE IF NOT EXISTS club_social_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            link_type TEXT NOT NULL DEFAULT 'link',
            url TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 0,
            show_home INTEGER NOT NULL DEFAULT 1,
            show_profile INTEGER NOT NULL DEFAULT 1,
            position INTEGER NOT NULL DEFAULT 100,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    for key, section, title, amount, position in ECONOMY_DEFAULTS:
        conn.execute(
            """
            INSERT OR IGNORE INTO jackcoin_economy_settings(
                setting_key, section, title, amount, position
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (key, section, title, amount, position),
        )
    for code, title, description, link_type, position in SOCIAL_LINK_DEFAULTS:
        conn.execute(
            """
            INSERT OR IGNORE INTO club_social_links(
                code, title, description, link_type, position
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (code, title, description, link_type, position),
        )

    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hi_jack_rating_imports'"
    ).fetchone() and not _has_column(conn, "hi_jack_rating_imports", "tournament_type"):
        conn.execute(
            "ALTER TABLE hi_jack_rating_imports ADD COLUMN tournament_type TEXT NOT NULL DEFAULT 'regular'"
        )

    conn.executescript(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_prelaunch_jackside_snapshot
        AFTER INSERT ON jackside_issues
        BEGIN
            INSERT OR IGNORE INTO jackcoin_economy_snapshots(
                entity_type, entity_id, setting_key, amount
            )
            SELECT 'jackside', NEW.campaign_code, setting_key, amount
            FROM jackcoin_economy_settings
            WHERE setting_key LIKE 'jackside_%'
               OR setting_key LIKE 'streak_%'
               OR setting_key LIKE 'ref_jackside_%';

            INSERT OR IGNORE INTO jackcoin_economy_snapshots(
                entity_type, entity_id, setting_key, amount
            )
            SELECT 'jackside_day', NEW.issue_date, setting_key, amount
            FROM jackcoin_economy_settings
            WHERE setting_key LIKE 'streak_%';

            UPDATE jackside_issues
            SET jackcoin_per_correct={_amount_sql('jackside_correct', entity_type='jackside', entity_id_sql='NEW.campaign_code')},
                jackcoin_completion_bonus={_amount_sql('jackside_completion', entity_type='jackside', entity_id_sql='NEW.campaign_code')},
                jackcoin_perfect_bonus={_amount_sql('jackside_perfect', entity_type='jackside', entity_id_sql='NEW.campaign_code')},
                updated_at=CURRENT_TIMESTAMP
            WHERE id=NEW.id;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_prelaunch_daily_campaign_defaults
        AFTER INSERT ON quiz_campaigns
        WHEN NEW.campaign_type='daily_414'
        BEGIN
            UPDATE quiz_campaigns
            SET jackcoin_per_correct={_amount_sql('jackside_correct', entity_type='jackside', entity_id_sql='NEW.code')},
                jackcoin_completion_bonus={_amount_sql('jackside_completion', entity_type='jackside', entity_id_sql='NEW.code')},
                jackcoin_perfect_bonus={_amount_sql('jackside_perfect', entity_type='jackside', entity_id_sql='NEW.code')},
                updated_at=CURRENT_TIMESTAMP
            WHERE id=NEW.id;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_prelaunch_final_correct_jc
        AFTER INSERT ON daily_414_final_answers
        WHEN NEW.is_correct=1
        BEGIN
            INSERT OR IGNORE INTO jackcoin_ledger(
                client_id, amount, operation_type, source_type, source_id,
                idempotency_key, comment
            )
            SELECT df.client_id,
                   {_amount_sql('jackside_final_correct', entity_type='jackside', entity_id_sql='ft.campaign_code')},
                   'earn', 'jackside_final_correct', CAST(NEW.id AS TEXT),
                   'jackside:final-correct:' || NEW.id,
                   'JACKSIDE: правильный ответ в суперфинале'
            FROM daily_414_finalists df
            JOIN daily_414_final_tables ft ON ft.id=df.final_table_id
            WHERE df.id=NEW.finalist_id
              AND ({_amount_sql('jackside_final_correct', entity_type='jackside', entity_id_sql='ft.campaign_code')}) > 0;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_prelaunch_final_win_jc
        AFTER UPDATE OF status ON daily_414_finalists
        WHEN NEW.status='winner' AND OLD.status<>'winner'
        BEGIN
            INSERT OR IGNORE INTO jackcoin_ledger(
                client_id, amount, operation_type, source_type, source_id,
                idempotency_key, comment
            )
            SELECT NEW.client_id,
                   {_amount_sql('jackside_final_win', entity_type='jackside', entity_id_sql='ft.campaign_code')},
                   'earn', 'jackside_final_win', CAST(NEW.final_table_id AS TEXT),
                   'jackside:final-win:' || NEW.id,
                   'JACKSIDE: победа в суперфинале'
            FROM daily_414_final_tables ft
            WHERE ft.id=NEW.final_table_id
              AND ({_amount_sql('jackside_final_win', entity_type='jackside', entity_id_sql='ft.campaign_code')}) > 0;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_prelaunch_final_win_insert_jc
        AFTER INSERT ON daily_414_finalists
        WHEN NEW.status='winner'
        BEGIN
            INSERT OR IGNORE INTO jackcoin_ledger(
                client_id, amount, operation_type, source_type, source_id,
                idempotency_key, comment
            )
            SELECT NEW.client_id,
                   {_amount_sql('jackside_final_win', entity_type='jackside', entity_id_sql='ft.campaign_code')},
                   'earn', 'jackside_final_win', CAST(NEW.final_table_id AS TEXT),
                   'jackside:final-win:' || NEW.id,
                   'JACKSIDE: победа в суперфинале'
            FROM daily_414_final_tables ft
            WHERE ft.id=NEW.final_table_id
              AND ({_amount_sql('jackside_final_win', entity_type='jackside', entity_id_sql='ft.campaign_code')}) > 0;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_prelaunch_streak_insert_jc
        AFTER INSERT ON daily_414_progress
        WHEN NEW.current_streak IN (7, 15, 30, 100)
        BEGIN
            INSERT OR IGNORE INTO jackcoin_ledger(
                client_id, amount, operation_type, source_type, source_id,
                idempotency_key, comment
            )
            SELECT NEW.client_id,
                   COALESCE(
                     (SELECT amount FROM jackcoin_economy_snapshots
                      WHERE entity_type='jackside_day' AND entity_id=NEW.last_issue_date
                        AND setting_key='streak_' || NEW.current_streak),
                     (SELECT amount FROM jackcoin_economy_settings
                      WHERE setting_key='streak_' || NEW.current_streak), 0
                   ),
                   'earn', 'jackside_streak', NEW.last_issue_date,
                   'jackside:streak:' || NEW.client_id || ':' || NEW.current_streak || ':' || NEW.last_issue_date,
                   'JACKSIDE: серия ' || NEW.current_streak || ' дней'
            WHERE COALESCE(
                     (SELECT amount FROM jackcoin_economy_snapshots
                      WHERE entity_type='jackside_day' AND entity_id=NEW.last_issue_date
                        AND setting_key='streak_' || NEW.current_streak),
                     (SELECT amount FROM jackcoin_economy_settings
                      WHERE setting_key='streak_' || NEW.current_streak), 0
                  ) > 0;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_prelaunch_streak_update_jc
        AFTER UPDATE OF current_streak ON daily_414_progress
        WHEN NEW.current_streak<>OLD.current_streak
         AND NEW.current_streak IN (7, 15, 30, 100)
        BEGIN
            INSERT OR IGNORE INTO jackcoin_ledger(
                client_id, amount, operation_type, source_type, source_id,
                idempotency_key, comment
            )
            SELECT NEW.client_id,
                   COALESCE(
                     (SELECT amount FROM jackcoin_economy_snapshots
                      WHERE entity_type='jackside_day' AND entity_id=NEW.last_issue_date
                        AND setting_key='streak_' || NEW.current_streak),
                     (SELECT amount FROM jackcoin_economy_settings
                      WHERE setting_key='streak_' || NEW.current_streak), 0
                   ),
                   'earn', 'jackside_streak', NEW.last_issue_date,
                   'jackside:streak:' || NEW.client_id || ':' || NEW.current_streak || ':' || NEW.last_issue_date,
                   'JACKSIDE: серия ' || NEW.current_streak || ' дней'
            WHERE COALESCE(
                     (SELECT amount FROM jackcoin_economy_snapshots
                      WHERE entity_type='jackside_day' AND entity_id=NEW.last_issue_date
                        AND setting_key='streak_' || NEW.current_streak),
                     (SELECT amount FROM jackcoin_economy_settings
                      WHERE setting_key='streak_' || NEW.current_streak), 0
                  ) > 0;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_prelaunch_jackside_referral_jc
        AFTER INSERT ON quiz_submissions
        WHEN IFNULL(NEW.main_round_completed,1)=1
         AND NEW.max_correct_count>0
         AND EXISTS (
             SELECT 1 FROM quiz_campaigns qc
             WHERE qc.code=NEW.campaign_code AND qc.campaign_type='daily_414'
         )
        BEGIN
            {_jackside_referral_trigger_sql(1)}
            {_jackside_referral_trigger_sql(2)}
            {_jackside_referral_trigger_sql(3)}
        END;
        """
    )

    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hi_jack_rating_imports'"
    ).fetchone():
        conn.executescript(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_prelaunch_hijack_import_snapshot
            AFTER INSERT ON hi_jack_rating_imports
            BEGIN
                UPDATE hi_jack_rating_imports
                SET tournament_type=CASE
                    WHEN lower(NEW.tournament_name) LIKE '%финал%'
                      OR lower(NEW.tournament_name) LIKE '%final%'
                    THEN 'final' ELSE 'regular' END
                WHERE id=NEW.id;

                INSERT OR IGNORE INTO jackcoin_economy_snapshots(
                    entity_type, entity_id, setting_key, amount
                )
                SELECT 'hijack', CAST(NEW.id AS TEXT), setting_key, amount
                FROM jackcoin_economy_settings
                WHERE setting_key LIKE 'hijack_%'
                   OR setting_key LIKE 'ref_hijack_%';
            END;

            CREATE TRIGGER IF NOT EXISTS trg_prelaunch_hijack_participation_jc
            AFTER INSERT ON hi_jack_rating_entries
            WHEN NEW.client_id IS NOT NULL
             AND EXISTS (
                 SELECT 1 FROM hi_jack_rating_imports i
                 WHERE i.id=NEW.import_id AND i.tournament_type='regular'
             )
            BEGIN
                INSERT OR IGNORE INTO jackcoin_ledger(
                    client_id, amount, operation_type, source_type, source_id,
                    idempotency_key, comment
                )
                SELECT NEW.client_id,
                       {_amount_sql('hijack_participation', entity_type='hijack', entity_id_sql='CAST(NEW.import_id AS TEXT)')},
                       'earn', 'hijack_participation', CAST(NEW.import_id AS TEXT),
                       'hijack:participation:' || NEW.import_id || ':' || NEW.client_id,
                       'HI JACK: участие в турнире'
                WHERE ({_amount_sql('hijack_participation', entity_type='hijack', entity_id_sql='CAST(NEW.import_id AS TEXT)')}) > 0;
            END;

            CREATE TRIGGER IF NOT EXISTS trg_prelaunch_hijack_referral_jc
            AFTER INSERT ON hi_jack_rating_entries
            WHEN NEW.client_id IS NOT NULL
            BEGIN
                {_hijack_referral_trigger_sql(1)}
                {_hijack_referral_trigger_sql(2)}
                {_hijack_referral_trigger_sql(3)}
            END;
            """
        )


def _economy_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM jackcoin_economy_settings
            ORDER BY position, setting_key
            """
        ).fetchall()
    ]


def _economy_map(conn: sqlite3.Connection) -> dict[str, int]:
    return {row["setting_key"]: int(row["amount"]) for row in _economy_rows(conn)}


def _next_issue_day(conn: sqlite3.Connection, timezone_name: str) -> date:
    tz = ZoneInfo(timezone_name)
    today = datetime.now(tz).date()
    row = conn.execute("SELECT MAX(issue_date) FROM jackside_issues").fetchone()
    if row and row[0]:
        try:
            latest = date.fromisoformat(str(row[0]))
        except ValueError:
            latest = today
        return max(today, latest + timedelta(days=1))
    return today


def _display_name(row: sqlite3.Row | dict[str, Any]) -> str:
    data = dict(row)
    nickname = str(data.get("nickname") or "").strip()
    if nickname:
        return nickname
    username = str(data.get("username") or "").strip()
    if username:
        return f"@{username.lstrip('@')}"
    first_name = str(data.get("first_name") or "").strip()
    if first_name:
        return first_name
    return "Игрок Hi, Jack"


def _rating_categories(conn: sqlite3.Connection, account_id: int) -> dict[str, bool]:
    row = conn.execute(
        """
        SELECT categories_json, granted FROM member_public_rating_consent_state
        WHERE account_id=?
        """,
        (account_id,),
    ).fetchone()
    if not row or not row["granted"]:
        return {}
    try:
        payload = json.loads(str(row["categories_json"] or "{}"))
    except json.JSONDecodeError:
        return {}
    return {str(key): bool(value) for key, value in payload.items()}


def _public_profile_payload(conn: sqlite3.Connection, client_id: int) -> dict[str, Any] | None:
    owner = conn.execute(
        """
        SELECT ma.id AS account_id, ma.is_active AS account_active,
               c.id AS client_id, c.nickname, c.username, c.first_name,
               c.client_status
        FROM member_accounts ma
        JOIN clients c ON c.id=ma.client_id
        WHERE ma.client_id=? AND ma.is_active=1
          AND IFNULL(c.client_status,'')<>'deleted'
        """,
        (client_id,),
    ).fetchone()
    if not owner:
        return None
    categories = _rating_categories(conn, int(owner["account_id"]))
    if not categories:
        return {
            "client_id": client_id,
            "restricted": True,
            "display_name": "Игрок Hi, Jack",
            "categories": {},
        }

    nickname_allowed = bool(categories.get("nickname"))
    avatar_allowed = bool(categories.get("avatar"))
    stats_allowed = bool(categories.get("result") or categories.get(PUBLIC_PROFILE_CATEGORY))
    history_allowed = bool(categories.get(PUBLIC_PROFILE_CATEGORY))
    titles_allowed = bool(categories.get("titles"))
    achievements_allowed = bool(categories.get("achievements"))

    avatar = None
    if avatar_allowed:
        avatar = conn.execute(
            """
            SELECT avatar_path, avatar_kind FROM member_profile_media
            WHERE client_id=? ORDER BY updated_at DESC LIMIT 1
            """,
            (client_id,),
        ).fetchone()

    collection = title_collection_payload(conn, client_id=client_id)
    titles = [
        item
        for item in collection["items"]
        if item["state"] == "active" and item["kind"] == "title"
    ] if titles_allowed else []
    achievements = [
        item
        for item in collection["items"]
        if item["state"] == "active" and item["kind"] == "achievement"
    ] if achievements_allowed else []

    jackside = {
        "games": 0,
        "correct": 0,
        "questions": 0,
        "accuracy": 0,
        "perfect": 0,
        "finals": 0,
        "wins": 0,
        "current_streak": 0,
        "best_streak": 0,
    }
    hijack = {
        "tournaments": 0,
        "rating_points": 0,
        "kills": 0,
    }
    history: list[dict[str, Any]] = []

    if stats_allowed:
        row = conn.execute(
            """
            SELECT COUNT(*) AS games,
                   COALESCE(SUM(correct_count),0) AS correct,
                   COALESCE(SUM(max_correct_count),0) AS questions,
                   COALESCE(SUM(CASE WHEN correct_count=max_correct_count
                                      AND max_correct_count>0 THEN 1 ELSE 0 END),0) AS perfect
            FROM quiz_submissions qs
            JOIN quiz_campaigns qc ON qc.code=qs.campaign_code
            WHERE qs.client_id=? AND qc.campaign_type='daily_414'
              AND IFNULL(qs.main_round_completed,1)=1
              AND qs.max_correct_count>0
            """,
            (client_id,),
        ).fetchone()
        games = int(row["games"] or 0)
        correct = int(row["correct"] or 0)
        questions = int(row["questions"] or 0)
        jackside.update(
            {
                "games": games,
                "correct": correct,
                "questions": questions,
                "accuracy": round((correct * 100 / questions), 1) if questions else 0,
                "perfect": int(row["perfect"] or 0),
            }
        )
        finals = conn.execute(
            """
            SELECT COUNT(DISTINCT final_table_id) AS finals,
                   COUNT(DISTINCT CASE WHEN status='winner' THEN final_table_id END) AS wins
            FROM daily_414_finalists WHERE client_id=?
            """,
            (client_id,),
        ).fetchone()
        jackside["finals"] = int(finals["finals"] or 0)
        jackside["wins"] = int(finals["wins"] or 0)
        progress = conn.execute(
            """
            SELECT current_streak, best_streak FROM daily_414_progress
            WHERE client_id=? ORDER BY updated_at DESC LIMIT 1
            """,
            (client_id,),
        ).fetchone()
        if progress:
            jackside["current_streak"] = int(progress["current_streak"] or 0)
            jackside["best_streak"] = int(progress["best_streak"] or 0)

        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hi_jack_rating_entries'"
        ).fetchone():
            hrow = conn.execute(
                """
                SELECT COUNT(DISTINCT import_id) AS tournaments,
                       COALESCE(SUM(rating_points),0) AS rating_points,
                       COALESCE(SUM(kills),0) AS kills
                FROM hi_jack_rating_entries
                WHERE client_id=?
                """,
                (client_id,),
            ).fetchone()
            points = float(hrow["rating_points"] or 0)
            hijack = {
                "tournaments": int(hrow["tournaments"] or 0),
                "rating_points": int(points) if points.is_integer() else round(points, 1),
                "kills": int(hrow["kills"] or 0),
            }

    if history_allowed:
        for row in conn.execute(
            """
            SELECT qs.id, qs.created_at, qs.correct_count, qs.max_correct_count,
                   qs.completion_time_ms, qs.campaign_code,
                   COALESCE(ji.issue_date, substr(qs.created_at,1,10)) AS event_date,
                   COALESCE(ji.title, qc.title, 'JACKSIDE') AS event_title
            FROM quiz_submissions qs
            JOIN quiz_campaigns qc ON qc.code=qs.campaign_code
            LEFT JOIN jackside_issues ji ON ji.campaign_code=qs.campaign_code
            WHERE qs.client_id=? AND qc.campaign_type='daily_414'
              AND IFNULL(qs.main_round_completed,1)=1
              AND qs.max_correct_count>0
            ORDER BY qs.created_at DESC, qs.id DESC LIMIT 50
            """,
            (client_id,),
        ).fetchall():
            history.append(
                {
                    "kind": "jackside",
                    "date": str(row["event_date"] or ""),
                    "title": str(row["event_title"] or "JACKSIDE"),
                    "result": f"{int(row['correct_count'] or 0)}/{int(row['max_correct_count'] or 0)}",
                    "sort": str(row["created_at"] or row["event_date"] or ""),
                }
            )
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hi_jack_rating_entries'"
        ).fetchone():
            for row in conn.execute(
                """
                SELECT i.id, i.tournament_name, i.tournament_date,
                       e.rating_points, e.kills, i.created_at
                FROM hi_jack_rating_entries e
                JOIN hi_jack_rating_imports i ON i.id=e.import_id
                WHERE e.client_id=?
                ORDER BY i.tournament_date DESC, i.id DESC LIMIT 50
                """,
                (client_id,),
            ).fetchall():
                points = float(row["rating_points"] or 0)
                points_text = str(int(points)) if points.is_integer() else str(round(points, 1))
                history.append(
                    {
                        "kind": "hijack",
                        "date": str(row["tournament_date"] or ""),
                        "title": str(row["tournament_name"] or "HI JACK"),
                        "result": f"{points_text} pts · {int(row['kills'] or 0)} kills",
                        "sort": str(row["tournament_date"] or row["created_at"] or ""),
                    }
                )
        history.sort(key=lambda item: item["sort"], reverse=True)
        history = history[:50]

    selected_title = next((item for item in titles if item.get("selected")), None)
    return {
        "client_id": client_id,
        "restricted": False,
        "display_name": _display_name(owner) if nickname_allowed else "Игрок Hi, Jack",
        "avatar_path": str(avatar["avatar_path"] or "") if avatar else "",
        "avatar_kind": str(avatar["avatar_kind"] or "") if avatar else "",
        "selected_title": selected_title,
        "titles": titles,
        "achievements": achievements,
        "jackside": jackside,
        "hijack": hijack,
        "history": history,
        "categories": categories,
    }


def _safe_external_url(value: str) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    if not (url.startswith("https://") or url.startswith("tg://")):
        raise ValueError("Ссылка должна начинаться с https:// или tg://")
    if len(url) > 1000:
        raise ValueError("Ссылка слишком длинная")
    return url


def _master_redirect(path: str, message: str, *, error: bool = False) -> RedirectResponse:
    key = "error" if error else "ok"
    return RedirectResponse(f"{path}?{urlencode({key: message})}", status_code=303)


def _reconcile_hijack_participation(
    conn: sqlite3.Connection, *, import_id: int, tournament_type: str
) -> int:
    setting = conn.execute(
        """
        SELECT COALESCE(
          (SELECT amount FROM jackcoin_economy_snapshots
           WHERE entity_type='hijack' AND entity_id=? AND setting_key='hijack_participation'),
          (SELECT amount FROM jackcoin_economy_settings WHERE setting_key='hijack_participation'),
          0
        )
        """,
        (str(import_id),),
    ).fetchone()
    regular_amount = int(setting[0] or 0)
    desired = regular_amount if tournament_type == "regular" else 0
    clients = conn.execute(
        """
        SELECT DISTINCT client_id FROM hi_jack_rating_entries
        WHERE import_id=? AND client_id IS NOT NULL
        """,
        (import_id,),
    ).fetchall()
    changed = 0
    for row in clients:
        client_id = int(row["client_id"])
        current = int(
            conn.execute(
                """
                SELECT COALESCE(SUM(amount),0) FROM jackcoin_ledger
                WHERE client_id=? AND source_id=?
                  AND source_type IN ('hijack_participation','hijack_participation_adjustment')
                """,
                (client_id, str(import_id)),
            ).fetchone()[0]
            or 0
        )
        delta = desired - current
        if not delta:
            continue
        conn.execute(
            """
            INSERT INTO jackcoin_ledger(
                client_id, amount, operation_type, source_type, source_id,
                idempotency_key, comment
            ) VALUES (?, ?, 'adjust', 'hijack_participation_adjustment', ?, ?, ?)
            """,
            (
                client_id,
                delta,
                str(import_id),
                f"hijack:participation-adjust:{import_id}:{client_id}:{secrets.token_hex(6)}",
                "HI JACK: корректировка начисления по типу турнира",
            ),
        )
        changed += 1
    return changed


def install_prelaunch_experience(app: FastAPI) -> FastAPI:
    if getattr(app.state, "prelaunch_experience_installed", False):
        return app
    app.state.prelaunch_experience_installed = True
    settings = app.state.settings
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

    with transaction(settings.db_path) as conn:
        ensure_prelaunch_schema(conn)

    @app.get("/master/economy", response_class=HTMLResponse)
    async def master_economy(request: Request, ok: str = "", error: str = ""):
        _require_master(request)
        with connect(settings.db_path) as conn:
            ensure_prelaunch_schema(conn)
            rows = _economy_rows(conn)
            audits = conn.execute(
                """
                SELECT * FROM jackcoin_economy_audit
                ORDER BY created_at DESC, id DESC LIMIT 30
                """
            ).fetchall()
            next_day = _next_issue_day(conn, settings.timezone_name)
        return templates.TemplateResponse(
            request,
            "master_economy.html",
            {
                "request": request,
                "admin_name": request.session.get("admin_name", ""),
                "admin_role": request.session.get("admin_role", ""),
                "csrf_token": request.session.get("csrf", ""),
                "asset_version": "prelaunch-v1",
                "rows": rows,
                "audits": audits,
                "next_issue_date": next_day.isoformat(),
                "next_issue_start": f"{next_day.isoformat()}T18:14",
                "ok": ok,
                "error": error,
            },
        )

    @app.post("/api/master/economy/update")
    async def master_economy_update(request: Request):
        _require_master(request)
        form = await request.form()
        _check_csrf(request, str(form.get("csrf_token") or ""))
        with transaction(settings.db_path) as conn:
            ensure_prelaunch_schema(conn)
            known = {row["setting_key"]: row for row in _economy_rows(conn)}
            changes = 0
            for key, row in known.items():
                if key not in form:
                    continue
                try:
                    amount = int(str(form.get(key) or "0").strip())
                except ValueError:
                    return _master_redirect(
                        "/master/economy", f"Некорректная сумма: {row['title']}", error=True
                    )
                if amount < 0 or amount > 1_000_000:
                    return _master_redirect(
                        "/master/economy", f"Сумма вне диапазона: {row['title']}", error=True
                    )
                old = int(row["amount"] or 0)
                if old == amount:
                    continue
                conn.execute(
                    """
                    UPDATE jackcoin_economy_settings
                    SET amount=?, updated_at=CURRENT_TIMESTAMP
                    WHERE setting_key=?
                    """,
                    (amount, key),
                )
                conn.execute(
                    """
                    INSERT INTO jackcoin_economy_audit(
                        setting_key, old_amount, new_amount, admin_id, admin_name
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        old,
                        amount,
                        int(request.session.get("admin_id") or 0) or None,
                        str(request.session.get("admin_name") or "")[:100],
                    ),
                )
                changes += 1
        return _master_redirect(
            "/master/economy",
            "Экономика JACKCOIN сохранена" if changes else "Изменений нет",
        )

    @app.post("/api/master/jackside-issues/create-default")
    async def master_create_default_issue(
        request: Request,
        issue_date: str = Form(...),
        starts_at: str = Form(...),
        title: str = Form(""),
        prize_jackcoin: int = Form(0),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        try:
            day = date.fromisoformat(issue_date)
            starts_local = datetime.fromisoformat(starts_at)
        except ValueError:
            return _master_redirect(
                "/master/jackside-issues", "Укажите корректные дату и время", error=True
            )
        try:
            with transaction(settings.db_path) as conn:
                ensure_prelaunch_schema(conn)
                economy = _economy_map(conn)
                starts_utc = starts_local.replace(
                    tzinfo=ZoneInfo(settings.timezone_name)
                ).astimezone(timezone.utc)
                issue = create_issue(
                    conn,
                    issue_date_value=day,
                    starts_at=starts_utc,
                    title=title or None,
                    admin_id=int(request.session.get("admin_id") or 0) or None,
                    jackcoin_per_correct=economy["jackside_correct"],
                    jackcoin_completion_bonus=economy["jackside_completion"],
                    jackcoin_perfect_bonus=economy["jackside_perfect"],
                    final_prize_type="jackcoin" if int(prize_jackcoin or 0) > 0 else "none",
                    final_prize_jackcoin_amount=max(0, int(prize_jackcoin or 0)),
                    timezone_name=settings.timezone_name,
                )
                ensure_issue_campaign(
                    conn, issue=issue, timezone_name=settings.timezone_name
                )
        except ValueError as exc:
            message = "На эту дату выпуск уже существует" if str(exc) == "issue_date_exists" else str(exc)
            return _master_redirect("/master/jackside-issues", message, error=True)
        return _master_redirect(
            "/master/jackside-issues",
            f"Выпуск {day.strftime('%d.%m.%Y')} создан с актуальной экономикой JC",
        )

    @app.post("/api/master/economy/hijack/{import_id:int}/type")
    async def master_hijack_type(
        request: Request,
        import_id: int,
        tournament_type: str = Form(...),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        clean = str(tournament_type or "").strip().lower()
        if clean not in _ALLOWED_TOURNAMENT_TYPES:
            return _master_redirect("/master/economy", "Неизвестный тип турнира", error=True)
        with transaction(settings.db_path) as conn:
            ensure_prelaunch_schema(conn)
            found = conn.execute(
                "SELECT id FROM hi_jack_rating_imports WHERE id=?", (import_id,)
            ).fetchone()
            if not found:
                return _master_redirect("/master/economy", "Турнир не найден", error=True)
            conn.execute(
                "UPDATE hi_jack_rating_imports SET tournament_type=? WHERE id=?",
                (clean, import_id),
            )
            changed = _reconcile_hijack_participation(
                conn, import_id=import_id, tournament_type=clean
            )
        label = "обычный" if clean == "regular" else "финал"
        return _master_redirect(
            "/master/economy",
            f"Тип турнира изменён: {label}. Скорректировано балансов: {changed}",
        )

    @app.get("/master/club-links", response_class=HTMLResponse)
    async def master_club_links(request: Request, ok: str = "", error: str = ""):
        _require_master(request)
        with connect(settings.db_path) as conn:
            ensure_prelaunch_schema(conn)
            links = conn.execute(
                "SELECT * FROM club_social_links ORDER BY position, id"
            ).fetchall()
        return templates.TemplateResponse(
            request,
            "master_club_links.html",
            {
                "request": request,
                "admin_name": request.session.get("admin_name", ""),
                "admin_role": request.session.get("admin_role", ""),
                "csrf_token": request.session.get("csrf", ""),
                "asset_version": "prelaunch-v1",
                "links": links,
                "ok": ok,
                "error": error,
            },
        )

    @app.post("/api/master/club-links/{link_id:int}/update")
    async def master_club_link_update(
        request: Request,
        link_id: int,
        title: str = Form(...),
        description: str = Form(""),
        url: str = Form(""),
        is_active: bool = Form(False),
        show_home: bool = Form(False),
        show_profile: bool = Form(False),
        position: int = Form(100),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        try:
            clean_url = _safe_external_url(url)
        except ValueError as exc:
            return _master_redirect("/master/club-links", str(exc), error=True)
        clean_title = " ".join(str(title or "").split())[:100]
        if not clean_title:
            return _master_redirect("/master/club-links", "Укажите название", error=True)
        active = bool(is_active and clean_url)
        with transaction(settings.db_path) as conn:
            ensure_prelaunch_schema(conn)
            conn.execute(
                """
                UPDATE club_social_links
                SET title=?, description=?, url=?, is_active=?, show_home=?,
                    show_profile=?, position=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    clean_title,
                    " ".join(str(description or "").split())[:300],
                    clean_url,
                    int(active),
                    int(show_home),
                    int(show_profile),
                    max(0, min(9999, int(position))),
                    link_id,
                ),
            )
        return _master_redirect("/master/club-links", "Ссылка сохранена")

    @app.post("/api/master/club-links/create")
    async def master_club_link_create(
        request: Request,
        code: str = Form(...),
        title: str = Form(...),
        description: str = Form(""),
        url: str = Form(""),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        clean_code = re.sub(r"[^a-z0-9_]+", "_", str(code or "").lower()).strip("_")[:50]
        clean_title = " ".join(str(title or "").split())[:100]
        if not clean_code or not clean_title:
            return _master_redirect("/master/club-links", "Укажите код и название", error=True)
        try:
            clean_url = _safe_external_url(url)
        except ValueError as exc:
            return _master_redirect("/master/club-links", str(exc), error=True)
        try:
            with transaction(settings.db_path) as conn:
                ensure_prelaunch_schema(conn)
                conn.execute(
                    """
                    INSERT INTO club_social_links(
                        code,title,description,link_type,url,is_active,position
                    ) VALUES (?,?,?,'link',?,?,100)
                    """,
                    (clean_code, clean_title, " ".join(str(description or "").split())[:300], clean_url, int(bool(clean_url))),
                )
        except sqlite3.IntegrityError:
            return _master_redirect("/master/club-links", "Такой код уже существует", error=True)
        return _master_redirect("/master/club-links", "Ссылка добавлена")

    @app.get("/api/account/club-links")
    async def account_club_links(request: Request):
        _current_member(request, required=True)
        with connect(settings.db_path) as conn:
            ensure_prelaunch_schema(conn)
            rows = conn.execute(
                """
                SELECT code,title,description,link_type,url,show_home,show_profile,position
                FROM club_social_links
                WHERE is_active=1 AND url<>''
                ORDER BY position,id
                """
            ).fetchall()
        return JSONResponse({"links": [dict(row) for row in rows]})

    @app.get("/account/links", response_class=HTMLResponse)
    async def member_club_links(request: Request):
        member = _current_member(request, required=True)
        with connect(settings.db_path) as conn:
            ensure_prelaunch_schema(conn)
            links = conn.execute(
                """
                SELECT * FROM club_social_links
                WHERE is_active=1 AND url<>'' ORDER BY position,id
                """
            ).fetchall()
        return templates.TemplateResponse(
            request,
            "member_club_links.html",
            {
                "request": request,
                "member": member,
                "current_tab": "profile",
                "links": links,
                "asset_version": "prelaunch-v1",
            },
        )

    @app.get("/api/account/player-directory")
    async def account_player_directory(request: Request):
        _current_member(request, required=True)
        result: list[dict[str, Any]] = []
        with connect(settings.db_path) as conn:
            rows = conn.execute(
                """
                SELECT ma.id AS account_id,c.id AS client_id,c.nickname,c.username,c.first_name
                FROM member_accounts ma JOIN clients c ON c.id=ma.client_id
                WHERE ma.is_active=1 AND IFNULL(c.client_status,'')<>'deleted'
                ORDER BY c.id
                """
            ).fetchall()
            for row in rows:
                categories = _rating_categories(conn, int(row["account_id"]))
                if not categories.get("nickname"):
                    continue
                nickname = str(row["nickname"] or "").strip()
                if not nickname:
                    continue
                result.append({"client_id": int(row["client_id"]), "display_name": nickname})
        return JSONResponse({"players": result})

    @app.get("/players", response_class=HTMLResponse)
    async def players_directory(request: Request):
        member = _current_member(request, required=True)
        players: list[dict[str, Any]] = []
        with connect(settings.db_path) as conn:
            rows = conn.execute(
                """
                SELECT ma.id AS account_id,c.id AS client_id,c.nickname
                FROM member_accounts ma JOIN clients c ON c.id=ma.client_id
                WHERE ma.is_active=1 AND IFNULL(c.client_status,'')<>'deleted'
                ORDER BY lower(IFNULL(c.nickname,'')), c.id
                """
            ).fetchall()
            for row in rows:
                categories = _rating_categories(conn, int(row["account_id"]))
                if categories.get("nickname") and str(row["nickname"] or "").strip():
                    players.append(
                        {"client_id": int(row["client_id"]), "display_name": str(row["nickname"]).strip()}
                    )
        return templates.TemplateResponse(
            request,
            "players_directory.html",
            {
                "request": request,
                "member": member,
                "current_tab": "rating",
                "players": players,
                "asset_version": "prelaunch-v1",
            },
        )

    @app.get("/players/{client_id:int}", response_class=HTMLResponse)
    async def player_public_profile(request: Request, client_id: int):
        viewer = _current_member(request, required=True)
        if int(viewer["client_id"]) == int(client_id):
            return RedirectResponse("/account?tab=profile", status_code=303)
        with connect(settings.db_path) as conn:
            payload = _public_profile_payload(conn, client_id)
        if not payload:
            return HTMLResponse("Профиль не найден", status_code=404)
        return templates.TemplateResponse(
            request,
            "public_player_profile.html",
            {
                "request": request,
                "member": viewer,
                "current_tab": "rating",
                "profile": payload,
                "asset_version": "prelaunch-v1",
            },
        )

    return app


__all__ = [
    "ECONOMY_DEFAULTS",
    "ensure_prelaunch_schema",
    "install_prelaunch_experience",
]
