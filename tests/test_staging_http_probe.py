from __future__ import annotations

from pathlib import Path

import pytest

from load.staging_http_probe import (
    DEFAULT_DB,
    RequestSample,
    assert_staging_target,
    build_cohort_report,
    parse_levels,
)


def test_parse_levels_accepts_realistic_staging_ladder() -> None:
    assert parse_levels("1,3,5,10,15") == (1, 3, 5, 10, 15)


@pytest.mark.parametrize("value", ["0", "51", "40,40,40", ""])
def test_parse_levels_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_levels(value)


def test_assert_staging_target_accepts_known_local_staging() -> None:
    assert_staging_target(DEFAULT_DB, "http://127.0.0.1:8091")


def test_assert_staging_target_rejects_prod_database() -> None:
    with pytest.raises(RuntimeError, match="PROD"):
        assert_staging_target(
            Path("/opt/hi-jack-admin-helper/data/club_tools.sqlite3"),
            "http://127.0.0.1:8091",
        )


def test_assert_staging_target_rejects_prod_port() -> None:
    with pytest.raises(RuntimeError, match="non-STAGING HTTP target"):
        assert_staging_target(DEFAULT_DB, "http://127.0.0.1:8090")


def test_build_cohort_report_aggregates_answer_samples() -> None:
    samples = [
        RequestSample(0, "page", 10.0, 200, True),
        RequestSample(0, "answer_01", 20.0, 200, True),
        RequestSample(0, "answer_02", 40.0, 200, True),
        RequestSample(1, "answer_01", 60.0, 500, False, "boom"),
    ]

    report = build_cohort_report(
        concurrency=2,
        wall_ms=100.0,
        samples=samples,
    )

    assert report["concurrency"] == 2
    assert report["failures"] == 1
    assert report["steps"]["answer_all"]["count"] == 3
    assert report["steps"]["answer_all"]["failed"] == 1
    assert report["steps"]["answer_all"]["p50_ms"] == 40.0
    assert report["errors"][0]["step"] == "answer_01"
