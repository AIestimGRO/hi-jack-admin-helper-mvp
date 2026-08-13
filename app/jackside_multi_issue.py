from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.db import connect, transaction
from app.product_shell import _check_csrf, _require_master
from app.services import jackside_issues as issue_service
from app.services.daily_414 import DAILY_414_TIME_LIMIT_SECONDS


_MULTI_ISSUE_TABLE = """
CREATE TABLE jackside_issues_multi (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_date TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK(status IN (
            'draft', 'scheduled', 'lobby', 'main_live', 'waiting_final',
            'final_live', 'closed', 'cancelled', 'technical_review'
        )),
    campaign_code TEXT UNIQUE,
    rules_version_id INTEGER REFERENCES jackside_rules_versions(id),
    rules_version TEXT,
    starts_at TEXT,
    ends_at TEXT,
    main_question_count INTEGER NOT NULL DEFAULT 0,
    final_question_count INTEGER NOT NULL DEFAULT 0,
    jackcoin_per_correct INTEGER NOT NULL DEFAULT 5,
    jackcoin_completion_bonus INTEGER NOT NULL DEFAULT 10,
    jackcoin_perfect_bonus INTEGER NOT NULL DEFAULT 20,
    final_question_time_seconds INTEGER NOT NULL DEFAULT 30,
    final_prize_type TEXT NOT NULL DEFAULT 'none'
        CHECK(final_prize_type IN ('none', 'reward_card', 'jackcoin')),
    final_prize_catalog_reward_id INTEGER REFERENCES vault_catalog_rewards(id) ON DELETE SET NULL,
    final_prize_jackcoin_amount INTEGER NOT NULL DEFAULT 0,
    unique_participants INTEGER NOT NULL DEFAULT 0,
    results_json TEXT NOT NULL DEFAULT '{}',
    published_at TEXT,
    created_by_admin_id INTEGER REFERENCES admins(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_ISSUE_COLUMNS = (
    "id",
    "issue_date",
    "title",
    "status",
    "campaign_code",
    "rules_version_id",
    "rules_version",
    "starts_at",
    "ends_at",
    "main_question_count",
    "final_question_count",
    "jackcoin_per_correct",
    "jackcoin_completion_bonus",
    "jackcoin_perfect_bonus",
    "final_question_time_seconds",
    "final_prize_type",
    "final_prize_catalog_reward_id",
    "final_prize_jackcoin_amount",
    "unique_participants",
    "results_json",
    "published_at",
    "created_by_admin_id",
    "created_at",
    "updated_at",
)


def _has_unique_issue_date(conn: sqlite3.Connection) -> bool:
    for index in conn.execute("PRAGMA index_list('jackside_issues')").fetchall():
        if not int(index[2]):
            continue
        name = str(index[1])
        columns = [str(row[2]) for row in conn.execute(f"PRAGMA index_info('{name}')")]
        if columns == ["issue_date"]:
            return True
    return False


def ensure_multi_issue_schema(db_path: str | Path) -> bool:
    """Remove only the legacy one-release-per-date constraint, preserving IDs/data."""
    path = Path(db_path)
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        table = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='jackside_issues'"
        ).fetchone()
        if not table:
            return False
        if not _has_unique_issue_date(conn):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_jackside_issues_date_start "
                "ON jackside_issues(issue_date, starts_at, id)"
            )
            conn.commit()
            return False

        saved_indexes = [
            str(row[0])
            for row in conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND tbl_name='jackside_issues' AND sql IS NOT NULL"
            ).fetchall()
            if row[0]
        ]
        saved_triggers = [
            str(row[0])
            for row in conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='trigger' AND tbl_name='jackside_issues' AND sql IS NOT NULL"
            ).fetchall()
            if row[0]
        ]
        columns_sql = ", ".join(_ISSUE_COLUMNS)

        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DROP TABLE IF EXISTS jackside_issues_multi")
            conn.execute(_MULTI_ISSUE_TABLE)
            conn.execute(
                f"INSERT INTO jackside_issues_multi({columns_sql}) "
                f"SELECT {columns_sql} FROM jackside_issues"
            )
            source_count = int(conn.execute("SELECT COUNT(*) FROM jackside_issues").fetchone()[0])
            copy_count = int(
                conn.execute("SELECT COUNT(*) FROM jackside_issues_multi").fetchone()[0]
            )
            if source_count != copy_count:
                raise RuntimeError("jackside_issue_migration_count_mismatch")

            conn.execute("DROP TABLE jackside_issues")
            conn.execute("ALTER TABLE jackside_issues_multi RENAME TO jackside_issues")
            for statement in saved_indexes:
                conn.execute(statement)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_jackside_issues_date_start "
                "ON jackside_issues(issue_date, starts_at, id)"
            )
            for statement in saved_triggers:
                conn.execute(statement)

            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"jackside_issue_migration_fk_violation:{violations!r}")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"jackside_issue_migration_integrity:{integrity}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
        return True
    finally:
        conn.close()


def _campaign_code_exists(conn: sqlite3.Connection, code: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM quiz_campaigns WHERE code=? "
            "UNION ALL SELECT 1 FROM jackside_issues WHERE campaign_code=? LIMIT 1",
            (code, code),
        ).fetchone()
    )


def next_issue_campaign_code(
    conn: sqlite3.Connection,
    *,
    issue_date_value: date,
    starts_at: datetime,
    timezone_name: str = "Europe/Moscow",
) -> str:
    base = issue_service.issue_campaign_code(issue_date_value)
    if not _campaign_code_exists(conn, base):
        return base
    local_start = starts_at
    club_tz = ZoneInfo(timezone_name)
    if local_start.tzinfo is None:
        local_start = local_start.replace(tzinfo=club_tz)
    else:
        local_start = local_start.astimezone(club_tz)
    suffix = local_start.strftime("%H%M")
    candidate = issue_service.issue_campaign_code(issue_date_value, suffix=suffix)
    if not _campaign_code_exists(conn, candidate):
        return candidate
    serial = 2
    while serial < 1000:
        candidate = issue_service.issue_campaign_code(
            issue_date_value, suffix=f"{suffix}_{serial}"
        )
        if not _campaign_code_exists(conn, candidate):
            return candidate
        serial += 1
    raise RuntimeError("jackside_campaign_code_exhausted")


def create_issue_multi(
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
    rules = issue_service.ensure_default_rules(conn)
    code = next_issue_campaign_code(
        conn,
        issue_date_value=issue_date_value,
        starts_at=starts_at,
        timezone_name=timezone_name,
    )
    if final_prize_type == "none" and final_prize_catalog_reward_id:
        final_prize_type = "reward_card"
    clean_title = (title or f"JACKSIDE {issue_date_value.isoformat()}").strip()
    starts = issue_service._as_utc(starts_at)
    club_tz = ZoneInfo(timezone_name)
    ends = datetime.combine(
        issue_date_value,
        datetime.max.time().replace(microsecond=0),
        tzinfo=club_tz,
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
            issue_service._timestamp(starts),
            issue_service._timestamp(ends),
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


def get_issue_by_date_multi(
    conn: sqlite3.Connection, issue_date_value: date
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM jackside_issues WHERE issue_date=? "
        "ORDER BY starts_at, id LIMIT 1",
        (issue_date_value.isoformat(),),
    ).fetchone()


def _featured_key(status: str, starts_at: datetime | None, now: datetime, issue_id: int) -> tuple:
    priority = {
        "main_live": 0,
        "final_live": 0,
        "waiting_final": 1,
        "lobby": 2,
        "scheduled": 3,
    }.get(status, 50)
    if starts_at is None:
        distance = float("inf")
        stamp = float("inf")
    else:
        distance = abs((starts_at - now).total_seconds())
        stamp = starts_at.timestamp()
    return (priority, distance, stamp, issue_id)


def current_featured_issue_multi(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    timezone_name: str = "Europe/Moscow",
) -> dict[str, Any] | None:
    now_utc = issue_service._as_utc(now)
    candidates: list[tuple[tuple, dict[str, Any]]] = []
    for issue in issue_service.list_issues(conn, limit=100):
        if str(issue["status"] or "") == "draft":
            continue
        campaign = conn.execute(
            "SELECT * FROM quiz_campaigns WHERE code=?",
            (issue["campaign_code"],),
        ).fetchone()
        if not campaign:
            continue
        final_table = conn.execute(
            """
            SELECT * FROM daily_414_final_tables
            WHERE campaign_code=? AND campaign_version=?
            """,
            (issue["campaign_code"], int(campaign["current_version"] or 1)),
        ).fetchone()
        status = issue_service.compute_issue_status(
            issue,
            now=now_utc,
            final_table=final_table,
            timezone_name=timezone_name,
        )
        if status not in {"main_live", "final_live", "waiting_final", "lobby", "scheduled"}:
            continue
        starts = issue_service._parse_dt(str(issue["starts_at"]) if issue["starts_at"] else None)
        payload = issue_service.resolve_issue_for_campaign(
            conn, campaign, now=now_utc, timezone_name=timezone_name
        )
        candidates.append(
            (_featured_key(status, starts, now_utc, int(issue["id"])), payload)
        )
    if candidates:
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    legacy_candidates: list[tuple[tuple, dict[str, Any]]] = []
    campaigns = conn.execute(
        """
        SELECT * FROM quiz_campaigns
        WHERE campaign_type='daily_414' AND is_active=1
          AND archived_at IS NULL AND deleted_at IS NULL
        """
    ).fetchall()
    for campaign in campaigns:
        payload = issue_service.legacy_issue_from_campaign(
            conn, campaign, now=now_utc, timezone_name=timezone_name
        )
        status = str(payload.get("status") or "")
        if status not in {"main_live", "final_live", "waiting_final", "lobby", "scheduled"}:
            continue
        raw_start = payload.get("starts_at")
        starts = None
        if raw_start:
            parsed = datetime.fromisoformat(str(raw_start).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
            starts = parsed.astimezone(timezone.utc)
        legacy_candidates.append(
            (_featured_key(status, starts, now_utc, int(campaign["id"])), payload)
        )
    if legacy_candidates:
        legacy_candidates.sort(key=lambda item: item[0])
        return legacy_candidates[0][1]
    return None


def apply_service_overrides() -> None:
    issue_service.create_issue = create_issue_multi
    issue_service.get_issue_by_date = get_issue_by_date_multi
    issue_service.current_featured_issue = current_featured_issue_multi


def same_day_issues(
    conn: sqlite3.Connection,
    *,
    issue_date_value: date,
    exclude_issue_id: int | None = None,
) -> list[dict[str, Any]]:
    where = "issue_date=? AND status<>'cancelled'"
    params: list[Any] = [issue_date_value.isoformat()]
    if exclude_issue_id is not None:
        where += " AND id<>?"
        params.append(int(exclude_issue_id))
    rows = conn.execute(
        f"""
        SELECT id, issue_date, title, status, starts_at, campaign_code
        FROM jackside_issues
        WHERE {where}
        ORDER BY starts_at, id
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _redirect(message: str, *, error: bool = False) -> RedirectResponse:
    key = "error" if error else "ok"
    return RedirectResponse(
        f"/master/jackside?{urlencode({key: message})}", status_code=303
    )


def _local_start(value: str, timezone_name: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    tz = ZoneInfo(timezone_name)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _conflict_message(rows: list[dict[str, Any]], timezone_name: str) -> str:
    tz = ZoneInfo(timezone_name)
    labels: list[str] = []
    for row in rows[:4]:
        raw = row.get("starts_at")
        time_label = "без времени"
        if raw:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            time_label = parsed.astimezone(tz).strftime("%H:%M")
        labels.append(f"{time_label} — {row.get('title') or 'JACKSIDE'}")
    return "; ".join(labels)


def reschedule_future_issue(
    conn: sqlite3.Connection,
    *,
    issue_id: int,
    issue_date_value: date,
    starts_at: datetime,
    title: str | None,
    timezone_name: str,
) -> sqlite3.Row:
    issue = issue_service.get_issue(conn, int(issue_id))
    if not issue:
        raise ValueError("issue_not_found")
    old_start = issue_service._parse_dt(str(issue["starts_at"]) if issue["starts_at"] else None)
    now_utc = datetime.now(timezone.utc)
    if not old_start or old_start <= now_utc:
        raise ValueError("issue_already_started")
    if str(issue["status"] or "") not in {"draft", "scheduled", "lobby"}:
        raise ValueError("issue_not_editable")

    tz = ZoneInfo(timezone_name)
    local_start = starts_at.astimezone(tz) if starts_at.tzinfo else starts_at.replace(tzinfo=tz)
    if local_start.date() != issue_date_value:
        raise ValueError("issue_date_start_mismatch")
    new_start_utc = local_start.astimezone(timezone.utc)
    if new_start_utc <= now_utc:
        raise ValueError("issue_start_in_past")
    ends = datetime.combine(
        issue_date_value,
        datetime.max.time().replace(microsecond=0),
        tzinfo=tz,
    ).astimezone(timezone.utc)
    next_status = "draft" if str(issue["status"]) == "draft" else "scheduled"
    next_title = str(title or issue["title"] or "JACKSIDE").strip()[:100] or "JACKSIDE"
    conn.execute(
        """
        UPDATE jackside_issues
        SET issue_date=?, title=?, starts_at=?, ends_at=?, status=?,
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (
            issue_date_value.isoformat(),
            next_title,
            issue_service._timestamp(new_start_utc),
            issue_service._timestamp(ends),
            next_status,
            int(issue_id),
        ),
    )
    updated = issue_service.get_issue(conn, int(issue_id))
    issue_service.sync_campaign_schedule_from_issue(
        conn, updated, timezone_name=timezone_name
    )
    conn.execute(
        "UPDATE quiz_campaigns SET title=?, updated_at=CURRENT_TIMESTAMP WHERE code=?",
        (next_title, updated["campaign_code"]),
    )
    deadline = new_start_utc + timedelta(seconds=DAILY_414_TIME_LIMIT_SECONDS)
    conn.execute(
        """
        UPDATE quiz_attempts
        SET attempt_deadline_at=?
        WHERE campaign_code=? AND status='in_progress'
        """,
        (deadline.isoformat(timespec="milliseconds"), updated["campaign_code"]),
    )
    return updated


def install_jackside_multi_issue(app: FastAPI) -> FastAPI:
    if getattr(app.state, "jackside_multi_issue_installed", False):
        return app
    app.state.jackside_multi_issue_installed = True
    settings = app.state.settings

    @app.get("/api/master/jackside/date-conflicts")
    async def date_conflicts(
        request: Request,
        issue_date: str = Query(...),
        exclude_issue_id: int | None = Query(None),
    ):
        _require_master(request)
        try:
            day = date.fromisoformat(issue_date)
        except ValueError:
            return JSONResponse({"error": "invalid_date"}, status_code=422)
        with connect(settings.db_path) as conn:
            rows = same_day_issues(
                conn, issue_date_value=day, exclude_issue_id=exclude_issue_id
            )
        tz = ZoneInfo(settings.timezone_name)
        for row in rows:
            raw = row.get("starts_at")
            if raw:
                parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                row["starts_at_local"] = parsed.astimezone(tz).strftime("%Y-%m-%dT%H:%M")
        return JSONResponse({"issues": rows})

    @app.get("/api/master/jackside/issues/{issue_id}/schedule")
    async def issue_schedule(request: Request, issue_id: int):
        _require_master(request)
        with connect(settings.db_path) as conn:
            issue = issue_service.get_issue(conn, int(issue_id))
            if not issue:
                return JSONResponse({"error": "issue_not_found"}, status_code=404)
            participants = int(
                conn.execute(
                    "SELECT COUNT(*) FROM jackside_issue_participants WHERE issue_id=?",
                    (int(issue_id),),
                ).fetchone()[0]
            )
        starts = issue_service._parse_dt(str(issue["starts_at"]) if issue["starts_at"] else None)
        now_utc = datetime.now(timezone.utc)
        editable = bool(
            starts
            and starts > now_utc
            and str(issue["status"] or "") in {"draft", "scheduled", "lobby"}
        )
        local_value = ""
        if starts:
            local_value = starts.astimezone(ZoneInfo(settings.timezone_name)).strftime(
                "%Y-%m-%dT%H:%M"
            )
        return JSONResponse(
            {
                "issue": {
                    "id": int(issue["id"]),
                    "issue_date": str(issue["issue_date"]),
                    "title": str(issue["title"] or ""),
                    "status": str(issue["status"] or ""),
                    "starts_at_local": local_value,
                    "participants": participants,
                    "editable": editable,
                },
                "csrf_token": str(request.session.get("csrf") or ""),
            }
        )

    @app.post("/api/master/jackside/create-release-v2")
    async def create_release_v2(
        request: Request,
        issue_date: str = Form(...),
        starts_at: str = Form(...),
        title: str = Form(""),
        source: str = Form(""),
        confirm_same_day: str = Form("0"),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        try:
            day = date.fromisoformat(issue_date)
            starts_local = _local_start(starts_at, settings.timezone_name)
        except ValueError:
            return _redirect("Некорректная дата или время", error=True)
        if starts_local.date() != day:
            return _redirect("Дата старта должна совпадать с датой выпуска", error=True)
        if starts_local.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            return _redirect("Время старта должно быть в будущем", error=True)

        source = str(source or "").strip()
        scheduled = False
        schedule_warning = ""
        try:
            with transaction(settings.db_path) as conn:
                conflicts = same_day_issues(conn, issue_date_value=day)
                if conflicts and not _truthy(confirm_same_day):
                    raise ValueError("same_day_confirmation_required")

                if source.startswith("issue:"):
                    issue = issue_service.copy_issue(
                        conn,
                        source_issue_id=int(source.split(":", 1)[1]),
                        issue_date_value=day,
                        starts_at=starts_local,
                        admin_id=request.session.get("admin_id"),
                        timezone_name=settings.timezone_name,
                    )
                elif source.startswith("legacy:"):
                    from app.legacy_jackside_copy import copy_legacy_campaign_to_issue

                    issue = copy_legacy_campaign_to_issue(
                        conn,
                        source_campaign_id=int(source.split(":", 1)[1]),
                        issue_date_value=day,
                        starts_at=starts_local,
                        admin_id=request.session.get("admin_id"),
                        timezone_name=settings.timezone_name,
                    )
                elif source:
                    raise ValueError("invalid_source")
                else:
                    issue = create_issue_multi(
                        conn,
                        issue_date_value=day,
                        starts_at=starts_local,
                        title=title.strip() or None,
                        admin_id=request.session.get("admin_id"),
                        timezone_name=settings.timezone_name,
                    )
                if title.strip():
                    conn.execute(
                        "UPDATE jackside_issues SET title=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (title.strip()[:100], int(issue["id"])),
                    )
                    issue = issue_service.get_issue(conn, int(issue["id"]))
                campaign = issue_service.ensure_issue_campaign(
                    conn, issue=issue, timezone_name=settings.timezone_name
                )
                if title.strip():
                    conn.execute(
                        "UPDATE quiz_campaigns SET title=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (title.strip()[:100], int(campaign["id"])),
                    )
                if source:
                    try:
                        issue_service.schedule_issue(
                            conn,
                            issue_id=int(issue["id"]),
                            timezone_name=settings.timezone_name,
                        )
                        scheduled = True
                    except ValueError as exc:
                        schedule_warning = str(exc)
        except (ValueError, sqlite3.IntegrityError) as exc:
            messages = {
                "same_day_confirmation_required": (
                    "На эту дату уже есть JACKSIDE. Подтвердите создание ещё одного выпуска."
                ),
                "issue_not_found": "Исходный выпуск не найден",
                "legacy_campaign_not_found": "Старый квиз не найден",
                "legacy_campaign_has_no_questions": "В старом квизе нет активных вопросов",
                "invalid_source": "Неизвестный источник копирования",
                "issue_end_before_start": "Время старта выходит за дату выпуска",
            }
            return _redirect(messages.get(str(exc), str(exc)), error=True)

        if scheduled:
            return _redirect(
                f"Создан и запланирован JACKSIDE на {day.strftime('%d.%m.%Y')} "
                f"в {starts_local.strftime('%H:%M')}"
            )
        if source and schedule_warning:
            return _redirect(
                "Черновик создан, но не запланирован: " + schedule_warning,
                error=True,
            )
        return _redirect(
            f"Создан черновик JACKSIDE на {day.strftime('%d.%m.%Y')}"
        )

    @app.post("/api/master/jackside/issues/{issue_id}/reschedule")
    async def reschedule_issue_route(
        request: Request,
        issue_id: int,
        issue_date: str = Form(...),
        starts_at: str = Form(...),
        title: str = Form(""),
        confirm_same_day: str = Form("0"),
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        try:
            day = date.fromisoformat(issue_date)
            starts_local = _local_start(starts_at, settings.timezone_name)
        except ValueError:
            return _redirect("Некорректная дата или время", error=True)
        try:
            with transaction(settings.db_path) as conn:
                conflicts = same_day_issues(
                    conn, issue_date_value=day, exclude_issue_id=int(issue_id)
                )
                if conflicts and not _truthy(confirm_same_day):
                    raise ValueError("same_day_confirmation_required")
                updated = reschedule_future_issue(
                    conn,
                    issue_id=int(issue_id),
                    issue_date_value=day,
                    starts_at=starts_local,
                    title=title,
                    timezone_name=settings.timezone_name,
                )
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_audit_log'"
                ).fetchone():
                    conn.execute(
                        """
                        INSERT INTO admin_audit_log(
                            admin_id,admin_name,action,entity_type,entity_id,details
                        ) VALUES (?,?,?,?,?,?)
                        """,
                        (
                            request.session.get("admin_id"),
                            str(request.session.get("admin_name") or "Master"),
                            "reschedule_jackside_issue",
                            "jackside_issue",
                            int(issue_id),
                            (
                                '{"issue_date":"'
                                + str(updated["issue_date"])
                                + '","starts_at":"'
                                + str(updated["starts_at"])
                                + '"}'
                            ),
                        ),
                    )
        except ValueError as exc:
            messages = {
                "same_day_confirmation_required": (
                    "На эту дату уже есть JACKSIDE. Подтвердите перенос на тот же день."
                ),
                "issue_not_found": "Выпуск не найден",
                "issue_already_started": "Старт этого выпуска уже наступил — расписание заблокировано",
                "issue_not_editable": "Этот выпуск уже нельзя переносить",
                "issue_date_start_mismatch": "Дата выпуска и дата старта должны совпадать",
                "issue_start_in_past": "Новое время старта должно быть в будущем",
            }
            return _redirect(messages.get(str(exc), str(exc)), error=True)
        return _redirect(
            f"Расписание «{updated['title']}» изменено: "
            f"{day.strftime('%d.%m.%Y')} {starts_local.strftime('%H:%M')}"
        )

    return app


# Import-time override is intentional: app.main imports this module before main_impl
# and other extensions bind JACKSIDE service functions.
apply_service_overrides()


__all__ = [
    "apply_service_overrides",
    "create_issue_multi",
    "current_featured_issue_multi",
    "ensure_multi_issue_schema",
    "get_issue_by_date_multi",
    "install_jackside_multi_issue",
    "next_issue_campaign_code",
    "reschedule_future_issue",
    "same_day_issues",
]
