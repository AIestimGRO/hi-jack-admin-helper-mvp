from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.services.quiz_rewards import issue_referral_reward

JACKSIDE_REFERRAL_SCOPE = "jackside"
QUALIFY_DAYS = 3


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _display_name(row: sqlite3.Row | dict[str, Any]) -> str:
    for key in ("first_name", "nickname", "username"):
        value = str(row[key] or "").strip() if key in row.keys() else ""
        if value:
            return f"@{value.lstrip('@')}" if key == "username" else value
    return f"HJ #{int(row['id'])}"


def ensure_jackside_referral_code(conn: sqlite3.Connection, client_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM quiz_referral_codes WHERE client_id=? AND campaign_code=?",
        (client_id, JACKSIDE_REFERRAL_SCOPE),
    ).fetchone()
    if row:
        return row
    for _ in range(30):
        code = secrets.token_urlsafe(10).replace("-", "").replace("_", "")[:14]
        try:
            conn.execute(
                "INSERT INTO quiz_referral_codes(code, client_id, campaign_code) VALUES (?, ?, ?)",
                (code, client_id, JACKSIDE_REFERRAL_SCOPE),
            )
            break
        except sqlite3.IntegrityError:
            continue
    row = conn.execute(
        "SELECT * FROM quiz_referral_codes WHERE client_id=? AND campaign_code=?",
        (client_id, JACKSIDE_REFERRAL_SCOPE),
    ).fetchone()
    if not row:
        raise RuntimeError("jackside_referral_code_generation_failed")
    return row


def resolve_referral_code(
    conn: sqlite3.Connection, *, code: str, campaign_code: str | None = None
) -> sqlite3.Row | None:
    clean = str(code or "").strip()
    if not clean:
        return None
    row = conn.execute(
        "SELECT * FROM quiz_referral_codes WHERE code=? AND campaign_code=?",
        (clean, JACKSIDE_REFERRAL_SCOPE),
    ).fetchone()
    if row or not campaign_code:
        return row
    return conn.execute(
        "SELECT * FROM quiz_referral_codes WHERE code=? AND campaign_code=?",
        (clean, campaign_code),
    ).fetchone()


def fix_jackside_referral(
    conn: sqlite3.Connection,
    *,
    invited_client_id: int,
    referral_code: str,
    campaign_code: str,
) -> dict[str, Any]:
    existing = conn.execute(
        "SELECT * FROM referral_qualification_progress WHERE invited_client_id=?",
        (invited_client_id,),
    ).fetchone()
    owner = resolve_referral_code(conn, code=referral_code, campaign_code=campaign_code)
    if not owner:
        return {"status": "unknown_code", "fixed": bool(existing)}
    referrer_id = int(owner["client_id"])
    if referrer_id == int(invited_client_id):
        return {"status": "self_referral", "fixed": bool(existing)}
    if existing:
        return {
            "status": "already_fixed" if int(existing["referrer_client_id"]) == referrer_id else "referrer_locked",
            "fixed": True,
            "referrer_client_id": int(existing["referrer_client_id"]),
        }
    conn.execute(
        """
        INSERT INTO referral_qualification_progress(
            referrer_client_id, invited_client_id, referral_code_id, source_campaign_code
        ) VALUES (?, ?, ?, ?)
        """,
        (referrer_id, invited_client_id, int(owner["id"]), campaign_code),
    )
    return {"status": "fixed", "fixed": True, "referrer_client_id": referrer_id}


def record_referral_click(
    conn: sqlite3.Connection,
    *,
    code: str,
    campaign_code: str | None,
    ip_hash: str = "",
) -> sqlite3.Row | None:
    owner = resolve_referral_code(conn, code=code, campaign_code=campaign_code)
    if not owner:
        return None
    conn.execute(
        """INSERT INTO jackside_referral_clicks(
               referral_code_id, referrer_client_id, campaign_code, ip_hash
           ) VALUES (?, ?, ?, ?)""",
        (int(owner["id"]), int(owner["client_id"]), campaign_code, ip_hash[:128]),
    )
    return owner


def _completed_issue_dates(
    conn: sqlite3.Connection, *, client_id: int, timezone_name: str
) -> list[date]:
    tz = ZoneInfo(timezone_name)
    rows = conn.execute(
        """
        SELECT qs.created_at, ji.issue_date
        FROM quiz_submissions qs
        JOIN quiz_campaigns qc ON qc.code=qs.campaign_code AND qc.campaign_type='daily_414'
        LEFT JOIN jackside_issues ji ON ji.campaign_code=qs.campaign_code
        WHERE qs.client_id=? AND IFNULL(qs.main_round_completed,1)=1
          AND qs.max_correct_count > 0
        ORDER BY qs.created_at, qs.id
        """,
        (client_id,),
    ).fetchall()
    days: set[date] = set()
    for row in rows:
        if row["issue_date"]:
            days.add(date.fromisoformat(str(row["issue_date"])))
            continue
        parsed = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        days.add(parsed.astimezone(tz).date())
    return sorted(days)


def _reward_campaign(settings: sqlite3.Row, side: str) -> dict[str, Any]:
    preference = settings[f"{side}_preference_code"]
    amount = int(settings[f"{side}_amount"] or 0)
    delivery = str(settings[f"{side}_delivery_mode"] or "automatic")
    return {
        "code": f"jackside_referral_{side}",
        "referral_enabled": 1 if preference and amount > 0 else 0,
        "referral_preference_code": preference,
        "referral_amount": amount,
        "referral_delivery_mode": delivery,
        "reward_validity_mode": "unlimited",
        "reward_validity_value": 0,
        "reward_valid_from": None,
        "reward_valid_until": None,
    }


def process_referral_qualification(
    conn: sqlite3.Connection,
    *,
    invited_client_id: int,
    submission_id: int,
    timezone_name: str,
) -> sqlite3.Row | None:
    progress = conn.execute(
        "SELECT * FROM referral_qualification_progress WHERE invited_client_id=?",
        (invited_client_id,),
    ).fetchone()
    if not progress:
        return None
    days = _completed_issue_dates(conn, client_id=invited_client_id, timezone_name=timezone_name)
    count = len(days)
    first = days[0].isoformat() if count >= 1 else None
    second = days[1].isoformat() if count >= 2 else None
    qualified_date = days[2].isoformat() if count >= QUALIFY_DAYS else None
    newly_qualified = count >= QUALIFY_DAYS and not progress["qualified_at"]
    conn.execute(
        """
        UPDATE referral_qualification_progress
        SET distinct_completed_days=?, first_completed_date=?, second_completed_date=?,
            qualified_date=COALESCE(qualified_date, ?),
            qualified_at=CASE WHEN qualified_at IS NULL AND ? IS NOT NULL THEN CURRENT_TIMESTAMP ELSE qualified_at END,
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (count, first, second, qualified_date, qualified_date, int(progress["id"])),
    )
    if newly_qualified:
        settings = conn.execute("SELECT * FROM jackside_referral_settings WHERE id=1").fetchone()
        refreshed = conn.execute(
            "SELECT * FROM referral_qualification_progress WHERE id=?", (int(progress["id"]),)
        ).fetchone()
        if settings:
            ref_reward = issue_referral_reward(
                conn,
                client_id=int(refreshed["referrer_client_id"]),
                campaign=_reward_campaign(settings, "referrer"),
                submission_id=submission_id,
                milestone=int(refreshed["invited_client_id"]),
                timezone_name=timezone_name,
            )
            invited_reward = issue_referral_reward(
                conn,
                client_id=int(refreshed["invited_client_id"]),
                campaign=_reward_campaign(settings, "invited"),
                submission_id=submission_id,
                milestone=int(refreshed["referrer_client_id"]),
                timezone_name=timezone_name,
            )
            conn.execute(
                "UPDATE referral_qualification_progress SET referrer_reward_id=?, invited_reward_id=? WHERE id=?",
                (
                    int(ref_reward["id"]) if ref_reward else None,
                    int(invited_reward["id"]) if invited_reward else None,
                    int(refreshed["id"]),
                ),
            )
        _notify(
            conn,
            client_id=int(refreshed["referrer_client_id"]),
            notification_type="referral_qualified",
            title="Реферал квалифицирован",
            body="Приглашённый игрок завершил JACKSIDE в три разные даты.",
            entity_type="referral_progress",
            entity_id=int(refreshed["id"]),
        )
        _notify(
            conn,
            client_id=int(refreshed["invited_client_id"]),
            notification_type="referral_qualified_invited",
            title="Реферальная квалификация завершена",
            body="Ты завершил JACKSIDE в три разные даты.",
            entity_type="referral_progress",
            entity_id=int(refreshed["id"]),
        )
    refresh_member_engagement(conn, client_id=invited_client_id, timezone_name=timezone_name)
    if newly_qualified:
        refresh_member_engagement(
            conn, client_id=int(progress["referrer_client_id"]), timezone_name=timezone_name
        )
    return conn.execute(
        "SELECT * FROM referral_qualification_progress WHERE id=?", (int(progress["id"]),)
    ).fetchone()


def _period_bounds(now: datetime, period_code: str, tz: ZoneInfo) -> tuple[datetime, datetime, str]:
    local = now.astimezone(tz)
    if period_code == "week":
        start_date = local.date() - timedelta(days=local.weekday())
        end_date = start_date + timedelta(days=7)
        key = f"{start_date.isoformat()}_week"
    else:
        start_date = local.date().replace(day=1)
        if start_date.month == 12:
            end_date = date(start_date.year + 1, 1, 1)
        else:
            end_date = date(start_date.year, start_date.month + 1, 1)
        key = start_date.strftime("%Y-%m")
    start = datetime.combine(start_date, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(end_date, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    return start, end, key


def _metrics(
    conn: sqlite3.Connection,
    *,
    client_id: int,
    timezone_name: str,
    period_code: str = "all_time",
    now: datetime | None = None,
) -> dict[str, int]:
    tz = ZoneInfo(timezone_name)
    now_utc = _utc(now)
    where = ""
    params: list[Any] = [client_id]
    if period_code in {"week", "month"}:
        start, end, _ = _period_bounds(now_utc, period_code, tz)
        where = " AND qs.created_at>=? AND qs.created_at<?"
        params.extend([start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")])
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS games,
               COALESCE(SUM(qs.correct_count),0) AS correct_answers,
               COALESCE(SUM(CASE WHEN qs.correct_count=qs.max_correct_count AND qs.max_correct_count>0 THEN 1 ELSE 0 END),0) AS perfect_games
        FROM quiz_submissions qs
        JOIN quiz_campaigns qc ON qc.code=qs.campaign_code AND qc.campaign_type='daily_414'
        WHERE qs.client_id=? AND IFNULL(qs.main_round_completed,1)=1 AND qs.max_correct_count>0{where}
        """,
        params,
    ).fetchone()
    final_where = ""
    final_params: list[Any] = [client_id]
    if period_code in {"week", "month"}:
        start, end, _ = _period_bounds(now_utc, period_code, tz)
        final_where = " AND COALESCE(dft.completed_at,dft.created_at)>=? AND COALESCE(dft.completed_at,dft.created_at)<?"
        final_params.extend([start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")])
    final = conn.execute(
        f"""
        SELECT COUNT(DISTINCT df.final_table_id) AS finals,
               COUNT(DISTINCT CASE WHEN df.status='winner' THEN df.final_table_id END) AS wins
        FROM daily_414_finalists df
        JOIN daily_414_final_tables dft ON dft.id=df.final_table_id
        WHERE df.client_id=?{final_where}
        """,
        final_params,
    ).fetchone()
    referral_where = ""
    referral_params: list[Any] = [client_id]
    if period_code in {"week", "month"}:
        start, end, _ = _period_bounds(now_utc, period_code, tz)
        referral_where = " AND rqp.qualified_at>=? AND rqp.qualified_at<?"
        referral_params.extend([start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")])
    qualified = conn.execute(
        f"SELECT COUNT(*) FROM referral_qualification_progress rqp WHERE rqp.referrer_client_id=? AND rqp.qualified_at IS NOT NULL{referral_where}",
        referral_params,
    ).fetchone()[0]
    progress = conn.execute(
        "SELECT current_streak,best_streak FROM daily_414_progress WHERE client_id=? ORDER BY updated_at DESC LIMIT 1",
        (client_id,),
    ).fetchone()
    return {
        "completed_games": int(row["games"] or 0),
        "correct_answers": int(row["correct_answers"] or 0),
        "perfect_games": int(row["perfect_games"] or 0),
        "finals": int(final["finals"] or 0),
        "wins": int(final["wins"] or 0),
        "qualified_referrals": int(qualified or 0),
        "best_streak": int(progress["best_streak"] or 0) if progress else 0,
        "current_streak": int(progress["current_streak"] or 0) if progress else 0,
    }


def _notify(
    conn: sqlite3.Connection,
    *,
    client_id: int,
    notification_type: str,
    title: str,
    body: str,
    entity_type: str,
    entity_id: int,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO member_notifications(
            client_id, notification_type, title, body, entity_type, entity_id
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (client_id, notification_type, title, body, entity_type, entity_id),
    )



def _grant_configured_material_reward(
    conn: sqlite3.Connection,
    *,
    client_id: int,
    definition: sqlite3.Row,
    member_achievement_id: int | None = None,
    member_title_id: int | None = None,
    timezone_name: str,
) -> int | None:
    if not int(definition["material_reward_enabled"] or 0):
        return None
    preference = str(definition["material_preference_code"] or "").strip()
    amount = int(definition["material_reward_amount"] or 0)
    if not preference or amount < 1:
        return None
    pref = conn.execute(
        "SELECT kind,is_active FROM preference_types WHERE code=?", (preference,)
    ).fetchone()
    if not pref or pref["kind"] != "counter" or not pref["is_active"]:
        return None
    if member_achievement_id is not None:
        existing = conn.execute(
            "SELECT reward_id FROM engagement_reward_grants WHERE member_achievement_id=?",
            (member_achievement_id,),
        ).fetchone()
        unique_code = f"jackside_achievement_{member_achievement_id}"
        milestone = member_achievement_id
    else:
        existing = conn.execute(
            "SELECT reward_id FROM engagement_reward_grants WHERE member_title_id=?",
            (member_title_id,),
        ).fetchone()
        unique_code = f"jackside_title_{member_title_id}"
        milestone = int(member_title_id or 0)
    if existing:
        return int(existing["reward_id"]) if existing["reward_id"] else None
    campaign = {
        "code": unique_code,
        "referral_enabled": 1,
        "referral_preference_code": preference,
        "referral_amount": amount,
        "referral_delivery_mode": "automatic",
        "reward_validity_mode": "unlimited",
        "reward_validity_value": 0,
        "reward_valid_from": None,
        "reward_valid_until": None,
    }
    try:
        reward = issue_referral_reward(
            conn,
            client_id=client_id,
            campaign=campaign,
            submission_id=0,
            milestone=milestone,
            timezone_name=timezone_name,
        )
    except (ValueError, sqlite3.Error):
        reward = None
    conn.execute(
        """INSERT OR IGNORE INTO engagement_reward_grants(
               client_id,member_achievement_id,member_title_id,reward_id
           ) VALUES (?,?,?,?)""",
        (
            client_id,
            member_achievement_id,
            member_title_id,
            int(reward["id"]) if reward else None,
        ),
    )
    return int(reward["id"]) if reward else None

def refresh_member_engagement(
    conn: sqlite3.Connection,
    *,
    client_id: int,
    timezone_name: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_utc = _utc(now)
    tz = ZoneInfo(timezone_name)
    all_metrics = _metrics(
        conn, client_id=client_id, timezone_name=timezone_name, period_code="all_time", now=now_utc
    )
    for definition in conn.execute(
        "SELECT * FROM achievement_definitions WHERE is_enabled=1 ORDER BY position,id"
    ).fetchall():
        value = int(all_metrics.get(str(definition["condition_code"]), 0))
        if value < int(definition["threshold"]):
            continue
        cursor = conn.execute(
            """INSERT OR IGNORE INTO member_achievements(client_id,achievement_id,source_json)
               VALUES (?,?,?)""",
            (
                client_id,
                int(definition["id"]),
                json.dumps({"value": value, "threshold": int(definition["threshold"])}, ensure_ascii=False),
            ),
        )
        if cursor.rowcount:
            member_achievement_id = int(cursor.lastrowid)
            _grant_configured_material_reward(
                conn, client_id=client_id, definition=definition,
                member_achievement_id=member_achievement_id,
                timezone_name=timezone_name,
            )
            _notify(
                conn,
                client_id=client_id,
                notification_type="achievement",
                title=f"Новое достижение: {definition['name']}",
                body=str(definition["description"] or ""),
                entity_type="achievement",
                entity_id=int(definition["id"]),
            )

    for definition in conn.execute(
        "SELECT * FROM title_definitions WHERE is_enabled=1 ORDER BY priority,id"
    ).fetchall():
        title_type = str(definition["title_type"])
        period_code = str(definition["period_code"])
        metrics = all_metrics
        temporary_period_id = None
        expires_at = None
        if title_type == "temporary":
            start, end, period_key = _period_bounds(now_utc, period_code, tz)
            metrics = _metrics(
                conn,
                client_id=client_id,
                timezone_name=timezone_name,
                period_code=period_code,
                now=now_utc,
            )
            conn.execute(
                """INSERT OR IGNORE INTO temporary_title_periods(
                       title_definition_id,period_key,starts_at,ends_at
                   ) VALUES (?,?,?,?)""",
                (
                    int(definition["id"]),
                    period_key,
                    start.isoformat(timespec="seconds"),
                    end.isoformat(timespec="seconds"),
                ),
            )
            temporary_period_id = int(
                conn.execute(
                    "SELECT id FROM temporary_title_periods WHERE title_definition_id=? AND period_key=?",
                    (int(definition["id"]), period_key),
                ).fetchone()[0]
            )
            expires_at = end.isoformat(timespec="seconds")
        value = int(metrics.get(str(definition["condition_code"]), 0))
        if value < int(definition["threshold"]):
            continue
        if title_type == "temporary":
            existing = conn.execute(
                "SELECT id FROM member_titles WHERE client_id=? AND title_definition_id=? AND temporary_period_id=?",
                (client_id, int(definition["id"]), temporary_period_id),
            ).fetchone()
        else:
            existing = conn.execute(
                "SELECT id FROM member_titles WHERE client_id=? AND title_definition_id=? AND temporary_period_id IS NULL",
                (client_id, int(definition["id"])),
            ).fetchone()
        if existing:
            continue
        cursor = conn.execute(
            """INSERT INTO member_titles(
                   client_id,title_definition_id,temporary_period_id,expires_at,source_json
               ) VALUES (?,?,?,?,?)""",
            (
                client_id,
                int(definition["id"]),
                temporary_period_id,
                expires_at,
                json.dumps({"value": value, "threshold": int(definition["threshold"])}, ensure_ascii=False),
            ),
        )
        member_title_id = int(cursor.lastrowid)
        _grant_configured_material_reward(
            conn, client_id=client_id, definition=definition,
            member_title_id=member_title_id, timezone_name=timezone_name,
        )
        _notify(
            conn,
            client_id=client_id,
            notification_type="title",
            title=f"Новое звание: {definition['name']}",
            body=str(definition["description"] or ""),
            entity_type="member_title",
            entity_id=int(cursor.lastrowid),
        )
    return engagement_profile(conn, client_id=client_id, timezone_name=timezone_name, now=now_utc)


def select_permanent_title(conn: sqlite3.Connection, *, client_id: int, member_title_id: int) -> None:
    row = conn.execute(
        """
        SELECT mt.id FROM member_titles mt
        JOIN title_definitions td ON td.id=mt.title_definition_id
        WHERE mt.id=? AND mt.client_id=? AND mt.temporary_period_id IS NULL
          AND td.title_type='permanent'
        """,
        (member_title_id, client_id),
    ).fetchone()
    if not row:
        raise ValueError("title_not_available")
    conn.execute("UPDATE member_titles SET selected=0 WHERE client_id=? AND temporary_period_id IS NULL", (client_id,))
    conn.execute("UPDATE member_titles SET selected=1 WHERE id=?", (member_title_id,))


def effective_title(
    conn: sqlite3.Connection, *, client_id: int, now: datetime | None = None
) -> dict[str, Any] | None:
    now_iso = _utc(now).isoformat(timespec="seconds")
    temp = conn.execute(
        """
        SELECT mt.id AS member_title_id, td.* FROM member_titles mt
        JOIN title_definitions td ON td.id=mt.title_definition_id
        WHERE mt.client_id=? AND mt.temporary_period_id IS NOT NULL
          AND mt.expires_at>? AND td.is_enabled=1
        ORDER BY td.priority DESC, mt.awarded_at DESC, mt.id DESC LIMIT 1
        """,
        (client_id, now_iso),
    ).fetchone()
    if temp:
        return {**dict(temp), "temporary": True}
    permanent = conn.execute(
        """
        SELECT mt.id AS member_title_id, td.* FROM member_titles mt
        JOIN title_definitions td ON td.id=mt.title_definition_id
        WHERE mt.client_id=? AND mt.temporary_period_id IS NULL AND td.is_enabled=1
        ORDER BY mt.selected DESC, td.priority DESC, mt.awarded_at DESC, mt.id DESC LIMIT 1
        """,
        (client_id,),
    ).fetchone()
    return {**dict(permanent), "temporary": False} if permanent else None


def referral_dashboard(conn: sqlite3.Connection, *, client_id: int) -> dict[str, Any]:
    code = ensure_jackside_referral_code(conn, client_id)
    click_count = int(
        conn.execute("SELECT COUNT(*) FROM jackside_referral_clicks WHERE referrer_client_id=?", (client_id,)).fetchone()[0]
    )
    rows = conn.execute(
        """
        SELECT rqp.*, c.first_name,c.nickname,c.username
        FROM referral_qualification_progress rqp
        JOIN clients c ON c.id=rqp.invited_client_id
        WHERE rqp.referrer_client_id=? ORDER BY rqp.created_at DESC
        """,
        (client_id,),
    ).fetchall()
    history = []
    for row in rows:
        item = dict(row)
        item["display_name"] = _display_name({**dict(row), "id": int(row["invited_client_id"])})
        history.append(item)
    return {
        "code": str(code["code"]),
        "clicks": click_count,
        "registrations": len(rows),
        "one_day": sum(1 for row in rows if int(row["distinct_completed_days"] or 0) == 1),
        "two_days": sum(1 for row in rows if int(row["distinct_completed_days"] or 0) == 2),
        "three_days": sum(1 for row in rows if int(row["distinct_completed_days"] or 0) >= 3),
        "qualified": sum(1 for row in rows if row["qualified_at"]),
        "rewards_issued": sum(
            int(bool(row["referrer_reward_id"])) + int(bool(row["invited_reward_id"])) for row in rows
        ),
        "history": history,
    }


def engagement_profile(
    conn: sqlite3.Connection,
    *,
    client_id: int,
    timezone_name: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = effective_title(conn, client_id=client_id, now=now)
    permanent = [
        dict(row)
        for row in conn.execute(
            """SELECT mt.id AS member_title_id, mt.selected, mt.awarded_at, td.*
               FROM member_titles mt JOIN title_definitions td ON td.id=mt.title_definition_id
               WHERE mt.client_id=? AND mt.temporary_period_id IS NULL
               ORDER BY mt.selected DESC, td.priority DESC, mt.awarded_at DESC""",
            (client_id,),
        ).fetchall()
    ]
    temporary = [
        dict(row)
        for row in conn.execute(
            """SELECT mt.id AS member_title_id, mt.awarded_at, mt.expires_at, td.*
               FROM member_titles mt JOIN title_definitions td ON td.id=mt.title_definition_id
               WHERE mt.client_id=? AND mt.temporary_period_id IS NOT NULL
               ORDER BY mt.awarded_at DESC""",
            (client_id,),
        ).fetchall()
    ]
    achievements = [
        dict(row)
        for row in conn.execute(
            """SELECT ma.awarded_at, ad.* FROM member_achievements ma
               JOIN achievement_definitions ad ON ad.id=ma.achievement_id
               WHERE ma.client_id=? ORDER BY ma.awarded_at DESC, ma.id DESC""",
            (client_id,),
        ).fetchall()
    ]
    notifications = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM member_notifications WHERE client_id=? ORDER BY created_at DESC,id DESC LIMIT 20",
            (client_id,),
        ).fetchall()
    ]
    active_referral_titles = [
        item for item in permanent if str(item.get("category") or "") == "referrals"
    ]
    now_iso = _utc(now).isoformat(timespec="seconds")
    active_referral_titles.extend(
        item for item in temporary
        if str(item.get("category") or "") == "referrals"
        and item.get("expires_at") and str(item["expires_at"]) > now_iso
    )
    return {
        "current_title": current,
        "permanent_titles": permanent,
        "temporary_titles": temporary,
        "active_referral_titles": active_referral_titles,
        "achievements": achievements,
        "notifications": notifications,
        "referrals": referral_dashboard(conn, client_id=client_id),
    }
