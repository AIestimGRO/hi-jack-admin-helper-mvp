from __future__ import annotations

import json
import logging
from typing import Any


logger = logging.getLogger("jackside.ops")

_ALLOWED_ID_FIELDS = frozenset(
    {
        "issue_id",
        "submission_id",
        "final_table_id",
        "member_id",
        "campaign_code",
        "client_id",
        "attempt_id",
    }
)


def log_event(
    event_type: str,
    *,
    status: str = "ok",
    duration_ms: int | float | None = None,
    error_code: str | None = None,
    **ids: Any,
) -> None:
    """Emit one structured JSON ops log line. Never logs secrets or PII."""
    payload: dict[str, Any] = {
        "event_type": event_type,
        "status": status,
    }
    if duration_ms is not None:
        payload["duration_ms"] = int(duration_ms)
    if error_code is not None:
        payload["error_code"] = error_code
    for key, value in ids.items():
        if key not in _ALLOWED_ID_FIELDS or value is None:
            continue
        payload[key] = value
    logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))
