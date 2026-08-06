"""JACKSIDE daily issues: container, releases, rules, participants, legacy fallback."""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.services.daily_414 import (
    DAILY_414_ENTRY_WINDOW_SECONDS,
    DAILY_414_FINAL_TABLE_DELAY_SECONDS,
    DAILY_414_QUESTION_COUNT,
    DAILY_414_TIME_LIMIT_SECONDS,
    final_table_starts_at,
)
from app.services.jackside_copy import (
    DEFAULT_RULES_CONTENT,
    DEFAULT_RULES_TITLE,
    DEFAULT_RULES_VERSION,
    prize_headline,
    result_copy_for_score,
)
from app.services.quiz import load_builder_questions, load_final_questions


ISSUE_STATUSES = (
    "draft",
    "scheduled",
    "lobby",
    "main_live",
    "waiting_final",
    "final_live",
    "closed",
    "cancelled",
    "technical_review",
)
ADMIN_LOCKED_STATUSES = frozenset({"cancelled", "technical_review", "closed"})
CAMPAIGN_CODE_RE = re.compile(r"^jackside_\d{8}(?:_[a-z0-9]+)?$")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="seconds")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return _as_utc(parsed)


def ensure_default_rules(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT * FROM jackside_rules_versions
        WHERE is_active=1
        ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    if row:
        return row
    conn.execute(
        """
        INSERT INTO jackside_rules_versions(version, title, content, is_active)
        VALUES (?, ?, ?, 1)
        """,
        (DEFAULT_RULES_VERSION, DEFAULT_RULES_TITLE, DEFAULT_RULES_CONTENT),
    )
    return conn.execute(
        """
        SELECT * FROM jackside_rules_versions
        WHERE is_active=1 ORDER BY id DESC LIMIT 1
        """
    ).fetchone()


def active_rules(conn: sqlite3.Connection) -> sqlite3.Row:
    return ensure_default_rules(conn)


def publish_rules_version(
    conn: sqlite3.Connection,
    *,
    version: str,
    title: str,
    content: str,
) -> sqlite3.Row:
    clean_version = str(version or "").strip()
    clean_title = str(title or "").strip()
    clean_content = str(content or "").strip()
    if not clean_version or not clean_title or len(clean_content) < 40:
        raise ValueError("invalid_rules_version")
    conn.execute("UPDATE jackside_rules_versions SET is_active=0 WHERE is_active=1")
    try:
        cursor = conn.execute(
            """
            INSERT INTO jackside_rules_versions(version, title, content, is_active)
            VALUES (?, ?, ?, 1)
            """,
            (clean_version, clean_title, clean_content),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("rules_version_exists") from exc
    return conn.execute(
        "SELECT * FROM jackside_rules_versions WHERE id=?",
        (cursor.lastrowid,),
    ).fetchone()


def account_accepted_rules(
    conn: sqlite3.Connection, *, account_id: int, rules_version: str
) -> bool:
    return bool(
        conn.execute(
            """
            SELECT 1 FROM jackside_rules_acceptances
            WHERE account_id=? AND rules_version=?
            """,
            (account_id, rules_version),
        ).fetchone()
    )


def accept_rules(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    rules: sqlite3.Row | dict[str, Any],
    ip_hash: str,
    user_agent: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO jackside_rules_acceptances(
            account_id, rules_version_id, rules_version, ip_hash, user_agent
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            account_id,
            int(rules["id"]),
            str(rules["version"]),
            ip_hash or "unknown",
            (user_agent or "")[:300],
        ),
    )


def missing_jackside_rules(
    conn: sqlite3.Connection, *, account_id: int
) -> sqlite3.Row | None:
    rules = active_rules(conn)
    if account_accepted_rules(
        conn, account_id=account_id, rules_version=str(rules["version"])
    ):
        return None
    return rules


def issue_campaign_code(issue_date_value: date, *, suffix: str = "") -> str:
    base = f"jackside_{issue_date_value.isoformat().replace('-', '')}"
    clean = re.sub(r"[^a-z0-9_]", "", (suffix or "").lower())
    return f"{base}_{clean}" if clean else base


def create_issue(
    conn: sqlite3.Connection,
    *,
    issue_date_value: date,
    starts_at: datetime,
    title: str | None = None,
    admin_id: int | None = None,
    jackcoin_per_correct: int = 5,
    jackcoin_completion_bonus: int = 10,
    jackcoin_perfect_bonus: int = 20,
    final_prize_type: str = "none",
    final_prize_catalog_reward_id: int | None = None,
    final_prize_jackcoin_amount: int = 0,
    final_question_time_seconds: int = 30,
    timezone_name: str = "Europe/Moscow",
) -> sqlite3.Row:
    rules = ensure_default_rules(conn)
    code = issue_campaign_code(issue_date_value)
    existing = conn.execute(
        "SELECT id FROM jackside_issues WHERE issue_date=?",
        (issue_date_value.isoformat(),),
    ).fetchone()
    if existing:
        raise ValueError("issue_date_exists")
    if final_prize_type == "none" and final_prize_catalog_reward_id:
        final_prize_type = "reward_card"
    clean_title = (title or f"JACKSIDE {issue_date_value.isoformat()}").strip()
    starts = _as_utc(starts_at)
    club_tz = ZoneInfo(timezone_name)
    ends = datetime.combine(
        issue_date_value, datetime.max.time().replace(microsecond=0), tzinfo=club_tz
    ).astimezone(timezone.utc)
    if ends <= starts:
        raise ValueError("issue_end_before_start")
    cursor = conn.execute(
        """
        INSERT INTO jackside_issues(
            issue_date, title, status, campaign_code, rules_version_id,
            rules_version, starts_at, ends_at, jackcoin_per_correct,
            jackcoin_completion_bonus, jackcoin_perfect_bonus,
            final_question_time_seconds, final_prize_type,
            final_prize_catalog_reward_id, final_prize_jackcoin_amount,
            created_by_admin_id
        ) VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            issue_date_value.isoformat(),
            clean_title,
            code,
            int(rules["id"]),
            str(rules["version"]),
            _timestamp(starts),
            _timestamp(ends),
            max(0, int(jackcoin_per_correct)),
            max(0, int(jackcoin_completion_bonus)),
            max(0, int(jackcoin_perfect_bonus)),
            min(300, max(5, int(final_question_time_seconds))),
            final_prize_type,
            final_prize_catalog_reward_id,
            max(0, int(final_prize_jackcoin_amount)),
            admin_id,
        ),
    )
    return conn.execute(
        "SELECT * FROM jackside_issues WHERE id=?", (cursor.lastrowid,)
    ).fetchone()


def get_issue(conn: sqlite3.Connection, issue_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM jackside_issues WHERE id=?", (issue_id,)
    ).fetchone()


def get_issue_by_date(
    conn: sqlite3.Connection, issue_date_value: date
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM jackside_issues WHERE issue_date=?",
        (issue_date_value.isoformat(),),
    ).fetchone()


def get_issue_by_campaign(
    conn: sqlite3.Connection, campaign_code: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM jackside_issues WHERE campaign_code=?",
        (campaign_code,),
    ).fetchone()


def list_issues(
    conn: sqlite3.Connection, *, limit: int = 60
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM jackside_issues
            ORDER BY issue_date DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(200, int(limit))),),
        ).fetchall()
    )


def copy_issue(
    conn: sqlite3.Connection,
    *,
    source_issue_id: int,
    issue_date_value: date,
    starts_at: datetime,
    admin_id: int | None = None,
    timezone_name: str = "Europe/Moscow",
) -> sqlite3.Row:
    source = get_issue(conn, source_issue_id)
    if not source:
        raise ValueError("issue_not_found")
    created = create_issue(
        conn,
        issue_date_value=issue_date_value,
        starts_at=starts_at,
        title=f"JACKSIDE {issue_date_value.isoformat()}",
        admin_id=admin_id,
        jackcoin_per_correct=int(source["jackcoin_per_correct"]),
        jackcoin_completion_bonus=int(source["jackcoin_completion_bonus"]),
        jackcoin_perfect_bonus=int(source["jackcoin_perfect_bonus"]),
        final_prize_type=str(source["final_prize_type"]),
        final_prize_catalog_reward_id=source["final_prize_catalog_reward_id"],
        final_prize_jackcoin_amount=int(source["final_prize_jackcoin_amount"]),
        final_question_time_seconds=int(source["final_question_time_seconds"]),
        timezone_name=timezone_name,
    )
    if source["campaign_code"]:
        _copy_campaign_questions(
            conn,
            source_campaign=str(source["campaign_code"]),
            target_campaign=str(created["campaign_code"]),
        )
        main_count, final_count = _question_counts(conn, str(created["campaign_code"]))
        conn.execute(
            """
            UPDATE jackside_issues
            SET main_question_count=?, final_question_count=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (main_count, final_count, created["id"]),
        )
    return get_issue(conn, int(created["id"]))


def _copy_campaign_questions(
    conn: sqlite3.Connection, *, source_campaign: str, target_campaign: str
) -> None:
    section_map: dict[int, int] = {}
    for section in conn.execute(
        """
        SELECT * FROM quiz_sections WHERE campaign_code=? ORDER BY position, id
        """,
        (source_campaign,),
    ).fetchall():
        cursor = conn.execute(
            """
            INSERT INTO quiz_sections(
                campaign_code, title, theme, background_image, position
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                target_campaign,
                section["title"],
                section["theme"],
                section["background_image"],
                section["position"],
            ),
        )
        section_map[int(section["id"])] = int(cursor.lastrowid)
    for question in conn.execute(
        """
        SELECT * FROM quiz_questions
        WHERE campaign_code=? AND IFNULL(is_active,1)=1
        ORDER BY position, id
        """,
        (source_campaign,),
    ).fetchall():
        section_id = (
            section_map.get(int(question["section_id"]))
            if question["section_id"]
            else None
        )
        cursor = conn.execute(
            """
            INSERT INTO quiz_questions(
                campaign_code, code, type, title, visual_type, image_path,
                section_id, placeholder, accepted_text_answers_json, game_round,
                required, points, time_limit_seconds, position, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target_campaign,
                question["code"],
                question["type"],
                question["title"],
                question["visual_type"] or "standard",
                question["image_path"],
                section_id,
                question["placeholder"],
                question["accepted_text_answers_json"] or "[]",
                question["game_round"] or "main",
                question["required"],
                question["points"],
                question["time_limit_seconds"],
                question["position"],
                question["is_active"],
            ),
        )
        new_qid = int(cursor.lastrowid)
        for option in conn.execute(
            """
            SELECT * FROM quiz_options WHERE question_id=? ORDER BY position, id
            """,
            (question["id"],),
        ).fetchall():
            conn.execute(
                """
                INSERT INTO quiz_options(
                    question_id, code, text, is_correct, position
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    new_qid,
                    option["code"],
                    option["text"],
                    option["is_correct"],
                    option["position"],
                ),
            )


def _question_counts(conn: sqlite3.Connection, campaign_code: str) -> tuple[int, int]:
    main_count = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM quiz_questions
            WHERE campaign_code=? AND IFNULL(game_round,'main')='main'
              AND is_active=1
            """,
            (campaign_code,),
        ).fetchone()[0]
    )
    final_count = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM quiz_questions
            WHERE campaign_code=? AND game_round='final'
              AND is_active=1
            """,
            (campaign_code,),
        ).fetchone()[0]
    )
    return main_count, final_count


def refresh_issue_question_counts(conn: sqlite3.Connection, issue_id: int) -> sqlite3.Row:
    issue = get_issue(conn, issue_id)
    if not issue:
        raise ValueError("issue_not_found")
    main_count, final_count = _question_counts(conn, str(issue["campaign_code"]))
    conn.execute(
        """
        UPDATE jackside_issues
        SET main_question_count=?, final_question_count=?,
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (main_count, final_count, issue_id),
    )
    return get_issue(conn, issue_id)


def update_issue_settings(
    conn: sqlite3.Connection,
    *,
    issue_id: int,
    title: str | None = None,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    jackcoin_per_correct: int | None = None,
    jackcoin_completion_bonus: int | None = None,
    jackcoin_perfect_bonus: int | None = None,
    final_prize_type: str | None = None,
    final_prize_catalog_reward_id: int | None = None,
    final_prize_jackcoin_amount: int | None = None,
    final_question_time_seconds: int | None = None,
    status: str | None = None,
    timezone_name: str = "Europe/Moscow",
) -> sqlite3.Row:
    issue = get_issue(conn, issue_id)
    if not issue:
        raise ValueError("issue_not_found")
    if issue["status"] not in {"draft", "scheduled", "technical_review"} and status not in {
        "cancelled",
        "technical_review",
        "closed",
        None,
    }:
        if status and status != issue["status"]:
            raise ValueError("issue_not_editable")
    fields: list[str] = []
    values: list[Any] = []
    mapping = {
        "title": title,
        "jackcoin_per_correct": jackcoin_per_correct,
        "jackcoin_completion_bonus": jackcoin_completion_bonus,
        "jackcoin_perfect_bonus": jackcoin_perfect_bonus,
        "final_prize_type": final_prize_type,
        "final_prize_catalog_reward_id": final_prize_catalog_reward_id,
        "final_prize_jackcoin_amount": final_prize_jackcoin_amount,
        "final_question_time_seconds": final_question_time_seconds,
        "status": status,
    }
    for column, value in mapping.items():
        if value is None:
            continue
        if column == "status" and value not in ISSUE_STATUSES:
            raise ValueError("invalid_issue_status")
        fields.append(f"{column}=?")
        values.append(value)
    if starts_at is not None:
        fields.append("starts_at=?")
        values.append(_timestamp(starts_at))
    if ends_at is not None:
        fields.append("ends_at=?")
        values.append(_timestamp(ends_at))
    if not fields:
        return issue
    fields.append("updated_at=CURRENT_TIMESTAMP")
    values.append(issue_id)
    conn.execute(
        f"UPDATE jackside_issues SET {', '.join(fields)} WHERE id=?",
        values,
    )
    updated = get_issue(conn, issue_id)
    if updated and (starts_at is not None or ends_at is not None):
        sync_campaign_schedule_from_issue(
            conn, updated, timezone_name=timezone_name
        )
    return updated


def validate_issue_for_publish(
    conn: sqlite3.Connection, issue: sqlite3.Row | dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    campaign_code = str(issue["campaign_code"] or "")
    if not campaign_code:
        errors.append("missing_campaign_code")
        return errors
    main_questions = [
        q
        for q in load_builder_questions(conn, campaign_code)
        if str(q.get("game_round") or "main") == "main"
    ]
    final_questions = load_final_questions(conn, campaign_code)
    if len(main_questions) != DAILY_414_QUESTION_COUNT:
        errors.append("main_questions_must_be_ten")
    if len(final_questions) < 1:
        errors.append("final_questions_required")
    for question in main_questions + final_questions:
        qtype = str(question.get("type") or "")
        options = question.get("options") or []
        if qtype in {"single_choice", "multi_choice"}:
            if not options:
                errors.append(f"empty_options:{question.get('id')}")
            if not any(opt.get("correct") for opt in options):
                errors.append(f"missing_correct:{question.get('id')}")
            if any(not str(opt.get("text") or "").strip() for opt in options):
                errors.append(f"blank_option:{question.get('id')}")
        elif qtype == "text":
            accepted = question.get("accepted_text_answers") or []
            if not accepted:
                errors.append(f"missing_text_answers:{question.get('id')}")
    starts = _parse_dt(str(issue["starts_at"]) if issue["starts_at"] else None)
    ends = _parse_dt(str(issue["ends_at"]) if issue["ends_at"] else None)
    if not starts:
        errors.append("invalid_schedule_start")
    if ends and starts and ends <= starts:
        errors.append("invalid_schedule_end")
    prize_type = str(issue["final_prize_type"] or "none")
    if prize_type == "jackcoin" and int(issue["final_prize_jackcoin_amount"] or 0) <= 0:
        errors.append("invalid_jackcoin_prize")
    if prize_type == "reward_card":
        catalog_id = issue["final_prize_catalog_reward_id"]
        if not catalog_id:
            errors.append("missing_card_prize")
        else:
            catalog = conn.execute(
                "SELECT id FROM vault_catalog_rewards WHERE id=? AND is_active=1",
                (catalog_id,),
            ).fetchone()
            if not catalog:
                errors.append("invalid_card_prize")
    if not issue["rules_version"]:
        errors.append("missing_rules_version")
    return sorted(set(errors))


def issue_schedule_local(
    issue: sqlite3.Row | dict[str, Any],
    *,
    timezone_name: str = "Europe/Moscow",
) -> tuple[str, str | None]:
    """Campaign active_from/until must be naive local wall time, not UTC."""
    tz = ZoneInfo(timezone_name)
    starts = _parse_dt(str(issue["starts_at"]) if issue["starts_at"] else None)
    if not starts:
        raise ValueError("missing_starts_at")
    starts_local = starts.astimezone(tz).replace(tzinfo=None)
    ends = _parse_dt(str(issue["ends_at"]) if issue["ends_at"] else None)
    ends_local = ends.astimezone(tz).replace(tzinfo=None) if ends else None
    return (
        starts_local.strftime("%Y-%m-%dT%H:%M:%S"),
        ends_local.strftime("%Y-%m-%dT%H:%M:%S") if ends_local else None,
    )


def sync_campaign_schedule_from_issue(
    conn: sqlite3.Connection,
    issue: sqlite3.Row | dict[str, Any],
    *,
    timezone_name: str = "Europe/Moscow",
) -> None:
    code = issue.get("campaign_code") if isinstance(issue, dict) else issue["campaign_code"]
    if not code:
        return
    try:
        starts_local, ends_local = issue_schedule_local(
            issue, timezone_name=timezone_name
        )
    except ValueError:
        return
    conn.execute(
        """
        UPDATE quiz_campaigns
        SET active_from=?, active_until=COALESCE(?, active_until),
            updated_at=CURRENT_TIMESTAMP
        WHERE code=?
        """,
        (starts_local, ends_local, code),
    )


def effective_campaign_schedule(
    conn: sqlite3.Connection,
    campaign: sqlite3.Row | dict[str, Any],
    *,
    timezone_name: str = "Europe/Moscow",
) -> dict[str, Any]:
    """Return the runtime campaign schedule without mutating quiz_campaigns."""
    payload = dict(campaign)
    if str(payload.get("campaign_type") or "") != "daily_414":
        return payload
    issue = get_issue_by_campaign(conn, str(payload.get("code") or ""))
    if not issue:
        return payload
    starts_local, ends_local = issue_schedule_local(
        issue, timezone_name=timezone_name
    )
    payload["active_from"] = starts_local
    payload["active_until"] = ends_local
    return payload


def ensure_issue_campaign(
    conn: sqlite3.Connection,
    *,
    issue: sqlite3.Row,
    timezone_name: str = "Europe/Moscow",
) -> sqlite3.Row:
    code = str(issue["campaign_code"])
    campaign = conn.execute(
        "SELECT * FROM quiz_campaigns WHERE code=?", (code,)
    ).fetchone()
    if campaign:
        sync_campaign_schedule_from_issue(
            conn, issue, timezone_name=timezone_name
        )
        return conn.execute(
            "SELECT * FROM quiz_campaigns WHERE code=?", (code,)
        ).fetchone()
    starts_local, ends_local = issue_schedule_local(
        issue, timezone_name=timezone_name
    )
    conn.execute(
        """
        INSERT INTO quiz_campaigns(
            code, title, campaign_type, is_active, active_from, active_until,
            quiz_time_limit_seconds, max_attempts, verification_required,
            jackcoin_per_correct, jackcoin_completion_bonus, jackcoin_perfect_bonus,
            final_question_time_seconds, final_prize_type,
            final_prize_catalog_reward_id, final_prize_jackcoin_amount,
            welcome_kicker, welcome_text, start_button_text, current_version
        ) VALUES (
            ?, ?, 'daily_414', 0, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?,
            'JACKSIDE 4:14',
            'Один стол на весь клуб. 10 вопросов. 4:14. Одна попытка.',
            'ЗАНЯТЬ МЕСТО', 1
        )
        """,
        (
            code,
            issue["title"],
            starts_local,
            ends_local,
            DAILY_414_TIME_LIMIT_SECONDS,
            int(issue["jackcoin_per_correct"]),
            int(issue["jackcoin_completion_bonus"]),
            int(issue["jackcoin_perfect_bonus"]),
            int(issue["final_question_time_seconds"]),
            issue["final_prize_type"],
            issue["final_prize_catalog_reward_id"],
            int(issue["final_prize_jackcoin_amount"]),
        ),
    )
    return conn.execute(
        "SELECT * FROM quiz_campaigns WHERE code=?", (code,)
    ).fetchone()


def schedule_issue(
    conn: sqlite3.Connection,
    *,
    issue_id: int,
    timezone_name: str = "Europe/Moscow",
) -> sqlite3.Row:
    issue = refresh_issue_question_counts(conn, issue_id)
    errors = validate_issue_for_publish(conn, issue)
    if errors:
        raise ValueError("issue_invalid:" + ",".join(errors))
    campaign = ensure_issue_campaign(
        conn, issue=issue, timezone_name=timezone_name
    )
    starts_local, ends_local = issue_schedule_local(
        issue, timezone_name=timezone_name
    )
    conn.execute(
        """
        UPDATE quiz_campaigns
        SET title=?, is_active=1, active_from=?, active_until=?,
            jackcoin_per_correct=?, jackcoin_completion_bonus=?,
            jackcoin_perfect_bonus=?, final_question_time_seconds=?,
            final_prize_type=?, final_prize_catalog_reward_id=?,
            final_prize_jackcoin_amount=?, updated_at=CURRENT_TIMESTAMP
        WHERE code=?
        """,
        (
            issue["title"],
            starts_local,
            ends_local,
            int(issue["jackcoin_per_correct"]),
            int(issue["jackcoin_completion_bonus"]),
            int(issue["jackcoin_perfect_bonus"]),
            int(issue["final_question_time_seconds"]),
            issue["final_prize_type"],
            issue["final_prize_catalog_reward_id"],
            int(issue["final_prize_jackcoin_amount"]),
            campaign["code"],
        ),
    )
    conn.execute(
        """
        UPDATE jackside_issues
        SET status='scheduled', published_at=COALESCE(published_at, CURRENT_TIMESTAMP),
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (issue_id,),
    )
    return get_issue(conn, issue_id)


def compute_issue_status(
    issue: sqlite3.Row | dict[str, Any],
    *,
    now: datetime,
    final_table: sqlite3.Row | dict[str, Any] | None = None,
    timezone_name: str = "Europe/Moscow",
) -> str:
    stored = str(issue["status"] or "draft")
    if stored in ADMIN_LOCKED_STATUSES:
        return stored
    if stored == "draft":
        return "draft"
    starts = _parse_dt(str(issue["starts_at"]) if issue["starts_at"] else None)
    ends = _parse_dt(str(issue["ends_at"]) if issue["ends_at"] else None)
    now_utc = _as_utc(now)
    if not starts:
        return stored
    lobby_open = starts - timedelta(hours=2)
    if now_utc < lobby_open:
        return "scheduled"
    if now_utc < starts:
        return "lobby"
    final_start_local = None
    campaign_like = {
        "active_from": datetime.fromisoformat(
            starts.astimezone(ZoneInfo(timezone_name)).replace(tzinfo=None).isoformat(
                timespec="seconds"
            )
        ).isoformat(timespec="seconds")
    }
    # Prefer campaign-linked final delay using naive local active_from semantics.
    try:
        local_start = starts.astimezone(ZoneInfo(timezone_name)).replace(tzinfo=None)
        campaign_like = {"active_from": local_start.isoformat(timespec="seconds")}
        final_start_local = final_table_starts_at(campaign_like)
    except Exception:
        final_start_local = None
    final_start_utc = None
    if final_start_local is not None:
        final_start_utc = final_start_local.replace(
            tzinfo=ZoneInfo(timezone_name)
        ).astimezone(timezone.utc)
    else:
        final_start_utc = starts + timedelta(seconds=DAILY_414_FINAL_TABLE_DELAY_SECONDS)

    if final_table and str(final_table.get("status") if hasattr(final_table, "get") else final_table["status"]) == "completed":
        if ends and now_utc >= ends:
            return "closed"
        return "closed" if stored == "closed" else "closed"

    final_status = None
    if final_table is not None:
        final_status = str(
            final_table["status"] if not hasattr(final_table, "get") else final_table.get("status")
        )
    if now_utc >= final_start_utc:
        if final_status == "live":
            return "final_live"
        if final_status in {"completed", "unavailable"}:
            return "closed"
        return "final_live"
    if now_utc >= starts:
        # Main window still open until final start for late diagnostics;
        # product keeps per-player timer, not a global main deadline.
        if now_utc >= starts + timedelta(seconds=DAILY_414_ENTRY_WINDOW_SECONDS):
            return "waiting_final"
        return "main_live"
    return stored


def sync_issue_runtime_status(
    conn: sqlite3.Connection,
    *,
    issue_id: int,
    now: datetime,
    timezone_name: str = "Europe/Moscow",
) -> sqlite3.Row:
    issue = get_issue(conn, issue_id)
    if not issue:
        raise ValueError("issue_not_found")
    final_table = None
    if issue["campaign_code"]:
        campaign = conn.execute(
            "SELECT current_version FROM quiz_campaigns WHERE code=?",
            (issue["campaign_code"],),
        ).fetchone()
        version = int(campaign["current_version"] or 1) if campaign else 1
        final_table = conn.execute(
            """
            SELECT * FROM daily_414_final_tables
            WHERE campaign_code=? AND campaign_version=?
            """,
            (issue["campaign_code"], version),
        ).fetchone()
    runtime = compute_issue_status(
        issue, now=now, final_table=final_table, timezone_name=timezone_name
    )
    if runtime != issue["status"] and issue["status"] not in ADMIN_LOCKED_STATUSES:
        if issue["status"] != "draft":
            conn.execute(
                """
                UPDATE jackside_issues
                SET status=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (runtime, issue_id),
            )
            issue = get_issue(conn, issue_id)
    return issue


def register_issue_participant(
    conn: sqlite3.Connection,
    *,
    issue_id: int,
    client_id: int,
    account_id: int | None,
) -> bool:
    """Return True if this is the first unique join for the account/client."""
    existing = conn.execute(
        """
        SELECT id FROM jackside_issue_participants
        WHERE issue_id=? AND (
            client_id=? OR (? IS NOT NULL AND account_id=?)
        )
        LIMIT 1
        """,
        (issue_id, client_id, account_id, account_id),
    ).fetchone()
    if existing:
        return False
    try:
        conn.execute(
            """
            INSERT INTO jackside_issue_participants(
                issue_id, client_id, account_id, joined_at
            ) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (issue_id, client_id, account_id),
        )
    except sqlite3.IntegrityError:
        return False
    conn.execute(
        """
        UPDATE jackside_issues
        SET unique_participants=(
            SELECT COUNT(*) FROM jackside_issue_participants WHERE issue_id=?
        ), updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (issue_id, issue_id),
    )
    return True


def unique_participant_count(conn: sqlite3.Connection, *, issue_id: int) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM jackside_issue_participants WHERE issue_id=?",
            (issue_id,),
        ).fetchone()[0]
    )


def legacy_issue_from_campaign(
    conn: sqlite3.Connection,
    campaign: sqlite3.Row | dict[str, Any],
    *,
    now: datetime | None = None,
    timezone_name: str = "Europe/Moscow",
) -> dict[str, Any]:
    """Non-destructive view for classic daily_414 campaigns without jackside_issues."""
    rules = ensure_default_rules(conn)
    starts = campaign["active_from"]
    prize_type = str(campaign["final_prize_type"] or "none")
    if prize_type == "none" and campaign["final_prize_catalog_reward_id"]:
        prize_type = "reward_card"
    card_title = None
    if campaign["final_prize_catalog_reward_id"]:
        card = conn.execute(
            "SELECT title FROM vault_catalog_rewards WHERE id=?",
            (campaign["final_prize_catalog_reward_id"],),
        ).fetchone()
        card_title = card["title"] if card else None
    participants = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT client_id) FROM quiz_submissions
            WHERE campaign_code=? AND IFNULL(main_round_completed,1)=1
            """,
            (campaign["code"],),
        ).fetchone()[0]
    )
    main_count, final_count = _question_counts(conn, str(campaign["code"]))
    synthetic = {
        "id": None,
        "issue_date": (
            datetime.fromisoformat(str(starts)).date().isoformat()
            if starts
            else None
        ),
        "title": campaign["title"],
        "status": "main_live",
        "campaign_code": campaign["code"],
        "rules_version": rules["version"],
        "rules_version_id": rules["id"],
        "starts_at": starts,
        "ends_at": campaign["active_until"],
        "main_question_count": main_count,
        "final_question_count": final_count,
        "jackcoin_per_correct": int(campaign["jackcoin_per_correct"] or 5),
        "jackcoin_completion_bonus": int(campaign["jackcoin_completion_bonus"] or 10),
        "jackcoin_perfect_bonus": int(campaign["jackcoin_perfect_bonus"] or 20),
        "final_prize_type": prize_type,
        "final_prize_catalog_reward_id": campaign["final_prize_catalog_reward_id"],
        "final_prize_jackcoin_amount": int(campaign["final_prize_jackcoin_amount"] or 0),
        "final_question_time_seconds": int(
            campaign["final_question_time_seconds"] or 30
        ),
        "unique_participants": participants,
        "legacy": True,
        "prize_headline": prize_headline(
            prize_type=prize_type,
            jackcoin_amount=int(campaign["final_prize_jackcoin_amount"] or 0),
            card_title=card_title,
        ),
        "base_award_hint": (
            f"+{int(campaign['jackcoin_per_correct'] or 5)} JC за верный ответ, "
            f"+{int(campaign['jackcoin_completion_bonus'] or 10)} JC за завершение"
        ),
    }
    if now is not None:
        synthetic["status"] = compute_issue_status(
            synthetic, now=now, timezone_name=timezone_name
        )
    return attach_audience_counts(conn, synthetic)


def resolve_issue_for_campaign(
    conn: sqlite3.Connection,
    campaign: sqlite3.Row | dict[str, Any],
    *,
    now: datetime | None = None,
    timezone_name: str = "Europe/Moscow",
) -> dict[str, Any]:
    row = get_issue_by_campaign(conn, str(campaign["code"]))
    if not row:
        return legacy_issue_from_campaign(
            conn, campaign, now=now, timezone_name=timezone_name
        )
    issue = dict(row)
    final_table = None
    campaign_version = int(campaign["current_version"] or 1)
    final_table = conn.execute(
        """
        SELECT * FROM daily_414_final_tables
        WHERE campaign_code=? AND campaign_version=?
        """,
        (campaign["code"], campaign_version),
    ).fetchone()
    issue["status"] = compute_issue_status(
        issue,
        now=now or datetime.now(timezone.utc),
        final_table=final_table,
        timezone_name=timezone_name,
    )
    card_title = None
    if issue["final_prize_catalog_reward_id"]:
        card = conn.execute(
            "SELECT title FROM vault_catalog_rewards WHERE id=?",
            (issue["final_prize_catalog_reward_id"],),
        ).fetchone()
        card_title = card["title"] if card else None
    payload = dict(issue)
    payload["legacy"] = False
    payload["prize_headline"] = prize_headline(
        prize_type=str(issue["final_prize_type"]),
        jackcoin_amount=int(issue["final_prize_jackcoin_amount"] or 0),
        card_title=card_title,
    )
    payload["base_award_hint"] = (
        f"+{int(issue['jackcoin_per_correct'])} JC за верный ответ, "
        f"+{int(issue['jackcoin_completion_bonus'])} JC за завершение"
    )
    payload["unique_participants"] = unique_participant_count(
        conn, issue_id=int(issue["id"])
    )
    return attach_audience_counts(conn, payload)


def current_featured_issue(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    timezone_name: str = "Europe/Moscow",
) -> dict[str, Any] | None:
    issues = list_issues(conn, limit=30)
    best: dict[str, Any] | None = None
    best_rank = 99
    for issue in issues:
        if issue["status"] == "draft":
            continue
        issue_view = dict(issue)
        campaign = conn.execute(
            "SELECT * FROM quiz_campaigns WHERE code=?",
            (issue_view["campaign_code"],),
        ).fetchone()
        if not campaign:
            continue
        final_table = conn.execute(
            """
            SELECT * FROM daily_414_final_tables
            WHERE campaign_code=? AND campaign_version=?
            """,
            (issue_view["campaign_code"], int(campaign["current_version"] or 1)),
        ).fetchone()
        issue_view["status"] = compute_issue_status(
            issue_view,
            now=now,
            final_table=final_table,
            timezone_name=timezone_name,
        )
        status = str(issue_view["status"])
        rank = {
            "main_live": 0,
            "lobby": 1,
            "waiting_final": 2,
            "final_live": 3,
            "scheduled": 4,
        }.get(status, 50)
        if rank >= 50:
            continue
        payload = resolve_issue_for_campaign(
            conn, campaign, now=now, timezone_name=timezone_name
        )
        if rank < best_rank:
            best = payload
            best_rank = rank
    if best:
        return best
    # Legacy fallback: first active/upcoming daily_414 campaign
    campaigns = conn.execute(
        """
        SELECT * FROM quiz_campaigns
        WHERE campaign_type='daily_414' AND is_active=1
          AND archived_at IS NULL AND deleted_at IS NULL
        ORDER BY active_from IS NOT NULL, active_from, id
        """
    ).fetchall()
    for campaign in campaigns:
        return legacy_issue_from_campaign(
            conn, campaign, now=now, timezone_name=timezone_name
        )
    return None


ONLINE_WINDOW_MINUTES = 5


def campaign_audience_counts(
    conn: sqlite3.Connection, campaign_code: str
) -> dict[str, int]:
    """Completed = finished main round; online = active attempts in the last N minutes."""
    code = str(campaign_code or "")
    completed = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT client_id) FROM quiz_submissions
            WHERE campaign_code=?
              AND client_id IS NOT NULL
              AND IFNULL(main_round_completed, 1)=1
            """,
            (code,),
        ).fetchone()[0]
    )
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=ONLINE_WINDOW_MINUTES)
    ).isoformat(timespec="milliseconds")
    online = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT client_id) FROM quiz_attempts
            WHERE campaign_code=?
              AND status='in_progress'
              AND client_id IS NOT NULL
              AND last_activity_at >= ?
            """,
            (code, cutoff),
        ).fetchone()[0]
    )
    return {"completed_count": completed, "online_count": online}


def attach_audience_counts(
    conn: sqlite3.Connection, issue: dict[str, Any]
) -> dict[str, Any]:
    code = issue.get("campaign_code")
    if not code:
        issue.setdefault("completed_count", 0)
        issue.setdefault("online_count", 0)
        return issue
    counts = campaign_audience_counts(conn, str(code))
    issue.update(counts)
    return issue


def public_issue_card(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "issue_id": issue.get("id"),
        "issue_date": issue.get("issue_date"),
        "title": issue.get("title"),
        "status": issue.get("status"),
        "starts_at": issue.get("starts_at"),
        "ends_at": issue.get("ends_at"),
        "campaign_code": issue.get("campaign_code"),
        "prize_headline": issue.get("prize_headline"),
        "base_award_hint": issue.get("base_award_hint"),
        "unique_participants": int(issue.get("unique_participants") or 0),
        "completed_count": int(issue.get("completed_count") or 0),
        "online_count": int(issue.get("online_count") or 0),
        "final_question_count": int(issue.get("final_question_count") or 0),
        "main_question_count": int(
            issue.get("main_question_count") or DAILY_414_QUESTION_COUNT
        ),
        "rules_version": issue.get("rules_version"),
        "legacy": bool(issue.get("legacy")),
        "jackcoin_per_correct": int(issue.get("jackcoin_per_correct") or 5),
        "jackcoin_completion_bonus": int(issue.get("jackcoin_completion_bonus") or 10),
        "final_prize_type": issue.get("final_prize_type"),
        "final_prize_jackcoin_amount": int(
            issue.get("final_prize_jackcoin_amount") or 0
        ),
    }


def finish_result_payload(
    *,
    correct_count: int,
    completion_time_ms: int | None,
    place: int | None,
    participant_count: int,
    jackcoin_awarded: int,
    streak_days: int,
    final_status: str | None,
    prize_note: str | None,
) -> dict[str, Any]:
    copy = result_copy_for_score(correct_count)
    return {
        "result_title": copy["title"],
        "result_message": copy["message"],
        "result_copy_code": copy["code"],
        "correct_count": int(correct_count),
        "completion_time_ms": completion_time_ms,
        "place": place,
        "participant_count": int(participant_count),
        "jackcoin_awarded": int(jackcoin_awarded),
        "streak_days": int(streak_days),
        "final_status": final_status,
        "prize_note": prize_note,
    }
