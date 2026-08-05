"""Reproducible concurrent load scenario for JACKSIDE daily_414 participants."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import BASE_DIR, Settings
from app.db import connect, init_db, transaction
from app.main import create_app
from app.services.auth import bootstrap_master
from app.services.daily_414 import DAILY_414_TIME_LIMIT_SECONDS
from app.services.member_accounts import MEMBER_COOKIE_NAME, hash_password, issue_session
from app.services.quiz import seed_questions_from_json

CAMPAIGN_CODE = "load_daily_414"
STANDARD_USER_COUNTS = (50, 100, 300, 500)


class ClientIPASGI:
    """Rewrite ASGI client host from X-Forwarded-For so IP rate limits stay per-user."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {key: value for key, value in scope.get("headers", [])}
            raw = headers.get(b"x-forwarded-for", b"127.0.0.1").decode("latin-1")
            ip = raw.split(",")[0].strip() or "127.0.0.1"
            scope = dict(scope)
            scope["client"] = (ip, 0)
        await self.app(scope, receive, send)


def percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (len(sorted_values) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    weight = rank - low
    return float(sorted_values[low] * (1 - weight) + sorted_values[high] * weight)


def is_database_lock_error(text: str) -> bool:
    lowered = text.lower()
    return "database is locked" in lowered or "database is busy" in lowered or "sqlite_busy" in lowered


def seed_load_world(
    settings: Settings,
    *,
    users: int,
) -> list[dict[str, Any]]:
    """Create active daily_414 campaign, questions, and member sessions."""
    local_now = datetime.now(ZoneInfo(settings.timezone_name)).replace(tzinfo=None)
    active_from = (local_now - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S")
    password_hash = hash_password("loadpass1")
    members: list[dict[str, Any]] = []

    with transaction(settings.db_path) as conn:
        conn.execute(
            """
            INSERT INTO quiz_campaigns(
                code, title, campaign_type, quiz_time_limit_seconds,
                max_attempts, verification_required, current_version, is_active,
                active_from, active_until, welcome_kicker, welcome_text,
                start_button_text, jackcoin_per_correct, jackcoin_completion_bonus,
                jackcoin_perfect_bonus, final_prize_type, final_prize_jackcoin_amount,
                final_question_time_seconds
            ) VALUES (
                ?, 'JACKSIDE Load 4:14', 'daily_414', ?, 1, 1, 1, 1,
                ?, NULL, 'JACKSIDE 4:14',
                '10 вопросов. Одна попытка.',
                'ПОСМОТРЕТЬ ПРИЗ ДНЯ', 5, 10, 20, 'jackcoin', 500, 30
            )
            """,
            (CAMPAIGN_CODE, DAILY_414_TIME_LIMIT_SECONDS, active_from),
        )
        for index in range(1, 11):
            question_id = int(
                conn.execute(
                    """
                    INSERT INTO quiz_questions(
                        campaign_code, code, type, title, required, points,
                        position, is_active, game_round
                    ) VALUES (?, ?, 'single_choice', ?, 1, 1, ?, 1, 'main')
                    """,
                    (CAMPAIGN_CODE, f"m{index}", f"Вопрос {index}", index * 10),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO quiz_options(
                    question_id, code, text, is_correct, position
                ) VALUES (?, ?, 'Верно', 1, 10), (?, ?, 'Неверно', 0, 20)
                """,
                (
                    question_id,
                    f"m{index}_yes",
                    question_id,
                    f"m{index}_no",
                ),
            )
        for index in range(1, 3):
            final_id = int(
                conn.execute(
                    """
                    INSERT INTO quiz_questions(
                        campaign_code, code, type, title, game_round,
                        required, points, position, is_active, time_limit_seconds
                    ) VALUES (?, ?, 'single_choice', ?, 'final', 1, 1, ?, 1, 30)
                    """,
                    (CAMPAIGN_CODE, f"f{index}", f"Финал {index}", index * 10),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO quiz_options(
                    question_id, code, text, is_correct, position
                ) VALUES (?, ?, 'Верно', 1, 10), (?, ?, 'Неверно', 0, 20)
                """,
                (
                    final_id,
                    f"f{index}_yes",
                    final_id,
                    f"f{index}_no",
                ),
            )

        documents = list(
            conn.execute("SELECT * FROM legal_documents WHERE is_active=1").fetchall()
        )
        for user_index in range(users):
            phone_local = f"900{user_index:07d}"
            email = f"load{user_index:04d}@load.test"
            client_id = int(
                conn.execute(
                    """
                    INSERT INTO clients(
                        first_name, phone_raw, phone_full, phone_local, source
                    ) VALUES (?, ?, ?, ?, 'load_harness')
                    """,
                    (
                        f"Load {user_index}",
                        f"+7 {phone_local[:3]} {phone_local[3:6]}-{phone_local[6:]}",
                        f"7{phone_local}",
                        phone_local,
                    ),
                ).lastrowid
            )
            account_id = int(
                conn.execute(
                    """
                    INSERT INTO member_accounts(
                        client_id, email, email_normalized, password_hash,
                        email_verified_at
                    ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (client_id, email, email, password_hash),
                ).lastrowid
            )
            for document in documents:
                conn.execute(
                    """
                    INSERT INTO member_consents(
                        account_id, document_id, document_code, document_version,
                        ip_hash, user_agent
                    ) VALUES (?, ?, ?, ?, 'load-ip', 'jackside-load')
                    """,
                    (
                        account_id,
                        document["id"],
                        document["code"],
                        document["version"],
                    ),
                )
            token = issue_session(
                conn,
                secret_key=settings.secret_key,
                account_id=account_id,
                session_version=1,
                days=1,
                ip_hash=f"load-{user_index}",
                user_agent="jackside-load",
            )
            members.append(
                {
                    "index": user_index,
                    "client_id": client_id,
                    "account_id": account_id,
                    "phone": phone_local,
                    "token": token,
                    "ip": f"10.66.{(user_index // 250) % 250}.{(user_index % 250) + 1}",
                }
            )
    return members


async def run_user_journey(
    client: httpx.AsyncClient,
    member: dict[str, Any],
    *,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    started = time.perf_counter()
    errors: list[str] = []
    headers = {
        "Cookie": f"{MEMBER_COOKIE_NAME}={member['token']}",
        "X-Forwarded-For": member["ip"],
    }
    jackcoin_awarded = None
    final_state = None

    async with semaphore:
        try:
            started_resp = await client.post(
                "/api/quiz/start",
                json={
                    "campaign": CAMPAIGN_CODE,
                    "phone": member["phone"],
                    "username": f"load_{member['index']}",
                    "name": f"Load {member['index']}",
                },
                headers=headers,
            )
            if started_resp.status_code != 200:
                errors.append(
                    f"start:{started_resp.status_code}:{started_resp.text[:240]}"
                )
                return _journey_result(
                    member, started, errors, jackcoin_awarded, final_state
                )
            payload = started_resp.json()
            attempt_token = payload["attempt_token"]
            for question in payload["questions"]:
                answer = question["options"][0]["id"]
                answer_resp = await client.post(
                    "/api/quiz/answer",
                    json={
                        "attempt_token": attempt_token,
                        "question_id": question["id"],
                        "answer": answer,
                    },
                    headers=headers,
                )
                if answer_resp.status_code != 200:
                    errors.append(
                        f"answer:{question['id']}:{answer_resp.status_code}:"
                        f"{answer_resp.text[:240]}"
                    )
                    return _journey_result(
                        member, started, errors, jackcoin_awarded, final_state
                    )

            finish_resp = await client.post(
                "/api/quiz/finish",
                json={"attempt_token": attempt_token},
                headers=headers,
            )
            if finish_resp.status_code != 200:
                errors.append(
                    f"finish:{finish_resp.status_code}:{finish_resp.text[:240]}"
                )
                return _journey_result(
                    member, started, errors, jackcoin_awarded, final_state
                )
            finish_body = finish_resp.json()
            jackcoin_awarded = finish_body.get("jackcoin_awarded")
            if jackcoin_awarded is None and isinstance(
                finish_body.get("daily_414"), dict
            ):
                jackcoin_awarded = finish_body["daily_414"].get("jackcoin_awarded")

            for _ in range(3):
                status_resp = await client.get(
                    f"/api/quiz/final-table/status?campaign={CAMPAIGN_CODE}",
                    headers=headers,
                )
                if status_resp.status_code != 200:
                    errors.append(
                        f"final_status:{status_resp.status_code}:"
                        f"{status_resp.text[:240]}"
                    )
                    break
                status_body = status_resp.json()
                final_state = status_body.get("state")
                question = status_body.get("question") or status_body.get(
                    "final_question"
                )
                if question and isinstance(question, dict) and question.get("id"):
                    options = question.get("options") or []
                    answer_value = options[0]["id"] if options else ""
                    await client.post(
                        "/api/quiz/final-table/answer",
                        json={
                            "campaign": CAMPAIGN_CODE,
                            "question_id": question["id"],
                            "answer": answer_value,
                        },
                        headers=headers,
                    )
                    break
                await asyncio.sleep(0.05)
        except Exception as exc:  # noqa: BLE001 — load harness captures all failures
            errors.append(f"exception:{type(exc).__name__}:{exc}")

    return _journey_result(member, started, errors, jackcoin_awarded, final_state)


def _journey_result(
    member: dict[str, Any],
    started: float,
    errors: list[str],
    jackcoin_awarded: Any,
    final_state: Any,
) -> dict[str, Any]:
    return {
        "user_index": member["index"],
        "client_id": member["client_id"],
        "duration_ms": (time.perf_counter() - started) * 1000.0,
        "errors": errors,
        "ok": not errors,
        "jackcoin_awarded": jackcoin_awarded,
        "final_state": final_state,
    }


def analyze_database(db_path: Path) -> dict[str, int]:
    with connect(db_path) as conn:
        duplicate_submission_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT client_id FROM quiz_submissions
                    WHERE campaign_code=?
                    GROUP BY client_id
                    HAVING COUNT(*) > 1
                )
                """,
                (CAMPAIGN_CODE,),
            ).fetchone()[0]
        )
        double_jackcoin = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT source_id FROM jackcoin_ledger
                    WHERE source_type='daily_414'
                      AND source_id IS NOT NULL
                      AND source_id <> ''
                    GROUP BY source_id
                    HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
        )
        clients_with_multi_ledger = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT client_id FROM jackcoin_ledger
                    WHERE source_type='daily_414'
                    GROUP BY client_id
                    HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
        )
        lost_answers = 0
        for row in conn.execute(
            """
            SELECT answers_json, questions_snapshot_json
            FROM quiz_submissions
            WHERE campaign_code=?
            """,
            (CAMPAIGN_CODE,),
        ).fetchall():
            try:
                answers = json.loads(row["answers_json"] or "{}")
                questions = json.loads(row["questions_snapshot_json"] or "[]")
            except json.JSONDecodeError:
                lost_answers += 1
                continue
            expected = len(questions) if questions else 10
            filled = 0
            for question in questions or [{"id": f"m{i}"} for i in range(1, 11)]:
                qid = str(question.get("id") or "")
                value = answers.get(qid)
                if isinstance(value, list):
                    if value:
                        filled += 1
                elif str(value or "").strip():
                    filled += 1
            if filled < expected:
                lost_answers += 1
    return {
        "duplicate_submission_count": duplicate_submission_count,
        "double_jackcoin_count": max(double_jackcoin, clients_with_multi_ledger),
        "lost_answers": lost_answers,
    }


def build_report(
    *,
    users: int,
    concurrency: int,
    results: list[dict[str, Any]],
    duration_total_s: float,
    db_stats: dict[str, int],
) -> dict[str, Any]:
    durations = sorted(float(item["duration_ms"]) for item in results)
    error_samples: list[str] = []
    database_locked_count = 0
    for item in results:
        for error in item["errors"]:
            if is_database_lock_error(error):
                database_locked_count += 1
            if len(error_samples) < 20:
                error_samples.append(error)
    errors_count = sum(len(item["errors"]) for item in results)
    return {
        "users": users,
        "concurrency": concurrency,
        "avg_ms": round(statistics.fmean(durations), 2) if durations else 0.0,
        "p95_ms": round(percentile(durations, 95), 2),
        "p99_ms": round(percentile(durations, 99), 2),
        "min_ms": round(durations[0], 2) if durations else 0.0,
        "max_ms": round(durations[-1], 2) if durations else 0.0,
        "ok_count": sum(1 for item in results if item["ok"]),
        "errors_count": errors_count,
        "error_samples": error_samples,
        "database_locked_count": database_locked_count,
        "duplicate_submission_count": db_stats["duplicate_submission_count"],
        "double_jackcoin_count": db_stats["double_jackcoin_count"],
        "lost_answers": db_stats["lost_answers"],
        "duration_total_s": round(duration_total_s, 3),
        "campaign": CAMPAIGN_CODE,
    }


def print_markdown_summary(report: dict[str, Any]) -> None:
    print("## JACKSIDE load summary")
    print()
    print(f"- users: **{report['users']}** (concurrency {report['concurrency']})")
    print(
        f"- latency: avg **{report['avg_ms']} ms**, "
        f"p95 **{report['p95_ms']} ms**, p99 **{report['p99_ms']} ms**"
    )
    print(
        f"- ok/errors: **{report['ok_count']}/{report['errors_count']}** "
        f"(db locked {report['database_locked_count']})"
    )
    print(
        f"- integrity: duplicate submissions **{report['duplicate_submission_count']}**, "
        f"double jackcoin **{report['double_jackcoin_count']}**, "
        f"lost answers **{report['lost_answers']}**"
    )
    print(f"- duration_total_s: **{report['duration_total_s']}**")
    if report["error_samples"]:
        print()
        print("### Error samples")
        for sample in report["error_samples"][:8]:
            print(f"- `{sample}`")


async def run_load(*, users: int, concurrency: int, report_path: Path) -> dict[str, Any]:
    temp_dir = Path(tempfile.mkdtemp(prefix="jackside-load-"))
    db_path = temp_dir / "load.sqlite3"
    settings = Settings(
        admin_pin="2468",
        admin_name="Load Admin",
        secret_key="load-secret-key-that-is-longer-than-32-characters",
        db_path=db_path,
        secure_cookie=False,
        member_portal_enabled=True,
    )
    init_db(db_path)
    with transaction(db_path) as conn:
        bootstrap_master(
            conn,
            username=settings.master_login,
            display_name=settings.admin_name,
            pin=settings.admin_pin,
        )
        seed_questions_from_json(conn, BASE_DIR / "data" / "quiz_questions.json")
    members = seed_load_world(settings, users=users)
    app = ClientIPASGI(create_app(settings))
    transport = httpx.ASGITransport(app=app)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    wall_started = time.perf_counter()
    async with httpx.AsyncClient(
        transport=transport, base_url="http://load.test"
    ) as client:
        results = await asyncio.gather(
            *[
                run_user_journey(client, member, semaphore=semaphore)
                for member in members
            ]
        )
    duration_total_s = time.perf_counter() - wall_started
    db_stats = analyze_database(db_path)
    report = build_report(
        users=users,
        concurrency=concurrency,
        results=list(results),
        duration_total_s=duration_total_s,
        db_stats=db_stats,
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print_markdown_summary(report)
    print()
    print(f"Report written to {report_path}")
    print(f"Temp DB: {db_path}")
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--users",
        type=int,
        default=50,
        help=f"Participant count (standard: {', '.join(map(str, STANDARD_USER_COUNTS))})",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=0,
        help="Max concurrent journeys (default: same as --users)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("docs/load-reports/latest.json"),
        help="JSON report output path",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.users < 1:
        print("--users must be >= 1", file=sys.stderr)
        return 2
    concurrency = args.concurrency if args.concurrency > 0 else args.users
    asyncio.run(
        run_load(
            users=args.users,
            concurrency=concurrency,
            report_path=args.report,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
