from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import prelaunch_experience, profile_experience
from app.db import connect
from app.product_shell import _current_member
from app.services import jackside_analytics, jackside_engagement


_ORIGINAL_ENGAGEMENT_PROFILE = jackside_engagement.engagement_profile
_ORIGINAL_TITLE_COLLECTION = profile_experience.title_collection_payload
_ORIGINAL_PUBLIC_PROFILE_PAYLOAD = prelaunch_experience._public_profile_payload


def _name_key(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _dedupe_named(items: list[dict[str, Any]], seen: set[str] | None = None) -> list[dict[str, Any]]:
    used = seen if seen is not None else set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = _name_key(item.get("name"))
        if key and key in used:
            continue
        if key:
            used.add(key)
        result.append(item)
    return result


def dedupe_engagement_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    seen: set[str] = set()
    permanent = _dedupe_named(list(payload.get("permanent_titles") or []), seen)
    temporary = _dedupe_named(list(payload.get("temporary_titles") or []), seen)
    achievements = _dedupe_named(list(payload.get("achievements") or []), seen)
    visible_title_names = {
        _name_key(item.get("name")) for item in [*permanent, *temporary] if _name_key(item.get("name"))
    }
    active_referral_titles = [
        item
        for item in list(payload.get("active_referral_titles") or [])
        if _name_key(item.get("name")) in visible_title_names
    ]
    result["permanent_titles"] = permanent
    result["temporary_titles"] = temporary
    result["achievements"] = achievements
    result["active_referral_titles"] = _dedupe_named(active_referral_titles)
    return result


def _engagement_profile_deduped(*args, **kwargs) -> dict[str, Any]:
    return dedupe_engagement_payload(_ORIGINAL_ENGAGEMENT_PROFILE(*args, **kwargs))


def dedupe_title_collection(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    items = [dict(item) for item in list(payload.get("items") or [])]
    # The collection is ordered with earned titles first. Keeping the first item
    # for the same visible name prevents legacy title/achievement twins from
    # appearing twice without deleting either historical database record.
    deduped = _dedupe_named(items)
    result["items"] = deduped
    result["active_count"] = sum(1 for item in deduped if item.get("state") == "active")
    result["total_count"] = len(deduped)
    return result


def _title_collection_deduped(*args, **kwargs) -> dict[str, Any]:
    return dedupe_title_collection(_ORIGINAL_TITLE_COLLECTION(*args, **kwargs))


def _public_profile_payload_deduped(*args, **kwargs):
    payload = _ORIGINAL_PUBLIC_PROFILE_PAYLOAD(*args, **kwargs)
    if not payload or payload.get("restricted"):
        return payload
    result = dict(payload)
    titles = _dedupe_named([dict(item) for item in list(payload.get("titles") or [])])
    title_names = {_name_key(item.get("name")) for item in titles if _name_key(item.get("name"))}
    achievements = [
        dict(item)
        for item in list(payload.get("achievements") or [])
        if _name_key(item.get("name")) not in title_names
    ]
    result["titles"] = titles
    result["achievements"] = _dedupe_named(achievements)
    result["selected_title"] = next((item for item in titles if item.get("selected")), None)
    return result


def _submission_day(row: dict[str, Any], tz: ZoneInfo) -> date:
    issue_date = str(row.get("issue_date") or "").strip()
    if issue_date:
        return date.fromisoformat(issue_date)
    return jackside_analytics._local_date(row.get("created_at"), tz)


def _month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    if start.month == 12:
        return start, date(start.year + 1, 1, 1)
    return start, date(start.year, start.month + 1, 1)


def _calendar_month_rating(submissions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for row in submissions:
        client_id = int(row["client_id"])
        item = grouped.setdefault(
            client_id,
            {
                "client_id": client_id,
                "display_name": jackside_analytics._display_name(row),
                "completed_count": 0,
                "active_dates": set(),
                "correct_total": 0,
                "question_total": 0,
                "last_result_at": "",
            },
        )
        item["completed_count"] += 1
        item["active_dates"].add(str(row.get("issue_date") or row.get("created_at") or "")[:10])
        item["correct_total"] += int(row.get("correct_count") or 0)
        item["question_total"] += int(row.get("max_correct_count") or 0)
        item["last_result_at"] = max(
            str(item["last_result_at"]), str(row.get("created_at") or "")
        )

    ranked: list[dict[str, Any]] = []
    calibrating: list[dict[str, Any]] = []
    for item in grouped.values():
        completed_count = int(item["completed_count"])
        question_total = int(item["question_total"])
        correct_total = int(item["correct_total"])
        active_days = len(item.pop("active_dates"))
        confirmed_accuracy = jackside_analytics._wilson_lower_bound(correct_total, question_total)
        activity_score = min(completed_count / jackside_analytics.FULL_ACTIVITY_GAMES, 1) * 100
        rating_score = (
            confirmed_accuracy * jackside_analytics.ACCURACY_WEIGHT
            + activity_score * jackside_analytics.ACTIVITY_WEIGHT
        )
        is_calibrating = (
            completed_count < jackside_analytics.MIN_RATED_GAMES
            or question_total < jackside_analytics.MIN_RATED_ANSWERS
        )
        entry = {
            **item,
            "active_days": active_days,
            "accuracy": jackside_analytics._percent(correct_total, question_total),
            "confirmed_accuracy": jackside_analytics._round_number(confirmed_accuracy),
            "activity_score": jackside_analytics._round_number(activity_score),
            "rating_score": jackside_analytics._round_number(rating_score),
            "status": "calibration" if is_calibrating else "ranked",
            "calibration_games_left": max(0, jackside_analytics.MIN_RATED_GAMES - completed_count),
            "calibration_answers_left": max(0, jackside_analytics.MIN_RATED_ANSWERS - question_total),
            "calibration_progress": round(
                min(
                    completed_count / jackside_analytics.MIN_RATED_GAMES,
                    question_total / jackside_analytics.MIN_RATED_ANSWERS,
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


def calendar_jackside_rating_payload(
    conn,
    *,
    client_id: int,
    period: str,
    as_of: datetime | str | None = None,
    timezone_name: str = "Europe/Moscow",
) -> dict[str, Any]:
    if period not in {"month", "year"}:
        raise ValueError("unknown_calendar_rating_period")
    now_utc = jackside_analytics._as_utc(as_of)
    tz = ZoneInfo(timezone_name)
    today = now_utc.astimezone(tz).date()
    submissions = jackside_analytics._completed_submissions(conn)

    if period == "month":
        start, end = _month_bounds(today)
        filtered = [row for row in submissions if start <= _submission_day(row, tz) < end]
        rows = _calendar_month_rating(filtered)
        label = start.strftime("%m.%Y")
    else:
        start = date(today.year, 1, 1)
        end = date(today.year + 1, 1, 1)
        filtered = [row for row in submissions if start <= _submission_day(row, tz) < end]
        submission_ids = {int(row["id"]) for row in filtered}
        finals = [
            row
            for row in jackside_analytics._final_rows(conn)
            if int(row["submission_id"]) in submission_ids
        ]
        rows = jackside_analytics._all_time_rating(filtered, finals, {}, today=today)
        label = str(today.year)

    me = next((row for row in rows if int(row["client_id"]) == int(client_id)), None)
    return {
        "period": period,
        "label": label,
        "rows": rows,
        "me": me,
        "total": len(rows),
        "source_rows": len(filtered),
        "stored_source_rows": len(submissions),
    }


def install_prelaunch_data_integrity(app: FastAPI) -> FastAPI:
    if getattr(app.state, "prelaunch_data_integrity_installed", False):
        return app
    app.state.prelaunch_data_integrity_installed = True
    settings = app.state.settings

    jackside_engagement.engagement_profile = _engagement_profile_deduped
    profile_experience.title_collection_payload = _title_collection_deduped
    prelaunch_experience.title_collection_payload = _title_collection_deduped
    prelaunch_experience._public_profile_payload = _public_profile_payload_deduped

    @app.get("/api/account/jackside-calendar-rating")
    async def account_jackside_calendar_rating(request: Request, period: str = "month"):
        member = _current_member(request, required=True)
        try:
            with connect(settings.db_path) as conn:
                payload = calendar_jackside_rating_payload(
                    conn,
                    client_id=int(member["client_id"]),
                    period=str(period or "month"),
                    timezone_name=settings.timezone_name,
                )
        except ValueError:
            return JSONResponse({"error": "unknown_period"}, status_code=400)
        return JSONResponse(payload)

    return app


__all__ = [
    "calendar_jackside_rating_payload",
    "dedupe_engagement_payload",
    "dedupe_title_collection",
    "install_prelaunch_data_integrity",
]
