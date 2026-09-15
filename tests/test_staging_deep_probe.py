from pathlib import Path

import pytest

from load.staging_deep_probe import (
    Timing,
    percentile,
    timing_stats,
    validate_target,
)


def test_percentile_interpolates() -> None:
    assert percentile([10.0], 95) == 10.0
    assert percentile([10.0, 20.0], 50) == 15.0
    assert percentile([], 95) == 0.0


def test_timing_stats_counts_failures_and_statuses() -> None:
    report = timing_stats(
        [
            Timing("a", 10.0, 200),
            Timing("b", 20.0, 200),
            Timing("c", 30.0, 503, "boom"),
        ]
    )
    assert report["count"] == 3
    assert report["failures"] == 1
    assert report["status_counts"] == {"200": 2, "503": 1}
    assert report["max_ms"] == 30.0


@pytest.mark.parametrize(
    "target_url",
    [
        "http://127.0.0.1:8091",
        "https://quiz-v2.hijackpoker.ru",
        "https://club-v2.hijackpoker.ru",
    ],
)
def test_validate_target_allows_only_known_staging_paths(target_url: str) -> None:
    validate_target(
        Path("/opt/hi-jack-admin-helper-v2/data/club_tools.sqlite3"),
        target_url,
    )


def test_validate_target_refuses_prod_db() -> None:
    with pytest.raises(RuntimeError, match="non-STAGING database"):
        validate_target(
            Path("/opt/hi-jack-admin-helper/data/club_tools.sqlite3"),
            "https://quiz-v2.hijackpoker.ru",
        )


def test_validate_target_refuses_prod_host() -> None:
    with pytest.raises(RuntimeError, match="non-STAGING target"):
        validate_target(
            Path("/opt/hi-jack-admin-helper-v2/data/club_tools.sqlite3"),
            "https://club.hijackpoker.ru",
        )
