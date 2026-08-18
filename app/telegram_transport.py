from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.db import connect, transaction


SIGNATURE_HEADER = "X-HJC-Signature"
TIMESTAMP_HEADER = "X-HJC-Timestamp"
SOURCE_HEADER = "X-HJC-Source"
SIGNATURE_MAX_AGE_SECONDS = 300
MAX_TRANSPORT_ATTEMPTS = 5


class TelegramTransportError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sqlite_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def sign_transport_body(secret: str, timestamp: str, body: bytes) -> str:
    key = str(secret or "").encode("utf-8")
    payload = timestamp.encode("ascii") + b"." + body
    digest = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def signed_transport_headers(
    secret: str,
    body: bytes,
    *,
    timestamp: int | None = None,
    source: str = "admin-helper",
) -> dict[str, str]:
    raw_timestamp = str(int(timestamp if timestamp is not None else time.time()))
    return {
        "Content-Type": "application/json",
        TIMESTAMP_HEADER: raw_timestamp,
        SIGNATURE_HEADER: sign_transport_body(secret, raw_timestamp, body),
        SOURCE_HEADER: source,
    }


def verify_transport_signature(
    secret: str,
    body: bytes,
    timestamp_value: str,
    signature_value: str,
    *,
    now_timestamp: int | None = None,
) -> bool:
    if not secret or not timestamp_value or not signature_value:
        return False
    try:
        request_timestamp = int(timestamp_value)
    except (TypeError, ValueError):
        return False
    current = int(now_timestamp if now_timestamp is not None else time.time())
    if abs(current - request_timestamp) > SIGNATURE_MAX_AGE_SECONDS:
        return False
    expected = sign_transport_body(secret, str(request_timestamp), body)
    return hmac.compare_digest(expected, str(signature_value))


def _default_post_json(
    url: str,
    secret: str,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = UrlRequest(
        url,
        data=body,
        headers=signed_transport_headers(secret, body),
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_body = response.read()
            status = int(getattr(response, "status", 200))
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            detail = ""
        raise TelegramTransportError(
            f"transport_http_{exc.code}: {detail or exc.reason}"
        ) from exc
    except URLError as exc:
        raise TelegramTransportError(f"transport_network_error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise TelegramTransportError("transport_timeout") from exc

    if status < 200 or status >= 300:
        raise TelegramTransportError(f"transport_http_{status}")
    try:
        decoded = json.loads(response_body.decode("utf-8")) if response_body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TelegramTransportError("transport_invalid_json") from exc
    if not isinstance(decoded, dict):
        raise TelegramTransportError("transport_invalid_response")
    return decoded


def _backoff_seconds(attempts: int) -> int:
    schedule = (30, 60, 300, 900, 3600)
    index = max(0, min(int(attempts) - 1, len(schedule) - 1))
    return schedule[index]


def _refresh_campaign_status(conn, campaign_id: int | None) -> None:
    if campaign_id is None:
        return
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total_count,
            SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued_count,
            SUM(CASE WHEN status='sending' THEN 1 ELSE 0 END) AS sending_count,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_count,
            SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent_count
        FROM telegram_notification_outbox
        WHERE campaign_id=?
        """,
        (campaign_id,),
    ).fetchone()
    if not row or int(row["total_count"] or 0) == 0:
        return
    queued = int(row["queued_count"] or 0)
    sending = int(row["sending_count"] or 0)
    failed = int(row["failed_count"] or 0)
    sent = int(row["sent_count"] or 0)
    if sending:
        status = "sending"
    elif queued:
        status = "queued"
    elif failed:
        status = "failed"
    elif sent == int(row["total_count"]):
        status = "sent"
    else:
        return
    conn.execute(
        """
        UPDATE telegram_notification_campaigns
        SET status=?,updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (status, campaign_id),
    )


def _transport_gate(settings: Any) -> tuple[bool, str]:
    if not bool(getattr(settings, "telegram_notifications_enabled", False)):
        return False, "environment_disabled"
    if not str(getattr(settings, "telegram_transport_url", "") or "").strip():
        return False, "transport_url_missing"
    if not str(getattr(settings, "telegram_bridge_secret", "") or "").strip():
        return False, "bridge_secret_missing"
    with connect(settings.db_path) as conn:
        row = conn.execute(
            "SELECT sending_enabled FROM telegram_notification_settings WHERE id=1"
        ).fetchone()
    if not row or not bool(row["sending_enabled"]):
        return False, "internal_switch_disabled"
    return True, "ready"


def transport_status(settings: Any) -> dict[str, Any]:
    ready, reason = _transport_gate(settings)
    return {
        "ready": ready,
        "reason": reason,
        "environment_enabled": bool(
            getattr(settings, "telegram_notifications_enabled", False)
        ),
        "transport_url_configured": bool(
            str(getattr(settings, "telegram_transport_url", "") or "").strip()
        ),
        "bridge_secret_configured": bool(
            str(getattr(settings, "telegram_bridge_secret", "") or "").strip()
        ),
    }


def _claim_outbox_row(db_path: Any, outbox_id: int):
    with transaction(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM telegram_notification_outbox
            WHERE id=? AND status='queued'
              AND (next_attempt_at IS NULL OR next_attempt_at<=CURRENT_TIMESTAMP)
            """,
            (outbox_id,),
        ).fetchone()
        if not row:
            return None
        cursor = conn.execute(
            """
            UPDATE telegram_notification_outbox
            SET status='sending',attempts=attempts+1,
                last_error=NULL,updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='queued'
            """,
            (outbox_id,),
        )
        if not cursor.rowcount:
            return None
        return dict(row)


def _record_transport_acceptance(
    db_path: Any,
    row: dict[str, Any],
    task_id: str,
) -> None:
    with transaction(db_path) as conn:
        conn.execute(
            """
            INSERT INTO telegram_notification_deliveries(
                outbox_id,campaign_id,client_id,status,provider_message_id
            ) VALUES (?,?,?,'accepted',?)
            """,
            (
                int(row["id"]),
                row.get("campaign_id"),
                int(row["client_id"]),
                task_id or None,
            ),
        )
        _refresh_campaign_status(conn, row.get("campaign_id"))


def _record_transport_error(
    db_path: Any,
    row: dict[str, Any],
    error_text: str,
) -> None:
    attempts = int(row.get("attempts") or 0) + 1
    final_failure = attempts >= MAX_TRANSPORT_ATTEMPTS
    next_attempt_at = None
    if not final_failure:
        next_attempt_at = _sqlite_datetime(
            _utc_now() + timedelta(seconds=_backoff_seconds(attempts))
        )
    status = "failed" if final_failure else "queued"
    with transaction(db_path) as conn:
        conn.execute(
            """
            UPDATE telegram_notification_outbox
            SET status=?,next_attempt_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (status, next_attempt_at, error_text[:2000], int(row["id"])),
        )
        conn.execute(
            """
            INSERT INTO telegram_notification_deliveries(
                outbox_id,campaign_id,client_id,status,error_code,error_text
            ) VALUES (?,?,?,'transport_error','transport_dispatch',?)
            """,
            (
                int(row["id"]),
                row.get("campaign_id"),
                int(row["client_id"]),
                error_text[:2000],
            ),
        )
        _refresh_campaign_status(conn, row.get("campaign_id"))


def dispatch_telegram_outbox_once(
    settings: Any,
    *,
    limit: int = 20,
    sender: Callable[[str, str, dict[str, Any], float], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ready, reason = _transport_gate(settings)
    if not ready:
        return {"ok": False, "reason": reason, "accepted": 0, "failed": 0}

    safe_limit = max(1, min(int(limit), 100))
    with connect(settings.db_path) as conn:
        candidate_rows = conn.execute(
            """
            SELECT id FROM telegram_notification_outbox
            WHERE status='queued'
              AND (next_attempt_at IS NULL OR next_attempt_at<=CURRENT_TIMESTAMP)
            ORDER BY created_at,id
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()

    post_json = sender or _default_post_json
    accepted = 0
    failed = 0
    skipped = 0
    for candidate in candidate_rows:
        row = _claim_outbox_row(settings.db_path, int(candidate["id"]))
        if not row:
            skipped += 1
            continue
        try:
            payload = json.loads(str(row.get("payload_json") or "{}"))
            if not isinstance(payload, dict):
                raise TelegramTransportError("outbox_payload_invalid")
            text = str(payload.get("text") or "").strip()
            if not text:
                raise TelegramTransportError("outbox_text_missing")
            envelope = {
                "version": 1,
                "outbox_id": int(row["id"]),
                "idempotency_key": str(row["idempotency_key"]),
                "chat_id": str(row["telegram_chat_id"]),
                "text": text,
                "category": str(row.get("category") or "club_updates"),
                "button_text": str(payload.get("button_text") or "").strip(),
                "button_url": str(payload.get("button_url") or "").strip(),
            }
            response = post_json(
                str(settings.telegram_transport_url),
                str(settings.telegram_bridge_secret),
                envelope,
                float(getattr(settings, "telegram_transport_timeout_seconds", 5.0)),
            )
            if not bool(response.get("accepted")):
                raise TelegramTransportError(
                    str(response.get("detail") or "transport_not_accepted")
                )
            task_id = str(response.get("task_id") or "")[:200]
            _record_transport_acceptance(settings.db_path, row, task_id)
            accepted += 1
        except Exception as exc:
            _record_transport_error(settings.db_path, row, str(exc))
            failed += 1

    return {
        "ok": True,
        "reason": "dispatched",
        "accepted": accepted,
        "failed": failed,
        "skipped": skipped,
        "considered": len(candidate_rows),
    }


def apply_delivery_result(
    db_path: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        outbox_id = int(payload.get("outbox_id"))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_outbox_id") from exc
    idempotency_key = str(payload.get("idempotency_key") or "")
    result_status = str(payload.get("status") or "")
    if result_status not in {"sent", "failed"}:
        raise ValueError("invalid_delivery_status")
    if not idempotency_key:
        raise ValueError("missing_idempotency_key")

    provider_message_id = str(payload.get("telegram_message_id") or "")[:200] or None
    error_code = str(payload.get("error_code") or "")[:200] or None
    error_text = str(payload.get("error_text") or "")[:2000] or None

    with transaction(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM telegram_notification_outbox
            WHERE id=? AND idempotency_key=?
            """,
            (outbox_id, idempotency_key),
        ).fetchone()
        if not row:
            raise LookupError("outbox_not_found")

        already_terminal = str(row["status"]) in {"sent", "failed"}
        if not already_terminal:
            conn.execute(
                """
                UPDATE telegram_notification_outbox
                SET status=?,last_error=?,next_attempt_at=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    result_status,
                    error_text if result_status == "failed" else None,
                    outbox_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO telegram_notification_deliveries(
                    outbox_id,campaign_id,client_id,status,
                    provider_message_id,error_code,error_text,delivered_at
                ) VALUES (?,?,?,?,?,?,?,CASE WHEN ?='sent' THEN CURRENT_TIMESTAMP ELSE NULL END)
                """,
                (
                    outbox_id,
                    row["campaign_id"],
                    row["client_id"],
                    result_status,
                    provider_message_id,
                    error_code,
                    error_text,
                    result_status,
                ),
            )
            _refresh_campaign_status(conn, row["campaign_id"])

    return {
        "ok": True,
        "outbox_id": outbox_id,
        "status": result_status,
        "duplicate": already_terminal,
    }


def install_telegram_transport(app: FastAPI) -> FastAPI:
    if getattr(app.state, "telegram_transport_installed", False):
        return app
    app.state.telegram_transport_installed = True
    settings = app.state.settings

    @app.post("/api/internal/telegram/delivery-result")
    async def telegram_delivery_result(request: Request):
        secret = str(getattr(settings, "telegram_bridge_secret", "") or "").strip()
        if not secret:
            raise HTTPException(status_code=503, detail="telegram_bridge_not_configured")
        body = await request.body()
        if not verify_transport_signature(
            secret,
            body,
            request.headers.get(TIMESTAMP_HEADER, ""),
            request.headers.get(SIGNATURE_HEADER, ""),
        ):
            raise HTTPException(status_code=401, detail="invalid_transport_signature")
        try:
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("invalid_callback_payload")
            result = apply_delivery_result(settings.db_path, payload)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)

    return app


__all__ = [
    "TelegramTransportError",
    "apply_delivery_result",
    "dispatch_telegram_outbox_once",
    "install_telegram_transport",
    "sign_transport_body",
    "signed_transport_headers",
    "transport_status",
    "verify_transport_signature",
]
