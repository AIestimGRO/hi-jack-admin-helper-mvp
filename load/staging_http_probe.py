"""Real-HTTP load probe for JACKSIDE STAGING only.

This tool intentionally talks to the already running STAGING Uvicorn process
instead of creating an in-process ASGI application. It seeds a dedicated
``daily_414`` campaign and disposable test members in the STAGING SQLite DB,
then measures the same HTTP path used by players:

    GET /quiz -> questions -> identity -> start -> answer x10 -> finish

The default concurrency ladder is 1,3,5,10,15. Each cohort uses fresh members
because daily_414 permits one attempt per account.

Safety contract:
* refuses the PROD DB path;
* accepts only the known STAGING DB directory;
* accepts only local STAGING port 8091 or the v2 quiz hostname;
* never restarts services and never touches the timer;
* never deletes existing rows. Test rows are tagged with ``staging_http_load``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.db import connect, transaction
from app.services.daily_414 import DAILY_414_TIME_LIMIT_SECONDS
from app.services.member_accounts import MEMBER_COOKIE_NAME, hash_password, issue_session

DEFAULT_DB = Path("/opt/hi-jack-admin-helper-v2/data/club_tools.sqlite3")
PROD_DB = Path("/opt/hi-jack-admin-helper/data/club_tools.sqlite3")
DEFAULT_BASE_URL = "http://127.0.0.1:8091"
DEFAULT_LEVELS = (1, 3, 5, 10, 15)
SOURCE_TAG = "staging_http_load"


@dataclass
class RequestSample:
    user_index: int
    step: str
    duration_ms: float
    status_code: int
    ok: bool
    error: str = ""


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def parse_levels(raw: str) -> tuple[int, ...]:
    levels = tuple(int(part.strip()) for part in raw.split(",") if part.strip())
    if not levels or any(level < 1 or level > 50 for level in levels):
        raise ValueError("levels must contain integers from 1 to 50")
    if sum(levels) > 100:
        raise ValueError("sum(levels) must not exceed 100")
    return levels


def assert_staging_target(db_path: Path, base_url: str) -> None:
    resolved = db_path.expanduser().resolve()
    if resolved == PROD_DB:
        raise RuntimeError("refusing PROD database")
    if resolved != DEFAULT_DB:
        raise RuntimeError(
            f"refusing non-STAGING database: {resolved}; expected {DEFAULT_DB}"
        )

    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    local_staging = host in {"127.0.0.1", "localhost"} and port == 8091
    public_staging = host == "quiz-v2.hijackpoker.ru" and parsed.scheme == "https"
    if not (local_staging or public_staging):
        raise RuntimeError(
            "refusing non-STAGING HTTP target; use http://127.0.0.1:8091 "
            "or https://quiz-v2.hijackpoker.ru"
        )


def _campaign_code() -> str:
    return "stgload_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _phone_local(seed: int, index: int) -> str:
    # 10-digit local phone, unique enough for short-lived STAGING probes.
    value = ((seed % 100_000) * 100 + index) % 10_000_000
    return f"977{value:07d}"


def seed_staging_world(
    *,
    settings: Settings,
    campaign: str,
    users: int,
) -> list[dict[str, Any]]:
    local_now = datetime.now(ZoneInfo(settings.timezone_name)).replace(tzinfo=None)
    active_from = (local_now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    active_until = (local_now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    password_hash = hash_password("staging-load-pass-1")
    seed = int(time.time())
    members: list[dict[str, Any]] = []

    with transaction(settings.db_path) as conn:
        existing = conn.execute(
            "SELECT 1 FROM quiz_campaigns WHERE code=?", (campaign,)
        ).fetchone()
        if existing:
            raise RuntimeError(f"campaign already exists: {campaign}")

        conn.execute(
            """
            INSERT INTO quiz_campaigns(
                code, title, campaign_type, quiz_time_limit_seconds,
                max_attempts, verification_required, current_version, is_active,
                active_from, active_until, welcome_kicker, welcome_text,
                start_button_text, jackcoin_per_correct, jackcoin_completion_bonus,
                jackcoin_perfect_bonus, final_prize_type,
                final_prize_jackcoin_amount, final_question_time_seconds
            ) VALUES (
                ?, 'STAGING HTTP LOAD PROBE', 'daily_414', ?, 1, 1, 1, 1,
                ?, ?, 'STAGING LOAD', 'Synthetic diagnostic campaign',
                'START', 0, 0, 0, 'none', 0, 30
            )
            """,
            (
                campaign,
                DAILY_414_TIME_LIMIT_SECONDS,
                active_from,
                active_until,
            ),
        )

        for question_index in range(1, 11):
            question_id = int(
                conn.execute(
                    """
                    INSERT INTO quiz_questions(
                        campaign_code, code, type, title, required, points,
                        position, is_active, game_round
                    ) VALUES (?, ?, 'single_choice', ?, 1, 1, ?, 1, 'main')
                    """,
                    (
                        campaign,
                        f"m{question_index}",
                        f"Load question {question_index}",
                        question_index * 10,
                    ),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO quiz_options(
                    question_id, code, text, is_correct, position
                ) VALUES (?, ?, 'Correct', 1, 10), (?, ?, 'Wrong', 0, 20)
                """,
                (
                    question_id,
                    f"m{question_index}_yes",
                    question_id,
                    f"m{question_index}_no",
                ),
            )

        for final_index in range(1, 3):
            final_id = int(
                conn.execute(
                    """
                    INSERT INTO quiz_questions(
                        campaign_code, code, type, title, game_round,
                        required, points, position, is_active, time_limit_seconds
                    ) VALUES (?, ?, 'single_choice', ?, 'final',
                              1, 1, ?, 1, 30)
                    """,
                    (
                        campaign,
                        f"f{final_index}",
                        f"Load final {final_index}",
                        final_index * 10,
                    ),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO quiz_options(
                    question_id, code, text, is_correct, position
                ) VALUES (?, ?, 'Correct', 1, 10), (?, ?, 'Wrong', 0, 20)
                """,
                (
                    final_id,
                    f"f{final_index}_yes",
                    final_id,
                    f"f{final_index}_no",
                ),
            )

        for user_index in range(users):
            phone_local = _phone_local(seed, user_index)
            email = f"{campaign}-{user_index:03d}@example.invalid"
            client_id = int(
                conn.execute(
                    """
                    INSERT INTO clients(
                        first_name, phone_raw, phone_full, phone_local, source
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        f"STG Load {user_index:03d}",
                        f"+7{phone_local}",
                        f"7{phone_local}",
                        phone_local,
                        SOURCE_TAG,
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
            token = issue_session(
                conn,
                secret_key=settings.secret_key,
                account_id=account_id,
                session_version=1,
                days=1,
                ip_hash=f"staging-load-{campaign}-{user_index}",
                user_agent="jackside-staging-http-load",
            )
            members.append(
                {
                    "index": user_index,
                    "client_id": client_id,
                    "account_id": account_id,
                    "phone": phone_local,
                    "token": token,
                    "ip": f"10.77.{(user_index // 250) % 250}.{(user_index % 250) + 1}",
                }
            )

    return members


async def _timed_request(
    client: httpx.AsyncClient,
    samples: list[RequestSample],
    *,
    user_index: int,
    step: str,
    method: str,
    url: str,
    headers: dict[str, str],
    json_body: dict[str, Any] | None = None,
) -> httpx.Response | None:
    started = time.perf_counter()
    try:
        response = await client.request(
            method,
            url,
            headers=headers,
            json=json_body,
        )
        duration_ms = (time.perf_counter() - started) * 1000.0
        ok = 200 <= response.status_code < 400
        samples.append(
            RequestSample(
                user_index=user_index,
                step=step,
                duration_ms=duration_ms,
                status_code=response.status_code,
                ok=ok,
                error="" if ok else response.text[:200],
            )
        )
        return response
    except Exception as exc:  # noqa: BLE001 - probe must capture transport failures
        duration_ms = (time.perf_counter() - started) * 1000.0
        samples.append(
            RequestSample(
                user_index=user_index,
                step=step,
                duration_ms=duration_ms,
                status_code=0,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        )
        return None


async def run_member_journey(
    *,
    base_url: str,
    campaign: str,
    member: dict[str, Any],
    gate: asyncio.Event,
    request_timeout_seconds: float,
    answer_delay_ms: int,
) -> list[RequestSample]:
    samples: list[RequestSample] = []
    headers = {
        "Accept": "application/json",
        "Cookie": f"{MEMBER_COOKIE_NAME}={member['token']}",
        "X-Forwarded-For": member["ip"],
        "User-Agent": "jackside-staging-http-load/1.0",
    }
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
    timeout = httpx.Timeout(request_timeout_seconds)

    await gate.wait()
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        limits=limits,
        follow_redirects=False,
    ) as client:
        page_headers = dict(headers)
        page_headers["Accept"] = "text/html,application/xhtml+xml"
        response = await _timed_request(
            client,
            samples,
            user_index=member["index"],
            step="page",
            method="GET",
            url=f"/quiz?campaign={campaign}",
            headers=page_headers,
        )
        if response is None or response.status_code != 200:
            return samples

        response = await _timed_request(
            client,
            samples,
            user_index=member["index"],
            step="questions",
            method="GET",
            url=f"/api/quiz/questions?campaign={campaign}",
            headers=headers,
        )
        if response is None or response.status_code != 200:
            return samples

        response = await _timed_request(
            client,
            samples,
            user_index=member["index"],
            step="identity",
            method="GET",
            url=f"/api/quiz/identity?campaign={campaign}",
            headers=headers,
        )
        if response is None or response.status_code != 200:
            return samples

        response = await _timed_request(
            client,
            samples,
            user_index=member["index"],
            step="start",
            method="POST",
            url="/api/quiz/start",
            headers=headers,
            json_body={
                "campaign": campaign,
                "phone": member["phone"],
                "username": f"stg_load_{member['index']:03d}",
                "name": f"STG Load {member['index']:03d}",
            },
        )
        if response is None or response.status_code != 200:
            return samples
        try:
            payload = response.json()
            attempt_token = payload["attempt_token"]
            questions = list(payload.get("questions") or [])
        except (ValueError, KeyError, TypeError) as exc:
            samples.append(
                RequestSample(
                    user_index=member["index"],
                    step="start_payload",
                    duration_ms=0.0,
                    status_code=response.status_code,
                    ok=False,
                    error=f"invalid start payload: {exc}",
                )
            )
            return samples

        for question_index, question in enumerate(questions, start=1):
            options = list(question.get("options") or [])
            if not options:
                samples.append(
                    RequestSample(
                        user_index=member["index"],
                        step=f"answer_{question_index:02d}",
                        duration_ms=0.0,
                        status_code=0,
                        ok=False,
                        error="question has no options",
                    )
                )
                return samples
            response = await _timed_request(
                client,
                samples,
                user_index=member["index"],
                step=f"answer_{question_index:02d}",
                method="POST",
                url="/api/quiz/answer",
                headers=headers,
                json_body={
                    "attempt_token": attempt_token,
                    "question_id": question["id"],
                    "answer": options[0]["id"],
                },
            )
            if response is None or response.status_code != 200:
                return samples
            if answer_delay_ms:
                await asyncio.sleep(answer_delay_ms / 1000.0)

        await _timed_request(
            client,
            samples,
            user_index=member["index"],
            step="finish",
            method="POST",
            url="/api/quiz/finish",
            headers=headers,
            json_body={"attempt_token": attempt_token},
        )

    return samples


def _stats(samples: list[RequestSample]) -> dict[str, Any]:
    durations = [sample.duration_ms for sample in samples]
    status_counts: dict[str, int] = {}
    for sample in samples:
        key = str(sample.status_code)
        status_counts[key] = status_counts.get(key, 0) + 1
    return {
        "count": len(samples),
        "ok": sum(1 for sample in samples if sample.ok),
        "failed": sum(1 for sample in samples if not sample.ok),
        "avg_ms": round(statistics.fmean(durations), 2) if durations else 0.0,
        "p50_ms": round(percentile(durations, 50), 2),
        "p95_ms": round(percentile(durations, 95), 2),
        "max_ms": round(max(durations), 2) if durations else 0.0,
        "status_counts": status_counts,
    }


def build_cohort_report(
    *,
    concurrency: int,
    wall_ms: float,
    samples: list[RequestSample],
) -> dict[str, Any]:
    steps = sorted({sample.step for sample in samples})
    report_steps = {
        step: _stats([sample for sample in samples if sample.step == step])
        for step in steps
    }
    answer_samples = [sample for sample in samples if sample.step.startswith("answer_")]
    if answer_samples:
        report_steps["answer_all"] = _stats(answer_samples)
    return {
        "concurrency": concurrency,
        "wall_ms": round(wall_ms, 2),
        "requests": len(samples),
        "failures": sum(1 for sample in samples if not sample.ok),
        "steps": report_steps,
        "errors": [
            {
                "user": sample.user_index,
                "step": sample.step,
                "status": sample.status_code,
                "error": sample.error,
            }
            for sample in samples
            if not sample.ok
        ][:30],
    }


async def run_cohort(
    *,
    base_url: str,
    campaign: str,
    members: list[dict[str, Any]],
    request_timeout_seconds: float,
    answer_delay_ms: int,
) -> dict[str, Any]:
    gate = asyncio.Event()
    tasks = [
        asyncio.create_task(
            run_member_journey(
                base_url=base_url,
                campaign=campaign,
                member=member,
                gate=gate,
                request_timeout_seconds=request_timeout_seconds,
                answer_delay_ms=answer_delay_ms,
            )
        )
        for member in members
    ]
    started = time.perf_counter()
    gate.set()
    results = await asyncio.gather(*tasks)
    wall_ms = (time.perf_counter() - started) * 1000.0
    samples = [sample for user_samples in results for sample in user_samples]
    return build_cohort_report(
        concurrency=len(members),
        wall_ms=wall_ms,
        samples=samples,
    )


def db_outcome(db_path: Path, campaign: str) -> dict[str, Any]:
    with connect(db_path) as conn:
        attempt_rows = conn.execute(
            """
            SELECT status,COUNT(*) AS n
            FROM quiz_attempts
            WHERE campaign_code=?
            GROUP BY status
            ORDER BY status
            """,
            (campaign,),
        ).fetchall()
        submissions = int(
            conn.execute(
                "SELECT COUNT(*) FROM quiz_submissions WHERE campaign_code=?",
                (campaign,),
            ).fetchone()[0]
        )
        return {
            "attempt_status": {str(row["status"]): int(row["n"]) for row in attempt_rows},
            "submissions": submissions,
        }


def print_cohort(report: dict[str, Any]) -> None:
    print()
    print(
        f"=== concurrency={report['concurrency']} "
        f"wall={report['wall_ms']:.0f}ms failures={report['failures']} ==="
    )
    preferred = ["page", "questions", "identity", "start", "answer_all", "finish"]
    for step in preferred:
        stats = report["steps"].get(step)
        if not stats:
            continue
        print(
            f"{step:11s} n={stats['count']:3d} failed={stats['failed']:2d} "
            f"p50={stats['p50_ms']:8.1f}ms "
            f"p95={stats['p95_ms']:8.1f}ms "
            f"max={stats['max_ms']:8.1f}ms"
        )
    if report["errors"]:
        print("errors:")
        for error in report["errors"][:10]:
            print(
                f"  user={error['user']} step={error['step']} "
                f"status={error['status']} {error['error']}"
            )


async def async_main(args: argparse.Namespace) -> int:
    levels = parse_levels(args.levels)
    db_path = Path(args.db_path)
    assert_staging_target(db_path, args.base_url)

    settings = Settings(db_path=db_path)
    if len(settings.secret_key) < 32:
        raise RuntimeError(
            "HJC_SECRET_KEY is not loaded; run via the supplied STAGING wrapper "
            "that sources /opt/hi-jack-admin-helper-v2/.env"
        )

    async with httpx.AsyncClient(
        base_url=args.base_url,
        timeout=httpx.Timeout(5.0),
        follow_redirects=False,
    ) as health_client:
        health = await health_client.get("/health/live")
        if health.status_code != 200:
            raise RuntimeError(f"STAGING health failed: HTTP {health.status_code}")

    campaign = args.campaign or _campaign_code()
    total_users = sum(levels)
    members = seed_staging_world(
        settings=settings,
        campaign=campaign,
        users=total_users,
    )

    print(f"campaign={campaign}")
    print(f"base_url={args.base_url}")
    print(f"db={db_path}")
    print(f"levels={','.join(str(level) for level in levels)}")
    print(f"seeded_users={len(members)}")

    reports: list[dict[str, Any]] = []
    cursor = 0
    for level in levels:
        cohort = members[cursor : cursor + level]
        cursor += level
        report = await run_cohort(
            base_url=args.base_url,
            campaign=campaign,
            members=cohort,
            request_timeout_seconds=args.request_timeout,
            answer_delay_ms=args.answer_delay_ms,
        )
        reports.append(report)
        print_cohort(report)
        if args.cooldown_seconds > 0:
            await asyncio.sleep(args.cooldown_seconds)

    outcome = db_outcome(db_path, campaign)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "campaign": campaign,
        "base_url": args.base_url,
        "db_path": str(db_path),
        "levels": list(levels),
        "answer_delay_ms": args.answer_delay_ms,
        "request_timeout_seconds": args.request_timeout,
        "cohorts": reports,
        "db_outcome": outcome,
    }

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{campaign}.json"
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print()
    print("=== DB OUTCOME ===")
    print(json.dumps(outcome, ensure_ascii=False, sort_keys=True))
    print(f"REPORT={report_path}")
    print("STAGING_ONLY=PASS")
    return 0 if all(report["failures"] == 0 for report in reports) else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=str(DEFAULT_DB))
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--levels", default=",".join(map(str, DEFAULT_LEVELS)))
    parser.add_argument("--campaign", default="")
    parser.add_argument("--request-timeout", type=float, default=35.0)
    parser.add_argument("--answer-delay-ms", type=int, default=0)
    parser.add_argument("--cooldown-seconds", type=float, default=2.0)
    parser.add_argument("--report-dir", default="/tmp/jackside-staging-http-load")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
