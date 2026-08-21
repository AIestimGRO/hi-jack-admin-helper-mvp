from __future__ import annotations

from datetime import date, datetime
from typing import Any

import sqlite3

from app import jackside_multi_issue as multi_issue
from app.jackside_flexible_labels import normalize_builtin_perfect_labels
from app.services import daily_414_final as final_service
from app.services import jackside_copy as copy_service
from app.services import jackside_issues as issue_service


_PREVIOUS_EFFECTIVE_SCHEDULE = issue_service.effective_campaign_schedule
_PREVIOUS_VALIDATE_ISSUE = issue_service.validate_issue_for_publish
_PREVIOUS_ENSURE_DEFAULT_RULES = issue_service.ensure_default_rules
_PREVIOUS_RESCHEDULE = multi_issue.reschedule_future_issue
_PREVIOUS_SEED_FINALISTS = final_service.seed_finalists
_LEGACY_DEFAULT_RULES_VERSION = "1.1"
_LEGACY_DEFAULT_RULES_MARKER = (
    "в финал проходят до 10 лучших по правильным ответам, затем по зачётному времени;"
)


def effective_campaign_schedule_compat(
    conn: sqlite3.Connection,
    campaign: sqlite3.Row | dict[str, Any],
    *,
    timezone_name: str = "Europe/Moscow",
) -> dict[str, Any]:
    payload = _PREVIOUS_EFFECTIVE_SCHEDULE(
        conn, campaign, timezone_name=timezone_name
    )
    if str(payload.get("campaign_type") or "") != "daily_414":
        return payload
    code = str(payload.get("code") or "")
    if not code:
        return payload
    issue = issue_service.get_issue_by_campaign(conn, code)
    if issue and str(issue["status"] or "") == "draft":
        try:
            payload["is_active"] = int(campaign["is_active"] or 0)
        except (KeyError, IndexError, TypeError):
            pass
    return payload


def ensure_default_rules_compat(conn: sqlite3.Connection) -> sqlite3.Row:
    """Migrate only the known built-in 1.1 rules to the current built-in version."""
    current = _PREVIOUS_ENSURE_DEFAULT_RULES(conn)
    target_version = copy_service.DEFAULT_RULES_VERSION
    if str(current["version"] or "") == target_version:
        return current

    is_previous_builtin = bool(
        str(current["version"] or "") == _LEGACY_DEFAULT_RULES_VERSION
        and str(current["title"] or "") == copy_service.DEFAULT_RULES_TITLE
        and _LEGACY_DEFAULT_RULES_MARKER in str(current["content"] or "")
    )
    if not is_previous_builtin:
        return current

    existing = conn.execute(
        "SELECT * FROM jackside_rules_versions WHERE version=? ORDER BY id DESC LIMIT 1",
        (target_version,),
    ).fetchone()
    conn.execute("UPDATE jackside_rules_versions SET is_active=0 WHERE is_active=1")
    if existing:
        conn.execute(
            """
            UPDATE jackside_rules_versions
            SET title=?, content=?, is_active=1
            WHERE id=?
            """,
            (
                copy_service.DEFAULT_RULES_TITLE,
                copy_service.DEFAULT_RULES_CONTENT,
                int(existing["id"]),
            ),
        )
        rules_id = int(existing["id"])
    else:
        rules_id = int(
            conn.execute(
                """
                INSERT INTO jackside_rules_versions(version, title, content, is_active)
                VALUES (?, ?, ?, 1)
                """,
                (
                    target_version,
                    copy_service.DEFAULT_RULES_TITLE,
                    copy_service.DEFAULT_RULES_CONTENT,
                ),
            ).lastrowid
        )

    # Keep historical closed releases pinned to the rules they actually used.
    # Draft/current releases that still point at the previous built-in rules
    # follow the new product rule immediately.
    conn.execute(
        """
        UPDATE jackside_issues
        SET rules_version_id=?, rules_version=?, updated_at=CURRENT_TIMESTAMP
        WHERE rules_version=?
          AND status NOT IN ('closed', 'cancelled')
        """,
        (rules_id, target_version, _LEGACY_DEFAULT_RULES_VERSION),
    )
    return conn.execute(
        "SELECT * FROM jackside_rules_versions WHERE id=?",
        (rules_id,),
    ).fetchone()


def validate_issue_for_publish_compat(
    conn: sqlite3.Connection,
    issue: sqlite3.Row | dict[str, Any],
) -> list[str]:
    normalize_builtin_perfect_labels(conn)
    active = issue_service.ensure_default_rules(conn)
    current = issue
    if str(issue["rules_version"] or "") in {
        _LEGACY_DEFAULT_RULES_VERSION,
        copy_service.DEFAULT_RULES_VERSION,
    } and str(active["version"] or "") != str(issue["rules_version"] or ""):
        conn.execute(
            """
            UPDATE jackside_issues
            SET rules_version_id=?, rules_version=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (int(active["id"]), str(active["version"]), int(issue["id"])),
        )
        refreshed = issue_service.get_issue(conn, int(issue["id"]))
        if refreshed:
            current = refreshed
    return _PREVIOUS_VALIDATE_ISSUE(conn, current)


def reschedule_future_issue_compat(
    conn: sqlite3.Connection,
    *,
    issue_id: int,
    issue_date_value: date,
    starts_at: datetime,
    title: str | None,
    timezone_name: str,
) -> sqlite3.Row:
    exact_start = issue_service._timestamp(issue_service._as_utc(starts_at))
    duplicate = conn.execute(
        """
        SELECT id FROM jackside_issues
        WHERE starts_at=? AND id<>? AND status<>'cancelled'
        ORDER BY id DESC LIMIT 1
        """,
        (exact_start, int(issue_id)),
    ).fetchone()
    if duplicate:
        raise ValueError("На это время уже существует выпуск JACKSIDE")
    return _PREVIOUS_RESCHEDULE(
        conn,
        issue_id=issue_id,
        issue_date_value=issue_date_value,
        starts_at=starts_at,
        title=title,
        timezone_name=timezone_name,
    )


def seed_finalists_compat(
    conn: sqlite3.Connection,
    *,
    final_table: sqlite3.Row,
) -> None:
    """JACKSIDE final includes every fully completed main-round submission."""
    campaign_code = str(final_table["campaign_code"] or "")
    if not campaign_code.startswith("jackside_"):
        _PREVIOUS_SEED_FINALISTS(conn, final_table=final_table)
        return
    if conn.execute(
        "SELECT 1 FROM daily_414_finalists WHERE final_table_id=? LIMIT 1",
        (final_table["id"],),
    ).fetchone():
        return

    submissions = conn.execute(
        """
        SELECT id, client_id
        FROM quiz_submissions
        WHERE campaign_code=? AND campaign_version=?
          AND main_prize_eligible=1
          AND IFNULL(main_round_completed, 1)=1
        ORDER BY created_at ASC, id ASC
        """,
        (campaign_code, final_table["campaign_version"]),
    ).fetchall()
    conn.executemany(
        """
        INSERT OR IGNORE INTO daily_414_finalists(
            final_table_id, submission_id, client_id, seed
        ) VALUES (?, ?, ?, ?)
        """,
        [
            (
                final_table["id"],
                submission["id"],
                submission["client_id"],
                seed,
            )
            for seed, submission in enumerate(submissions, start=1)
        ],
    )


def result_copy_for_score_compat(
    correct_count: int,
    *,
    final_eligible: bool = True,
) -> dict[str, str]:
    """Keep score bands while making final admission independent of score/speed."""
    score = max(0, int(correct_count))
    if score <= 3:
        code = "0_3"
    elif score <= 6:
        code = "4_6"
    elif score <= 8:
        code = "7_8"
    elif score == 9:
        code = "9"
    else:
        code = "10"
    if final_eligible:
        message = (
            "Основная часть завершена — вы в финальном столе. "
            "Количество правильных ответов и скорость прохождения на допуск "
            "в финал не влияют. JACKCOIN за основную часть уже начислены."
        )
    else:
        message = (
            "Основная часть не была полностью завершена до общего дедлайна 4:14, "
            "поэтому финальный стол недоступен."
        )
    return {
        "code": code,
        "title": "Основной раунд завершён",
        "message": message,
    }


issue_service.ensure_default_rules = ensure_default_rules_compat
issue_service.effective_campaign_schedule = effective_campaign_schedule_compat
issue_service.validate_issue_for_publish = validate_issue_for_publish_compat
multi_issue.reschedule_future_issue = reschedule_future_issue_compat
final_service.seed_finalists = seed_finalists_compat
copy_service.result_copy_for_score = result_copy_for_score_compat


__all__ = [
    "effective_campaign_schedule_compat",
    "ensure_default_rules_compat",
    "reschedule_future_issue_compat",
    "result_copy_for_score_compat",
    "seed_finalists_compat",
    "validate_issue_for_publish_compat",
]
