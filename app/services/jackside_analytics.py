from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from math import sqrt
from statistics import median
from typing import Any, Callable
from zoneinfo import ZoneInfo


CACHE_KEY = "jackside.analytics.v1"
CACHE_REFRESH_SECONDS = 300
CACHE_MAX_AGE_SECONDS = 900
RATING_WINDOW_DAYS = 30
MIN_RATED_GAMES = 3
MIN_RATED_ANSWERS = 30
FULL_ACTIVITY_GAMES = 8
ACCURACY_WEIGHT = 0.85
ACTIVITY_WEIGHT = 0.15
WILSON_Z = 1.96


def _as_utc(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str)
        else value
    )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _local_date(value: datetime | str | None, tz: ZoneInfo) -> date:
    return _as_utc(value).astimezone(tz).date()


def _percent(numerator: int | float, denominator: int | float) -> int | float:
    if not denominator:
        return 0
    rounded = round(float(numerator) * 100 / float(denominator), 1)
    return int(rounded) if rounded.is_integer() else rounded


def _round_number(value: float) -> int | float:
    rounded = round(value, 1)
    return int(rounded) if rounded.is_integer() else rounded


def _wilson_lower_bound(correct: int, total: int) -> float:
    if total <= 0:
        return 0.0
    ratio = correct / total
    z_squared = WILSON_Z**2
    numerator = ratio + z_squared / (2 * total) - WILSON_Z * sqrt(
        (ratio * (1 - ratio) + z_squared / (4 * total)) / total
    )
    return 100 * numerator / (1 + z_squared / total)


def _display_name(row: sqlite3.Row | dict[str, Any]) -> str:
    first_name = str(row["first_name"] or "").strip()
    nickname = str(row["nickname"] or "").strip()
    username = str(row["username"] or "").strip().lstrip("@")
    client_id = int(row["client_id"] or row["id"])
    if first_name:
        return first_name
    if nickname:
        return nickname
    if username:
        return f"@{username}"
    return f"HJ #{client_id}"


def _title_code(points: int) -> str:
    if points >= 500:
        return "legend"
    if points >= 250:
        return "final_regular"
    if points >= 100:
        return "strong_player"
    if points >= 30:
        return "regular"
    if points > 0:
        return "participant"
    return "rookie"


def _effective_current_streak(
    current: int,
    last_issue_date: str | None,
    today: date,
) -> int:
    if not last_issue_date:
        return 0
    try:
        last_day = date.fromisoformat(str(last_issue_date))
    except ValueError:
        return 0
    return max(0, int(current)) if last_day >= today - timedelta(days=1) else 0


def _completed_submissions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT qs.id, qs.attempt_id, qs.campaign_code, qs.campaign_version,
               qs.client_id, qs.correct_count, qs.max_correct_count,
               qs.completion_time_ms, qs.main_prize_eligible,
               qs.jackcoin_awarded, qs.created_at,
               c.first_name, c.nickname, c.username,
               ji.issue_date
        FROM quiz_submissions qs
        JOIN quiz_campaigns qc
          ON qc.code=qs.campaign_code AND qc.campaign_type='daily_414'
        JOIN clients c ON c.id=qs.client_id
        LEFT JOIN jackside_issues ji ON ji.campaign_code=qs.campaign_code
        WHERE IFNULL(qs.main_round_completed, 1)=1
          AND qs.max_correct_count > 0
        ORDER BY qs.created_at, qs.id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _final_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT df.id AS finalist_id, df.final_table_id, df.submission_id,
               df.client_id, df.status, dft.campaign_code,
               dft.campaign_version, dft.outcome, dft.prize_resolution,
               dft.winner_jackcoin_awarded, dft.completed_at
        FROM daily_414_finalists df
        JOIN daily_414_final_tables dft ON dft.id=df.final_table_id
        ORDER BY dft.id, df.seed
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _month_rating(
    submissions: list[dict[str, Any]],
    *,
    now_utc: datetime,
) -> list[dict[str, Any]]:
    cutoff = now_utc - timedelta(days=RATING_WINDOW_DAYS)
    grouped: dict[int, dict[str, Any]] = {}
    for row in submissions:
        created_at = _as_utc(row["created_at"])
        if created_at < cutoff or created_at > now_utc:
            continue
        client_id = int(row["client_id"])
        item = grouped.setdefault(
            client_id,
            {
                "client_id": client_id,
                "display_name": _display_name(row),
                "completed_count": 0,
                "active_dates": set(),
                "correct_total": 0,
                "question_total": 0,
                "last_result_at": "",
            },
        )
        item["completed_count"] += 1
        item["active_dates"].add(created_at.date().isoformat())
        item["correct_total"] += int(row["correct_count"] or 0)
        item["question_total"] += int(row["max_correct_count"] or 0)
        item["last_result_at"] = max(
            str(item["last_result_at"]), str(row["created_at"] or "")
        )

    ranked: list[dict[str, Any]] = []
    calibrating: list[dict[str, Any]] = []
    for item in grouped.values():
        completed_count = int(item["completed_count"])
        question_total = int(item["question_total"])
        correct_total = int(item["correct_total"])
        active_days = len(item.pop("active_dates"))
        confirmed_accuracy = _wilson_lower_bound(correct_total, question_total)
        activity_score = min(completed_count / FULL_ACTIVITY_GAMES, 1) * 100
        rating_score = (
            confirmed_accuracy * ACCURACY_WEIGHT
            + activity_score * ACTIVITY_WEIGHT
        )
        is_calibrating = (
            completed_count < MIN_RATED_GAMES
            or question_total < MIN_RATED_ANSWERS
        )
        entry = {
            **item,
            "active_days": active_days,
            "accuracy": _percent(correct_total, question_total),
            "confirmed_accuracy": _round_number(confirmed_accuracy),
            "activity_score": _round_number(activity_score),
            "rating_score": _round_number(rating_score),
            "status": "calibration" if is_calibrating else "ranked",
            "calibration_games_left": max(0, MIN_RATED_GAMES - completed_count),
            "calibration_answers_left": max(0, MIN_RATED_ANSWERS - question_total),
            "calibration_progress": round(
                min(
                    completed_count / MIN_RATED_GAMES,
                    question_total / MIN_RATED_ANSWERS,
                    1,
                )
                * 100
            ),
            "_rating_raw": rating_score,
            "_confirmed_raw": confirmed_accuracy,
        }
        (calibrating if is_calibrating else ranked).append(entry)

    ranked.sort(
        key=lambda item: (
            -float(item["_rating_raw"]),
            -int(item["active_days"]),
            -int(item["correct_total"]),
            str(item["last_result_at"]),
            int(item["client_id"]),
        )
    )
    for place, item in enumerate(ranked, start=1):
        item["place"] = place
    calibrating.sort(
        key=lambda item: (
            -int(item["calibration_progress"]),
            -float(item["_confirmed_raw"]),
            -int(item["active_days"]),
            -int(item["correct_total"]),
            str(item["last_result_at"]),
            int(item["client_id"]),
        )
    )
    for item in calibrating:
        item["place"] = None
    result = ranked + calibrating
    for item in result:
        item.pop("_rating_raw", None)
        item.pop("_confirmed_raw", None)
    return result


def _today_rating(
    submissions: list[dict[str, Any]],
    finals: list[dict[str, Any]],
    *,
    today: date,
    tz: ZoneInfo,
) -> list[dict[str, Any]]:
    final_by_submission = {int(row["submission_id"]): row for row in finals}
    entries: list[dict[str, Any]] = []
    for row in submissions:
        issue_day = (
            date.fromisoformat(str(row["issue_date"]))
            if row.get("issue_date")
            else _local_date(row["created_at"], tz)
        )
        if issue_day != today:
            continue
        final = final_by_submission.get(int(row["id"]))
        status = str(final["status"]) if final else ""
        outcome = str(final["outcome"] or "") if final else ""
        entries.append(
            {
                "submission_id": int(row["id"]),
                "client_id": int(row["client_id"]),
                "display_name": _display_name(row),
                "correct_count": int(row["correct_count"] or 0),
                "question_total": int(row["max_correct_count"] or 0),
                "completion_time_ms": row["completion_time_ms"],
                "is_finalist": bool(final),
                "is_winner": status == "winner",
                "is_co_winner": status == "winner" and outcome == "co_winners",
                "created_at": str(row["created_at"] or ""),
            }
        )
    entries.sort(
        key=lambda item: (
            -int(item["correct_count"]),
            int(item["completion_time_ms"] or 2_147_483_647),
            int(item["submission_id"]),
        )
    )
    for place, item in enumerate(entries, start=1):
        item["place"] = place
        item["accuracy"] = _percent(
            int(item["correct_count"]), int(item["question_total"])
        )
        item["average_answer_time_ms"] = (
            round(int(item["completion_time_ms"]) / int(item["question_total"]))
            if item["completion_time_ms"] and item["question_total"]
            else None
        )
    return entries


def _all_time_rating(
    submissions: list[dict[str, Any]],
    finals: list[dict[str, Any]],
    progress: dict[int, dict[str, Any]],
    *,
    today: date,
) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for row in submissions:
        client_id = int(row["client_id"])
        item = grouped.setdefault(
            client_id,
            {
                "client_id": client_id,
                "display_name": _display_name(row),
                "completed_count": 0,
                "correct_total": 0,
                "question_total": 0,
                "completion_time_total_ms": 0,
                "timed_question_total": 0,
                "best_result": 0,
                "perfect_count": 0,
                "finals_count": 0,
                "wins_count": 0,
                "co_wins_count": 0,
            },
        )
        correct = int(row["correct_count"] or 0)
        questions = int(row["max_correct_count"] or 0)
        item["completed_count"] += 1
        item["correct_total"] += correct
        item["question_total"] += questions
        item["best_result"] = max(int(item["best_result"]), correct)
        if questions > 0 and correct == questions:
            item["perfect_count"] += 1
        if row["completion_time_ms"] is not None and questions > 0:
            item["completion_time_total_ms"] += int(row["completion_time_ms"])
            item["timed_question_total"] += questions

    seen_finals: set[tuple[int, int]] = set()
    seen_wins: set[tuple[int, int]] = set()
    for row in finals:
        client_id = int(row["client_id"])
        item = grouped.get(client_id)
        if not item:
            continue
        final_key = (client_id, int(row["final_table_id"]))
        if final_key not in seen_finals:
            item["finals_count"] += 1
            seen_finals.add(final_key)
        if str(row["status"]) == "winner" and final_key not in seen_wins:
            item["wins_count"] += 1
            if str(row["outcome"] or "") == "co_winners":
                item["co_wins_count"] += 1
            seen_wins.add(final_key)

    result: list[dict[str, Any]] = []
    for client_id, item in grouped.items():
        progress_row = progress.get(client_id, {})
        current_streak = _effective_current_streak(
            int(progress_row.get("current_streak") or 0),
            progress_row.get("last_issue_date"),
            today,
        )
        points = (
            int(item["correct_total"])
            + 2 * int(item["finals_count"])
            + 5 * int(item["wins_count"])
        )
        avg_answer = (
            round(
                int(item["completion_time_total_ms"])
                / int(item["timed_question_total"])
            )
            if item["timed_question_total"]
            else None
        )
        entry = {
            **item,
            "points": points,
            "accuracy": _percent(
                int(item["correct_total"]), int(item["question_total"])
            ),
            "average_answer_time_ms": avg_answer,
            "current_streak": current_streak,
            "best_streak": int(progress_row.get("best_streak") or 0),
            "title_code": _title_code(points),
        }
        entry.pop("completion_time_total_ms", None)
        entry.pop("timed_question_total", None)
        result.append(entry)

    result.sort(
        key=lambda item: (
            -int(item["points"]),
            -float(item["accuracy"]),
            int(item["average_answer_time_ms"] or 2_147_483_647),
            -int(item["completed_count"]),
            int(item["client_id"]),
        )
    )
    for place, item in enumerate(result, start=1):
        item["place"] = place
    return result


def _economy_by_client(conn: sqlite3.Connection) -> dict[int, dict[str, int]]:
    result: dict[int, dict[str, int]] = defaultdict(
        lambda: {
            "jackcoin_earned": 0,
            "jackcoin_spent": 0,
            "active_cards": 0,
            "used_cards": 0,
            "qualified_referrals": 0,
        }
    )
    for row in conn.execute(
        """
        SELECT client_id,
               COALESCE(SUM(CASE WHEN amount > 0 AND operation_type<>'vault_refund'
                                 THEN amount ELSE 0 END), 0) AS earned,
               COALESCE(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END), 0) AS spent
        FROM jackcoin_ledger GROUP BY client_id
        """
    ).fetchall():
        item = result[int(row["client_id"])]
        item["jackcoin_earned"] = int(row["earned"] or 0)
        item["jackcoin_spent"] = int(row["spent"] or 0)
    for row in conn.execute(
        """
        SELECT client_id,
               SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active_cards,
               SUM(CASE WHEN status='redeemed' THEN 1 ELSE 0 END) AS used_cards
        FROM vault_member_rewards GROUP BY client_id
        """
    ).fetchall():
        item = result[int(row["client_id"])]
        item["active_cards"] = int(row["active_cards"] or 0)
        item["used_cards"] = int(row["used_cards"] or 0)
    for row in conn.execute(
        """
        SELECT qr.referrer_client_id AS client_id, COUNT(*) AS qualified
        FROM quiz_referrals qr
        JOIN quiz_submissions qs ON qs.id=qr.submission_id
        JOIN quiz_campaigns qc
          ON qc.code=qs.campaign_code AND qc.campaign_type='daily_414'
        WHERE IFNULL(qs.main_round_completed, 1)=1
        GROUP BY qr.referrer_client_id
        """
    ).fetchall():
        result[int(row["client_id"])]["qualified_referrals"] = int(
            row["qualified"] or 0
        )
    return result


def _retention_rate(
    accounts: list[dict[str, Any]],
    completed_days: dict[int, set[date]],
    *,
    today: date,
    offset_days: int,
    tz: ZoneInfo,
) -> dict[str, Any]:
    eligible = 0
    returned = 0
    for row in accounts:
        cohort_day = _local_date(row["created_at"], tz)
        if cohort_day > today - timedelta(days=offset_days):
            continue
        eligible += 1
        target = cohort_day + timedelta(days=offset_days)
        if target in completed_days.get(int(row["client_id"]), set()):
            returned += 1
    return {
        "eligible": eligible,
        "returned": returned,
        "rate": _percent(returned, eligible) if eligible else None,
    }


def _admin_analytics(
    conn: sqlite3.Connection,
    submissions: list[dict[str, Any]],
    finals: list[dict[str, Any]],
    all_time: list[dict[str, Any]],
    *,
    now_utc: datetime,
    today: date,
    tz: ZoneInfo,
) -> dict[str, Any]:
    completed_days: dict[int, set[date]] = defaultdict(set)
    recent_30: list[dict[str, Any]] = []
    cutoff_utc = now_utc - timedelta(days=30)
    for row in submissions:
        issue_day = (
            date.fromisoformat(str(row["issue_date"]))
            if row.get("issue_date")
            else _local_date(row["created_at"], tz)
        )
        completed_days[int(row["client_id"])].add(issue_day)
        created_at = _as_utc(row["created_at"])
        if cutoff_utc <= created_at <= now_utc:
            recent_30.append(row)

    accounts = [
        dict(row)
        for row in conn.execute(
            """
            SELECT client_id, created_at FROM member_accounts
            WHERE is_active=1
            """
        ).fetchall()
    ]
    activity = {
        str(days): sum(
            1
            for dates in completed_days.values()
            if any(today - timedelta(days=days - 1) <= day <= today for day in dates)
        )
        for days in (3, 7, 14, 30)
    }
    current_streaks = [int(row["current_streak"]) for row in all_time]
    final_clients = {
        (int(row["client_id"]), int(row["final_table_id"])) for row in finals
    }
    recent_ids = {int(row["id"]) for row in recent_30}
    recent_finalists = {
        int(row["submission_id"])
        for row in finals
        if int(row["submission_id"]) in recent_ids
    }

    campaign_codes = [
        str(row[0])
        for row in conn.execute(
            "SELECT code FROM quiz_campaigns WHERE campaign_type='daily_414'"
        ).fetchall()
    ]
    attempt_rows: list[sqlite3.Row] = []
    if campaign_codes:
        placeholders = ",".join("?" for _ in campaign_codes)
        attempt_rows = conn.execute(
            f"""
            SELECT id, status, created_at FROM quiz_attempts
            WHERE campaign_code IN ({placeholders})
              AND created_at >= ? AND created_at <= ?
            """,
            (
                *campaign_codes,
                (now_utc - timedelta(days=30)).isoformat(timespec="seconds"),
                now_utc.isoformat(timespec="seconds"),
            ),
        ).fetchall()
    timeout_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM quiz_submissions qs
            JOIN quiz_campaigns qc
              ON qc.code=qs.campaign_code AND qc.campaign_type='daily_414'
            WHERE IFNULL(qs.timed_out, 0)=1
              AND qs.created_at >= ? AND qs.created_at <= ?
            """,
            (
                (now_utc - timedelta(days=30)).isoformat(timespec="seconds"),
                now_utc.isoformat(timespec="seconds"),
            ),
        ).fetchone()[0]
    )
    completed_recent = len(recent_30)
    started_attempts = max(
        len(attempt_rows), completed_recent + timeout_count
    )
    late_count = sum(
        1 for row in recent_30 if not int(row["main_prize_eligible"] or 0)
    )

    registered = len(accounts)
    joined_today = sum(1 for row in accounts if _local_date(row["created_at"], tz) == today)
    completed_today = sum(1 for dates in completed_days.values() if today in dates)

    ledger = conn.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN amount>0 AND operation_type<>'vault_refund'
                            THEN amount ELSE 0 END), 0) AS accrued,
          COALESCE(SUM(CASE WHEN amount<0 THEN -amount ELSE 0 END), 0) AS spent,
          COALESCE(SUM(CASE WHEN operation_type='vault_refund'
                            THEN amount ELSE 0 END), 0) AS refunded,
          COALESCE(SUM(amount), 0) AS balance
        FROM jackcoin_ledger
        """
    ).fetchone()
    accrual_reasons = [
        {"reason": str(row["source_type"]), "amount": int(row["amount"] or 0)}
        for row in conn.execute(
            """
            SELECT source_type, SUM(amount) AS amount
            FROM jackcoin_ledger
            WHERE amount>0 AND operation_type<>'vault_refund'
            GROUP BY source_type ORDER BY amount DESC, source_type
            """
        ).fetchall()
    ]
    reward_spend = [
        {"title": str(row["title"]), "amount": int(row["amount"] or 0)}
        for row in conn.execute(
            """
            SELECT vcr.title, SUM(vmr.price_paid_jc) AS amount
            FROM vault_member_rewards vmr
            JOIN vault_catalog_rewards vcr ON vcr.id=vmr.catalog_reward_id
            WHERE vmr.source_type='purchase' AND vmr.price_paid_jc>0
            GROUP BY vcr.id ORDER BY amount DESC, vcr.title
            """
        ).fetchall()
    ]
    rewards = conn.execute(
        """
        SELECT
          SUM(CASE WHEN activated_at IS NOT NULL THEN 1 ELSE 0 END) AS activated,
          SUM(CASE WHEN status='redeemed' THEN 1 ELSE 0 END) AS redeemed,
          SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END) AS expired,
          SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS unused,
          COUNT(*) AS total
        FROM vault_member_rewards
        """
    ).fetchone()
    co_payouts = conn.execute(
        """
        SELECT COUNT(*) AS tables_count,
               COALESCE(SUM(winner_jackcoin_awarded), 0) AS jackcoin
        FROM daily_414_final_tables
        WHERE outcome='co_winners' AND prize_resolution='awarded'
        """
    ).fetchone()
    manual_resolutions = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM daily_414_master_tasks WHERE status='done'
            """
        ).fetchone()[0]
    )
    qualified_referrals = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM quiz_referrals qr
            JOIN quiz_submissions qs ON qs.id=qr.submission_id
            JOIN quiz_campaigns qc
              ON qc.code=qs.campaign_code AND qc.campaign_type='daily_414'
            WHERE IFNULL(qs.main_round_completed, 1)=1
            """
        ).fetchone()[0]
    )
    avg_result = (
        _round_number(
            sum(int(row["correct_count"] or 0) for row in recent_30)
            / completed_recent
        )
        if completed_recent
        else 0
    )
    return {
        "participants": {
            "registered": registered,
            "joined_today": joined_today,
            "completed_today": completed_today,
            "activity": activity,
            "retention": {
                str(days): _retention_rate(
                    accounts,
                    completed_days,
                    today=today,
                    offset_days=days,
                    tz=tz,
                )
                for days in (1, 7, 30)
            },
            "average_streak": _round_number(sum(current_streaks) / len(current_streaks))
            if current_streaks
            else 0,
            "median_streak": _round_number(float(median(current_streaks)))
            if current_streaks
            else 0,
            "average_result": avg_result,
            "completion_rate": _percent(completed_recent, started_attempts),
            "timeout_rate": _percent(timeout_count, started_attempts),
            "final_rate": _percent(len(recent_finalists), completed_recent),
            "late_rate": _percent(late_count, completed_recent),
            "qualified_referrals": qualified_referrals,
            "reward_usage_rate": _percent(
                int(rewards["redeemed"] or 0), int(rewards["total"] or 0)
            ),
            "final_participations": len(final_clients),
            "window_days": 30,
        },
        "economy": {
            "accrued": int(ledger["accrued"] or 0),
            "spent": int(ledger["spent"] or 0),
            "refunded": int(ledger["refunded"] or 0),
            "current_balance": int(ledger["balance"] or 0),
            "accrual_reasons": accrual_reasons,
            "reward_spend": reward_spend,
            "activated": int(rewards["activated"] or 0),
            "redeemed": int(rewards["redeemed"] or 0),
            "expired": int(rewards["expired"] or 0),
            "unused": int(rewards["unused"] or 0),
            "co_winner_payouts": int(co_payouts["tables_count"] or 0),
            "co_winner_jackcoin": int(co_payouts["jackcoin"] or 0),
            "manual_prize_resolutions": manual_resolutions,
        },
    }


def build_jackside_analytics(
    conn: sqlite3.Connection,
    *,
    as_of: datetime | str | None = None,
    timezone_name: str = "Europe/Moscow",
) -> dict[str, Any]:
    now_utc = _as_utc(as_of)
    tz = ZoneInfo(timezone_name)
    today = now_utc.astimezone(tz).date()
    submissions = _completed_submissions(conn)
    finals = _final_rows(conn)
    progress = {
        int(row["client_id"]): dict(row)
        for row in conn.execute("SELECT * FROM daily_414_progress").fetchall()
    }
    today_rating = _today_rating(submissions, finals, today=today, tz=tz)
    month_rating = _month_rating(submissions, now_utc=now_utc)
    all_time_rating = _all_time_rating(
        submissions, finals, progress, today=today
    )
    economy = _economy_by_client(conn)
    player_stats: dict[str, dict[str, Any]] = {}
    for entry in all_time_rating:
        client_id = int(entry["client_id"])
        player_stats[str(client_id)] = {**entry, **economy.get(client_id, {})}
    return {
        "generated_at": now_utc.isoformat(timespec="seconds"),
        "timezone_name": timezone_name,
        "today_date": today.isoformat(),
        "today": today_rating,
        "month": month_rating,
        "all_time": all_time_rating,
        "player_stats": player_stats,
        "admin": _admin_analytics(
            conn,
            submissions,
            finals,
            all_time_rating,
            now_utc=now_utc,
            today=today,
            tz=tz,
        ),
    }


def save_jackside_analytics_snapshot(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
) -> None:
    state = conn.execute(
        "SELECT source_version FROM jackside_analytics_state WHERE id=1"
    ).fetchone()
    source_version = int(state["source_version"] if state else 0)
    payload["source_version"] = source_version
    conn.execute(
        """
        INSERT INTO jackside_analytics_cache(cache_key, payload_json, generated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
          payload_json=excluded.payload_json,
          generated_at=excluded.generated_at
        """,
        (
            CACHE_KEY,
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            str(payload["generated_at"]),
        ),
    )
    conn.execute(
        """
        UPDATE jackside_analytics_state
        SET refreshed_version=? WHERE id=1
        """,
        (source_version,),
    )


def refresh_jackside_analytics(
    conn: sqlite3.Connection,
    *,
    as_of: datetime | str | None = None,
    timezone_name: str = "Europe/Moscow",
) -> dict[str, Any]:
    payload = build_jackside_analytics(
        conn, as_of=as_of, timezone_name=timezone_name
    )
    save_jackside_analytics_snapshot(conn, payload)
    return payload


def load_jackside_analytics_snapshot(
    conn: sqlite3.Connection,
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload_json FROM jackside_analytics_cache WHERE cache_key=?",
        (CACHE_KEY,),
    ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def analytics_sources_changed(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        """
        SELECT source_version, refreshed_version
        FROM jackside_analytics_state WHERE id=1
        """
    ).fetchone()
    if not row:
        return True
    return int(row["source_version"]) != int(row["refreshed_version"])


def snapshot_is_stale(
    payload: dict[str, Any] | None,
    *,
    as_of: datetime | str | None = None,
    max_age_seconds: int = CACHE_REFRESH_SECONDS,
) -> bool:
    if not payload or not payload.get("generated_at"):
        return True
    generated = _as_utc(str(payload["generated_at"]))
    return (_as_utc(as_of) - generated).total_seconds() >= max_age_seconds


def format_duration_ms(value: int | None) -> str:
    if value is None:
        return "-"
    seconds = max(0, int(value)) / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    return f"{minutes}:{remainder:04.1f}"


def get_or_refresh_snapshot(
    conn: sqlite3.Connection,
    *,
    as_of: datetime | str | None = None,
    timezone_name: str = "Europe/Moscow",
    force: bool = False,
    refresh: Callable[..., dict[str, Any]] = refresh_jackside_analytics,
) -> dict[str, Any]:
    payload = load_jackside_analytics_snapshot(conn)
    if force or not payload:
        return refresh(conn, as_of=as_of, timezone_name=timezone_name)
    max_age = (
        CACHE_REFRESH_SECONDS
        if analytics_sources_changed(conn)
        else CACHE_MAX_AGE_SECONDS
    )
    if snapshot_is_stale(payload, as_of=as_of, max_age_seconds=max_age):
        return refresh(conn, as_of=as_of, timezone_name=timezone_name)
    return payload
