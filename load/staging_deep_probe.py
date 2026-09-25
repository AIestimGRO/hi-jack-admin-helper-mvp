"""Deep STAGING-only diagnostics for intermittent JACKSIDE quiz freezes.

Runs against the already running STAGING service. It compares direct Uvicorn
and public nginx/TLS paths, measures event-loop heartbeat latency during a
15-player cohort, exercises final-table polling, and measures static asset
bursts. Synthetic data is written only to the known STAGING SQLite database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.services.member_accounts import MEMBER_COOKIE_NAME
from load.staging_http_probe import run_cohort, seed_staging_world

STAGING_DB = Path("/opt/hi-jack-admin-helper-v2/data/club_tools.sqlite3")
ALLOWED_BASE_URLS = {
    "http://127.0.0.1:8091",
    "https://quiz-v2.hijackpoker.ru",
    "https://club-v2.hijackpoker.ru",
}
ASSET_PATHS = (
    "/static/css/quiz.css",
    "/static/css/quiz-theme.css",
    "/static/css/jackside-final-recovery.css",
    "/static/js/quiz.js",
    "/static/js/jackside-final-outcome-only.js",
    "/static/img/brand/hi-jack-mark.webp",
)


@dataclass
class Timing:
    label: str
    duration_ms: float
    status_code: int
    error: str = ""


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * pct / 100.0
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return float(ordered[low] * (1 - weight) + ordered[high] * weight)


def timing_stats(samples: list[Timing]) -> dict[str, Any]:
    values = [sample.duration_ms for sample in samples]
    statuses: dict[str, int] = {}
    for sample in samples:
        key = str(sample.status_code)
        statuses[key] = statuses.get(key, 0) + 1
    return {
        "count": len(samples),
        "failures": sum(1 for sample in samples if sample.status_code != 200),
        "avg_ms": round(statistics.fmean(values), 2) if values else 0.0,
        "p50_ms": round(percentile(values, 50), 2),
        "p95_ms": round(percentile(values, 95), 2),
        "p99_ms": round(percentile(values, 99), 2),
        "max_ms": round(max(values), 2) if values else 0.0,
        "status_counts": statuses,
    }


def validate_target(db_path: Path, base_url: str) -> None:
    if db_path.expanduser().resolve() != STAGING_DB:
        raise RuntimeError(f"refusing non-STAGING database: {db_path}")
    normalized = base_url.rstrip("/")
    if normalized not in ALLOWED_BASE_URLS:
        raise RuntimeError(f"refusing non-STAGING target: {base_url}")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError("unsupported scheme")


async def timed_get(
    client: httpx.AsyncClient,
    path: str,
    *,
    label: str,
    headers: dict[str, str] | None = None,
) -> Timing:
    started = time.perf_counter()
    try:
        response = await client.get(path, headers=headers)
        return Timing(
            label=label,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            status_code=response.status_code,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics capture transport errors
        return Timing(
            label=label,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            status_code=0,
            error=f"{type(exc).__name__}: {exc}",
        )


async def heartbeat(
    base_url: str,
    *,
    stop: asyncio.Event,
    interval_seconds: float = 0.2,
    timeout_seconds: float = 10.0,
) -> list[Timing]:
    samples: list[Timing] = []
    timeout = httpx.Timeout(timeout_seconds)
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        follow_redirects=False,
    ) as client:
        while not stop.is_set():
            samples.append(await timed_get(client, "/health/live", label="heartbeat"))
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass
    return samples


async def run_cohort_with_heartbeat(
    *,
    base_url: str,
    campaign: str,
    members: list[dict[str, Any]],
    timeout_seconds: float,
    answer_delay_ms: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    stop = asyncio.Event()
    beat_task = asyncio.create_task(heartbeat(base_url, stop=stop))
    try:
        cohort = await run_cohort(
            base_url=base_url,
            campaign=campaign,
            members=members,
            request_timeout_seconds=timeout_seconds,
            answer_delay_ms=answer_delay_ms,
        )
    finally:
        stop.set()
    beats = await beat_task
    return cohort, timing_stats(beats)


async def final_poll_burst(
    *,
    base_url: str,
    campaign: str,
    members: list[dict[str, Any]],
    rounds: int,
    interval_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    samples: list[Timing] = []
    timeout = httpx.Timeout(timeout_seconds)

    async def poll_member(member: dict[str, Any], round_index: int) -> Timing:
        headers = {
            "Cookie": f"{MEMBER_COOKIE_NAME}={member['token']}",
            "X-Forwarded-For": member["ip"],
            "User-Agent": "jackside-staging-deep-probe/1.0",
        }
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            follow_redirects=False,
        ) as client:
            return await timed_get(
                client,
                f"/api/quiz/final-table/status?campaign={campaign}",
                label=f"final_poll_{round_index:02d}",
                headers=headers,
            )

    for round_index in range(1, rounds + 1):
        round_samples = await asyncio.gather(
            *(poll_member(member, round_index) for member in members)
        )
        samples.extend(round_samples)
        if round_index < rounds:
            await asyncio.sleep(interval_seconds)
    return timing_stats(samples)


async def asset_burst(
    *,
    base_url: str,
    concurrency: int,
    rounds: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    samples: list[Timing] = []
    timeout = httpx.Timeout(timeout_seconds)

    async def fetch_one(path: str, worker: int, round_index: int) -> Timing:
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            follow_redirects=False,
        ) as client:
            return await timed_get(
                client,
                path,
                label=f"asset:{path}:r{round_index}:w{worker}",
            )

    for round_index in range(1, rounds + 1):
        tasks = []
        for worker in range(concurrency):
            path = ASSET_PATHS[worker % len(ASSET_PATHS)]
            tasks.append(fetch_one(path, worker, round_index))
        samples.extend(await asyncio.gather(*tasks))
    by_path: dict[str, dict[str, Any]] = {}
    for path in ASSET_PATHS:
        subset = [sample for sample in samples if f"asset:{path}:" in sample.label]
        if subset:
            by_path[path] = timing_stats(subset)
    return {"all": timing_stats(samples), "by_path": by_path}


async def idle_heartbeat_watch(
    *,
    base_url: str,
    seconds: float,
    interval_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    stop = asyncio.Event()

    async def stopper() -> None:
        await asyncio.sleep(seconds)
        stop.set()

    beat_task = asyncio.create_task(
        heartbeat(
            base_url,
            stop=stop,
            interval_seconds=interval_seconds,
            timeout_seconds=timeout_seconds,
        )
    )
    await stopper()
    samples = await beat_task
    slowest = sorted(samples, key=lambda sample: sample.duration_ms, reverse=True)[:10]
    return {
        "stats": timing_stats(samples),
        "slowest": [asdict(sample) for sample in slowest],
    }


def campaign_code(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    return f"stgdeep_{prefix}_{stamp}"[:64]


async def one_path_scenario(
    *,
    settings: Settings,
    base_url: str,
    users: int,
    answer_delay_ms: int,
    timeout_seconds: float,
    final_poll_rounds: int,
) -> dict[str, Any]:
    validate_target(Path(settings.db_path), base_url)
    campaign = campaign_code("local" if "8091" in base_url else "public")
    members = seed_staging_world(settings=settings, campaign=campaign, users=users)
    cohort, beat = await run_cohort_with_heartbeat(
        base_url=base_url,
        campaign=campaign,
        members=members,
        timeout_seconds=timeout_seconds,
        answer_delay_ms=answer_delay_ms,
    )
    final_poll = await final_poll_burst(
        base_url=base_url,
        campaign=campaign,
        members=members,
        rounds=final_poll_rounds,
        interval_seconds=1.0,
        timeout_seconds=timeout_seconds,
    )
    return {
        "campaign": campaign,
        "base_url": base_url,
        "users": users,
        "answer_delay_ms": answer_delay_ms,
        "cohort": cohort,
        "heartbeat_during_cohort": beat,
        "final_poll": final_poll,
    }


async def async_main(args: argparse.Namespace) -> int:
    db_path = Path(args.db_path)
    if db_path.expanduser().resolve() != STAGING_DB:
        raise RuntimeError("deep probe only accepts the known STAGING DB")
    settings = Settings(db_path=db_path)
    if len(settings.secret_key) < 32:
        raise RuntimeError("HJC_SECRET_KEY is not loaded")

    results: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "users": args.users,
        "answer_delay_ms": args.answer_delay_ms,
    }

    results["local"] = await one_path_scenario(
        settings=settings,
        base_url="http://127.0.0.1:8091",
        users=args.users,
        answer_delay_ms=args.answer_delay_ms,
        timeout_seconds=args.request_timeout,
        final_poll_rounds=args.final_poll_rounds,
    )
    results["public_quiz"] = await one_path_scenario(
        settings=settings,
        base_url="https://quiz-v2.hijackpoker.ru",
        users=args.users,
        answer_delay_ms=args.answer_delay_ms,
        timeout_seconds=args.request_timeout,
        final_poll_rounds=args.final_poll_rounds,
    )
    results["public_assets_quiz"] = await asset_burst(
        base_url="https://quiz-v2.hijackpoker.ru",
        concurrency=args.users,
        rounds=args.asset_rounds,
        timeout_seconds=args.request_timeout,
    )
    results["public_assets_club"] = await asset_burst(
        base_url="https://club-v2.hijackpoker.ru",
        concurrency=args.users,
        rounds=args.asset_rounds,
        timeout_seconds=args.request_timeout,
    )

    if args.analytics_watch_seconds > 0:
        results["analytics_scheduler_watch"] = await idle_heartbeat_watch(
            base_url="http://127.0.0.1:8091",
            seconds=args.analytics_watch_seconds,
            interval_seconds=args.analytics_watch_interval,
            timeout_seconds=args.request_timeout,
        )

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"deep-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"REPORT={report_path}")
    print("STAGING_ONLY=PASS")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=str(STAGING_DB))
    parser.add_argument("--users", type=int, default=15)
    parser.add_argument("--answer-delay-ms", type=int, default=250)
    parser.add_argument("--request-timeout", type=float, default=35.0)
    parser.add_argument("--final-poll-rounds", type=int, default=10)
    parser.add_argument("--asset-rounds", type=int, default=3)
    parser.add_argument("--analytics-watch-seconds", type=float, default=320.0)
    parser.add_argument("--analytics-watch-interval", type=float, default=1.0)
    parser.add_argument("--report-dir", default="/tmp/jackside-staging-deep-probe")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 1 <= args.users <= 20:
        raise SystemExit("--users must be between 1 and 20")
    if args.final_poll_rounds < 1 or args.final_poll_rounds > 30:
        raise SystemExit("--final-poll-rounds must be 1..30")
    if args.asset_rounds < 1 or args.asset_rounds > 10:
        raise SystemExit("--asset-rounds must be 1..10")
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
