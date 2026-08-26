from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import sqlite3

from app import jackside_multi_issue as multi_issue
from app.services import daily_414 as daily_service
from app.services import jackside_copy as copy_service
from app.services import jackside_issues as issue_service
from app.services.daily_414 import DAILY_414_FINAL_TABLE_DELAY_SECONDS
from app.services.quiz import load_builder_questions


_ORIGINAL_EFFECTIVE_CAMPAIGN_SCHEDULE = issue_service.effective_campaign_schedule
_ORIGINAL_RESCHEDULE_FUTURE_ISSUE = multi_issue.reschedule_future_issue
_ORIGINAL_CREATE_ISSUE_MULTI = multi_issue.create_issue_multi
_ORIGINAL_VALIDATE_ISSUE = issue_service.validate_issue_for_publish
_ORIGINAL_ENSURE_DEFAULT_RULES = issue_service.ensure_default_rules
_ORIGINAL_RESULT_COPY = copy_service.result_copy_for_score

FLEX_RULES_VERSION = copy_service.DEFAULT_RULES_VERSION
FLEX_RULES_CONTENT = copy_service.DEFAULT_RULES_CONTENT

_LEGACY_BUILTIN_RULES = {
    "1.1": "в финал проходят до 10 лучших по правильным ответам, затем по зачётному времени;",
    "1.2": "в финал проходят до 10 лучших по правильным ответам, затем по зачётному времени;",
    "1.3": "количество правильных ответов и скорость прохождения основной части на допуск в финал не влияют;",
}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def ensure_default_rules_flexible(conn: sqlite3.Connection) -> sqlite3.Row:
    """Keep one active JACKSIDE rules source and migrate only known built-ins."""
    current = _ORIGINAL_ENSURE_DEFAULT_RULES(conn)
    current_version = str(current["version"] or "")
    if current_version == FLEX_RULES_VERSION:
        return current

    legacy_marker = _LEGACY_BUILTIN_RULES.get(current_version)
    content = str(current["content"] or "")
    is_known_builtin = bool(
        legacy_marker
        and str(current["title"] or "") == copy_service.DEFAULT_RULES_TITLE
        and legacy_marker in content
    )
    if not is_known_builtin:
        return current

    existing = conn.execute(
        "SELECT * FROM jackside_rules_versions WHERE version=? ORDER BY id DESC LIMIT 1",
        (FLEX_RULES_VERSION,),
    ).fetchone()
    conn.execute("UPDATE jackside_rules_versions SET is_active=0 WHERE is_active=1")
    if existing:
        rules_id = int(existing["id"])
        conn.execute(
            """
            UPDATE jackside_rules_versions
            SET title=?, content=?, is_active=1
            WHERE id=?
            """,
            (
                copy_service.DEFAULT_RULES_TITLE,
                FLEX_RULES_CONTENT,
                rules_id,
            ),
        )
    else:
        rules_id = int(
            conn.execute(
                """
                INSERT INTO jackside_rules_versions(version, title, content, is_active)
                VALUES (?, ?, ?, 1)
                """,
                (
                    FLEX_RULES_VERSION,
                    copy_service.DEFAULT_RULES_TITLE,
                    FLEX_RULES_CONTENT,
                ),
            ).lastrowid
        )

    conn.execute(
        """
        UPDATE jackside_issues
        SET rules_version_id=?, rules_version=?, updated_at=CURRENT_TIMESTAMP
        WHERE rules_version=?
          AND status NOT IN ('closed', 'cancelled')
        """,
        (rules_id, FLEX_RULES_VERSION, current_version),
    )
    return conn.execute(
        "SELECT * FROM jackside_rules_versions WHERE id=?",
        (rules_id,),
    ).fetchone()


def validate_issue_for_publish_flexible(
    conn: sqlite3.Connection,
    issue: sqlite3.Row | dict[str, Any],
) -> list[str]:
    """JACKSIDE allows any positive number of main-round questions."""
    errors = [
        error
        for error in _ORIGINAL_VALIDATE_ISSUE(conn, issue)
        if error != "main_questions_must_be_ten"
    ]
    campaign_code = str(issue["campaign_code"] or "")
    if campaign_code:
        main_questions = [
            question
            for question in load_builder_questions(conn, campaign_code)
            if str(question.get("game_round") or "main") == "main"
        ]
        if not main_questions:
            errors.append("main_questions_required")
    return sorted(set(errors))


def validate_daily_questions_flexible(
    questions: list[dict[str, Any]],
    campaign_code: str,
) -> None:
    if not questions:
        raise ValueError("daily_414_requires_questions")
    if any(str(question.get("campaign")) != campaign_code for question in questions):
        raise ValueError("daily_414_requires_own_questions")


def public_daily_questions_flexible(
    questions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    total = max(1, len(questions))
    for index, question in enumerate(questions):
        item = dict(question)
        ratio = (index + 1) / total
        if ratio <= 0.2:
            stage = "preflop"
        elif ratio <= 0.5:
            stage = "flop"
        elif ratio <= 0.8:
            stage = "turn"
        else:
            stage = "river"
        item["game_stage"] = stage
        item["river_reveal"] = index == total - 1
        result.append(item)
    return result


def award_daily_jackcoin_flexible(
    conn: sqlite3.Connection,
    *,
    client_id: int,
    submission_id: int,
    issue_day: date,
    correct_count: int,
    max_correct_count: int,
    jackcoin_per_correct: int = daily_service.JACKCOIN_PER_CORRECT,
    jackcoin_completion_bonus: int = daily_service.JACKCOIN_COMPLETION_BONUS,
    jackcoin_perfect_bonus: int = daily_service.JACKCOIN_PERFECT_BONUS,
) -> dict[str, int]:
    progress = conn.execute(
        "SELECT * FROM daily_414_progress WHERE client_id=?",
        (client_id,),
    ).fetchone()
    previous_date = (
        date.fromisoformat(str(progress["last_issue_date"]))
        if progress and progress["last_issue_date"]
        else None
    )
    previous_streak = int(progress["current_streak"]) if progress else 0
    streak = daily_service._next_streak(
        previous_date=previous_date,
        previous_streak=previous_streak,
        current_date=issue_day,
    )
    best_streak = max(streak, int(progress["best_streak"]) if progress else 0)
    streak_bonus = (
        0
        if daily_service._launch_economy_enabled(conn)
        else daily_service.JACKCOIN_STREAK_BONUSES.get(streak, 0)
    )
    per_correct = max(0, int(jackcoin_per_correct))
    completion_amount = max(0, int(jackcoin_completion_bonus))
    perfect_bonus = max(0, int(jackcoin_perfect_bonus))
    answers_correct = max(0, int(correct_count))
    question_total = max(0, int(max_correct_count))
    answer_amount = answers_correct * per_correct
    perfect_amount = (
        perfect_bonus
        if question_total > 0 and answers_correct == question_total
        else 0
    )
    total = answer_amount + completion_amount + perfect_amount + streak_bonus

    conn.execute(
        """
        INSERT INTO daily_414_progress(
            client_id, current_streak, best_streak, last_issue_date, updated_at
        ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(client_id) DO UPDATE SET
            current_streak=excluded.current_streak,
            best_streak=MAX(daily_414_progress.best_streak, excluded.best_streak),
            last_issue_date=excluded.last_issue_date,
            updated_at=CURRENT_TIMESTAMP
        """,
        (client_id, streak, best_streak, issue_day.isoformat()),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO jackcoin_ledger(
            client_id, amount, operation_type, source_type, source_id,
            idempotency_key, comment
        ) VALUES (?, ?, 'earn', 'daily_414', ?, ?, ?)
        """,
        (
            client_id,
            total,
            str(submission_id),
            f"daily_414:submission:{submission_id}",
            (
                f"4:14: {answers_correct} правильных × {per_correct} JC"
                f" + {completion_amount} JC за завершение"
                f"{f' + {perfect_amount} JC за идеальный результат' if perfect_amount else ''}"
                f"{f' + {streak_bonus} JC за серию {streak} дней' if streak_bonus else ''}"
            ),
        ),
    )
    return {
        "total": total,
        "answers": answer_amount,
        "completion": completion_amount,
        "perfect": perfect_amount,
        "streak_bonus": streak_bonus,
        "streak_days": streak,
        "best_streak": best_streak,
    }


def result_copy_for_score_flexible(
    correct_count: int,
    *,
    final_eligible: bool = True,
    max_correct_count: int | None = None,
) -> dict[str, str]:
    """Backward-compatible name for the canonical JACKSIDE result copy."""
    return _ORIGINAL_RESULT_COPY(
        correct_count,
        final_eligible=final_eligible,
        max_correct_count=max_correct_count,
    )


def create_issue_multi_guarded(
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
    exact_start = issue_service._timestamp(issue_service._as_utc(starts_at))
    duplicate = conn.execute(
        """
        SELECT id FROM jackside_issues
        WHERE starts_at=? AND status<>'cancelled'
        ORDER BY id DESC LIMIT 1
        """,
        (exact_start,),
    ).fetchone()
    if duplicate:
        raise ValueError("На это время уже существует выпуск JACKSIDE")
    return _ORIGINAL_CREATE_ISSUE_MULTI(
        conn,
        issue_date_value=issue_date_value,
        starts_at=starts_at,
        title=title,
        admin_id=admin_id,
        jackcoin_per_correct=jackcoin_per_correct,
        jackcoin_completion_bonus=jackcoin_completion_bonus,
        jackcoin_perfect_bonus=jackcoin_perfect_bonus,
        final_prize_type=final_prize_type,
        final_prize_catalog_reward_id=final_prize_catalog_reward_id,
        final_prize_jackcoin_amount=final_prize_jackcoin_amount,
        final_question_time_seconds=final_question_time_seconds,
        timezone_name=timezone_name,
    )


def _final_window_end(
    conn: sqlite3.Connection,
    *,
    issue: sqlite3.Row | dict[str, Any],
    campaign_code: str,
) -> datetime | None:
    starts = issue_service._parse_dt(
        str(issue["starts_at"]) if issue["starts_at"] else None
    )
    if not starts:
        return None
    fallback = min(300, max(5, int(issue["final_question_time_seconds"] or 30)))
    rows = conn.execute(
        """
        SELECT time_limit_seconds
        FROM quiz_questions
        WHERE campaign_code=? AND game_round='final' AND IFNULL(is_active,1)=1
        ORDER BY position,id
        """,
        (campaign_code,),
    ).fetchall()
    durations: list[int] = []
    for row in rows:
        try:
            seconds = int(row["time_limit_seconds"] or fallback)
        except (TypeError, ValueError):
            seconds = fallback
        durations.append(min(300, max(5, seconds)))
    if not durations:
        durations = [fallback]
    return starts + timedelta(
        seconds=DAILY_414_FINAL_TABLE_DELAY_SECONDS + sum(durations)
    )


def _final_prize_settlement_pending(
    final_table: sqlite3.Row | dict[str, Any] | None,
) -> bool:
    """Keep a completed winner final reachable until its configured prize settles."""
    if not final_table or str(final_table["status"] or "") != "completed":
        return False
    if str(final_table["outcome"] or "") in {"no_winner", "cancelled"}:
        return False
    if str(final_table["prize_resolution"] or "") in {
        "awarded",
        "manual_task",
        "none",
    }:
        return False
    if final_table["winner_reward_id"] or int(
        final_table["winner_jackcoin_awarded"] or 0
    ):
        return False
    if final_table["winner_reward_error"]:
        return False
    prize_type = str(final_table["prize_type"] or "none")
    if final_table["prize_catalog_reward_id"]:
        return True
    return prize_type == "jackcoin" and int(
        final_table["prize_jackcoin_amount"] or 0
    ) > 0


def _runtime_status(
    conn: sqlite3.Connection,
    *,
    issue: sqlite3.Row | dict[str, Any],
    campaign: sqlite3.Row | dict[str, Any],
    now: datetime,
    timezone_name: str,
) -> str:
    code = str(campaign["code"] or "")
    version = max(1, int(campaign["current_version"] or 1))
    final_table = conn.execute(
        """
        SELECT * FROM daily_414_final_tables
        WHERE campaign_code=? AND campaign_version=?
        """,
        (code, version),
    ).fetchone()
    runtime = issue_service.compute_issue_status(
        issue,
        now=now,
        final_table=final_table,
        timezone_name=timezone_name,
    )
    if _final_prize_settlement_pending(final_table):
        return "final_live"
    final_end = _final_window_end(conn, issue=issue, campaign_code=code)
    if final_end and issue_service._as_utc(now) >= final_end:
        if runtime in {"waiting_final", "final_live"}:
            if final_table and str(final_table["status"] or "") not in {
                "completed",
                "unavailable",
            }:
                return runtime
            return "closed"
    return runtime


def effective_campaign_schedule_multi(
    conn: sqlite3.Connection,
    campaign: sqlite3.Row | dict[str, Any],
    *,
    timezone_name: str = "Europe/Moscow",
) -> dict[str, Any]:
    """Hide a finished release from member campaign lists without mutating history."""
    payload = _ORIGINAL_EFFECTIVE_CAMPAIGN_SCHEDULE(
        conn, campaign, timezone_name=timezone_name
    )
    if str(payload.get("campaign_type") or "") != "daily_414":
        return payload
    code = str(payload.get("code") or "")
    if not code:
        return payload
    issue = issue_service.get_issue_by_campaign(conn, code)
    if not issue:
        return payload
    runtime = _runtime_status(
        conn,
        issue=issue,
        campaign=payload,
        now=datetime.now(timezone.utc),
        timezone_name=timezone_name,
    )
    if runtime in {"closed", "cancelled", "technical_review", "draft"}:
        payload["is_active"] = 0
    return payload


def _featured_key(
    status: str,
    starts_at: datetime | None,
    now: datetime,
    issue_id: int,
) -> tuple[int, float, float, int]:
    priority = {
        "main_live": 0,
        "final_live": 0,
        "waiting_final": 1,
        "lobby": 2,
        "scheduled": 3,
    }.get(status, 50)
    if starts_at is None:
        return (priority, float("inf"), float("inf"), issue_id)
    return (
        priority,
        abs((starts_at - now).total_seconds()),
        starts_at.timestamp(),
        issue_id,
    )


def current_featured_issue_runtime(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    timezone_name: str = "Europe/Moscow",
) -> dict[str, Any] | None:
    """Choose the running release, otherwise the nearest next release."""
    now_utc = issue_service._as_utc(now)
    candidates: list[tuple[tuple[int, float, float, int], dict[str, Any]]] = []
    for issue in issue_service.list_issues(conn, limit=100):
        if str(issue["status"] or "") == "draft":
            continue
        campaign = conn.execute(
            "SELECT * FROM quiz_campaigns WHERE code=?",
            (issue["campaign_code"],),
        ).fetchone()
        if not campaign or not int(campaign["is_active"] or 0):
            continue
        status = _runtime_status(
            conn,
            issue=issue,
            campaign=campaign,
            now=now_utc,
            timezone_name=timezone_name,
        )
        if status not in {
            "main_live",
            "final_live",
            "waiting_final",
            "lobby",
            "scheduled",
        }:
            continue
        starts = issue_service._parse_dt(
            str(issue["starts_at"]) if issue["starts_at"] else None
        )
        payload = issue_service.resolve_issue_for_campaign(
            conn,
            campaign,
            now=now_utc,
            timezone_name=timezone_name,
        )
        payload["status"] = status
        candidates.append(
            (_featured_key(status, starts, now_utc, int(issue["id"])), payload)
        )
    if candidates:
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    legacy: list[tuple[tuple[int, float, float, int], dict[str, Any]]] = []
    rows = conn.execute(
        """
        SELECT * FROM quiz_campaigns
        WHERE campaign_type='daily_414' AND is_active=1
          AND archived_at IS NULL AND deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM jackside_issues ji WHERE ji.campaign_code=quiz_campaigns.code
          )
        """
    ).fetchall()
    for campaign in rows:
        payload = issue_service.legacy_issue_from_campaign(
            conn, campaign, now=now_utc, timezone_name=timezone_name
        )
        status = str(payload.get("status") or "")
        if status not in {
            "main_live",
            "final_live",
            "waiting_final",
            "lobby",
            "scheduled",
        }:
            continue
        starts = None
        raw = payload.get("starts_at")
        if raw:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            starts = parsed.astimezone(timezone.utc)
        legacy.append(
            (
                _featured_key(status, starts, now_utc, int(campaign["id"])),
                payload,
            )
        )
    if legacy:
        legacy.sort(key=lambda item: item[0])
        return legacy[0][1]
    return None


def reschedule_future_issue_runtime(
    conn: sqlite3.Connection,
    *,
    issue_id: int,
    issue_date_value: date,
    starts_at: datetime,
    title: str | None,
    timezone_name: str,
) -> sqlite3.Row:
    """Reschedule while keeping the per-day streak economy snapshotted."""
    updated = _ORIGINAL_RESCHEDULE_FUTURE_ISSUE(
        conn,
        issue_id=issue_id,
        issue_date_value=issue_date_value,
        starts_at=starts_at,
        title=title,
        timezone_name=timezone_name,
    )
    if _table_exists(conn, "jackcoin_economy_snapshots") and _table_exists(
        conn, "jackcoin_economy_settings"
    ):
        conn.execute(
            """
            INSERT OR IGNORE INTO jackcoin_economy_snapshots(
                entity_type, entity_id, setting_key, amount
            )
            SELECT 'jackside_day', ?, setting_key, amount
            FROM jackcoin_economy_settings
            WHERE setting_key LIKE 'streak_%'
            """,
            (issue_date_value.isoformat(),),
        )
    return updated


def apply_runtime_overrides() -> None:
    issue_service.ensure_default_rules = ensure_default_rules_flexible
    issue_service.validate_issue_for_publish = validate_issue_for_publish_flexible
    issue_service.effective_campaign_schedule = effective_campaign_schedule_multi
    issue_service.current_featured_issue = current_featured_issue_runtime
    issue_service.create_issue = create_issue_multi_guarded
    multi_issue.create_issue_multi = create_issue_multi_guarded
    multi_issue.reschedule_future_issue = reschedule_future_issue_runtime
    daily_service.validate_daily_questions = validate_daily_questions_flexible
    daily_service.public_daily_questions = public_daily_questions_flexible
    daily_service.award_daily_jackcoin = award_daily_jackcoin_flexible


apply_runtime_overrides()

# The admin IA templates use a lightweight version token for these assets.
# Bump it in one place so browsers do not retain the pre-hotfix JS.
try:
    from app import admin_information_architecture as admin_ia

    admin_ia.ASSET_VERSION = "admin-ia-v5"
except ImportError:
    pass


__all__ = [
    "apply_runtime_overrides",
    "award_daily_jackcoin_flexible",
    "create_issue_multi_guarded",
    "current_featured_issue_runtime",
    "effective_campaign_schedule_multi",
    "ensure_default_rules_flexible",
    "reschedule_future_issue_runtime",
    "result_copy_for_score_flexible",
    "validate_daily_questions_flexible",
    "validate_issue_for_publish_flexible",
]
