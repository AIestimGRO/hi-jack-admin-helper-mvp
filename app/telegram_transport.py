from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from fastapi import FastAPI

from app.db import connect, transaction


MAX_TRANSPORT_ATTEMPTS = 5
TELEGRAM_PRIVATE_USER_ID_MAX = 0xFFFFFFFFFF


class TelegramTransportError(RuntimeError):
    pass


class TelegramPermanentError(TelegramTransportError):
    pass


class TelegramRateLimitError(TelegramTransportError):
    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TelegramConfigurationError(TelegramTransportError):
    pass


def validate_private_chat_id(chat_id: str) -> str:
    """Validate a private Telegram user/chat ID before any Bot API request.

    Notification Center sends direct messages to users authorized through
    ``telegram:bot_access``. Telegram user IDs are positive integers in the
    documented MTProto/Bot API user-ID range. This also rejects stale OIDC
    ``sub`` values that were previously stored as if they were chat IDs.
    """
    raw = str(chat_id or "").strip()
    if not raw or not raw.isascii() or not raw.isdigit():
        raise TelegramPermanentError("telegram_private_chat_id_invalid")
    try:
        value = int(raw)
    except ValueError as exc:
        raise TelegramPermanentError("telegram_private_chat_id_invalid") from exc
    if value < 1 or value > TELEGRAM_PRIVATE_USER_ID_MAX:
        raise TelegramPermanentError("telegram_private_chat_id_invalid")
    return str(value)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sqlite_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _decode_error_payload(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _raise_telegram_api_error(
    error_code: int,
    description: str,
    parameters: dict[str, Any] | None = None,
) -> None:
    clean_description = str(description or "telegram_api_error")[:1000]
    if error_code == 429:
        retry_after = None
        if isinstance(parameters, dict):
            try:
                retry_after = int(parameters.get("retry_after"))
            except (TypeError, ValueError):
                retry_after = None
        raise TelegramRateLimitError(clean_description, retry_after)
    if error_code in {401, 404}:
        raise TelegramConfigurationError(clean_description)
    if error_code in {400, 403}:
        raise TelegramPermanentError(clean_description)
    raise TelegramTransportError(f"telegram_api_{error_code}: {clean_description}")


def _default_send_message(
    bot_token: str,
    chat_id: str,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    token = str(bot_token or "").strip()
    if not token:
        raise TelegramConfigurationError("telegram_bot_token_missing")

    text = str(payload.get("text") or "").strip()
    if not text:
        raise TelegramPermanentError("outbox_text_missing")

    safe_chat_id = validate_private_chat_id(chat_id)
    request_payload: dict[str, Any] = {
        "chat_id": safe_chat_id,
        "text": text,
    }
    button_text = str(payload.get("button_text") or "").strip()
    button_url = str(payload.get("button_url") or "").strip()
    if button_text or button_url:
        if not button_text or not button_url:
            raise TelegramPermanentError("button_text_and_url_must_be_set_together")
        request_payload["reply_markup"] = {
            "inline_keyboard": [[{"text": button_text, "url": button_url}]]
        }

    body = json.dumps(
        request_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = UrlRequest(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_body = response.read()
    except HTTPError as exc:
        raw = exc.read()
        error_payload = _decode_error_payload(raw)
        _raise_telegram_api_error(
            int(error_payload.get("error_code") or exc.code),
            str(error_payload.get("description") or exc.reason or "telegram_http_error"),
            error_payload.get("parameters") if isinstance(error_payload, dict) else None,
        )
        raise AssertionError("unreachable")
    except URLError as exc:
        raise TelegramTransportError("telegram_network_error") from exc
    except TimeoutError as exc:
        raise TelegramTransportError("telegram_timeout") from exc

    decoded = _decode_error_payload(response_body)
    if not decoded:
        raise TelegramTransportError("telegram_invalid_json")
    if not bool(decoded.get("ok")):
        _raise_telegram_api_error(
            int(decoded.get("error_code") or 500),
            str(decoded.get("description") or "telegram_api_error"),
            decoded.get("parameters") if isinstance(decoded, dict) else None,
        )
    result = decoded.get("result")
    if not isinstance(result, dict):
        raise TelegramTransportError("telegram_result_missing")
    return result


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
    if not str(getattr(settings, "telegram_bot_token", "") or "").strip():
        return False, "bot_token_missing"
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
        "bot_token_configured": bool(
            str(getattr(settings, "telegram_bot_token", "") or "").strip()
        ),
    }


def _claim_outbox_row(db_path: Any, outbox_id: int) -> dict[str, Any] | None:
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


def _record_send_success(
    db_path: Any,
    row: dict[str, Any],
    provider_message_id: str,
) -> None:
    with transaction(db_path) as conn:
        conn.execute(
            """
            UPDATE telegram_notification_outbox
            SET status='sent',next_attempt_at=NULL,last_error=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (int(row["id"]),),
        )
        conn.execute(
            """
            INSERT INTO telegram_notification_deliveries(
                outbox_id,campaign_id,client_id,status,
                provider_message_id,delivered_at
            ) VALUES (?,?,?,'sent',?,CURRENT_TIMESTAMP)
            """,
            (
                int(row["id"]),
                row.get("campaign_id"),
                int(row["client_id"]),
                provider_message_id or None,
            ),
        )
        _refresh_campaign_status(conn, row.get("campaign_id"))


def _record_send_error(
    db_path: Any,
    row: dict[str, Any],
    error_text: str,
    *,
    permanent: bool = False,
    retry_after: int | None = None,
    error_code: str = "telegram_dispatch",
) -> bool:
    attempts = int(row.get("attempts") or 0) + 1
    final_failure = permanent or attempts >= MAX_TRANSPORT_ATTEMPTS
    next_attempt_at = None
    if not final_failure:
        delay = max(1, int(retry_after or _backoff_seconds(attempts)))
        next_attempt_at = _sqlite_datetime(_utc_now() + timedelta(seconds=delay))
    status = "failed" if final_failure else "queued"
    delivery_status = "failed" if final_failure else "transport_error"
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
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                int(row["id"]),
                row.get("campaign_id"),
                int(row["client_id"]),
                delivery_status,
                error_code[:200],
                error_text[:2000],
            ),
        )
        _refresh_campaign_status(conn, row.get("campaign_id"))
    return final_failure


def dispatch_telegram_outbox_once(
    settings: Any,
    *,
    limit: int = 20,
    sender: Callable[[str, str, dict[str, Any], float], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ready, reason = _transport_gate(settings)
    if not ready:
        return {
            "ok": False,
            "reason": reason,
            "sent": 0,
            "failed": 0,
            "retrying": 0,
        }

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

    send_message = sender or _default_send_message
    sent = 0
    failed = 0
    retrying = 0
    skipped = 0
    blocked = False
    for candidate in candidate_rows:
        row = _claim_outbox_row(settings.db_path, int(candidate["id"]))
        if not row:
            skipped += 1
            continue
        try:
            payload = json.loads(str(row.get("payload_json") or "{}"))
            if not isinstance(payload, dict):
                raise TelegramPermanentError("outbox_payload_invalid")
            result = send_message(
                str(settings.telegram_bot_token),
                str(row["telegram_chat_id"]),
                payload,
                float(getattr(settings, "telegram_transport_timeout_seconds", 10.0)),
            )
            message_id = str(result.get("message_id") or "")[:200]
            _record_send_success(settings.db_path, row, message_id)
            sent += 1
        except TelegramPermanentError as exc:
            _record_send_error(
                settings.db_path,
                row,
                str(exc),
                permanent=True,
                error_code="telegram_permanent",
            )
            failed += 1
        except TelegramRateLimitError as exc:
            _record_send_error(
                settings.db_path,
                row,
                str(exc),
                retry_after=exc.retry_after,
                error_code="telegram_rate_limit",
            )
            retrying += 1
        except TelegramConfigurationError as exc:
            _record_send_error(
                settings.db_path,
                row,
                str(exc),
                error_code="telegram_configuration",
            )
            retrying += 1
            blocked = True
            break
        except Exception as exc:
            final_failure = _record_send_error(
                settings.db_path,
                row,
                str(exc),
                error_code="telegram_transport",
            )
            if final_failure:
                failed += 1
            else:
                retrying += 1

    return {
        "ok": True,
        "reason": "configuration_blocked" if blocked else "dispatched",
        "sent": sent,
        "failed": failed,
        "retrying": retrying,
        "skipped": skipped,
        "considered": len(candidate_rows),
    }


def install_telegram_transport(app: FastAPI) -> FastAPI:
    if getattr(app.state, "telegram_transport_installed", False):
        return app
    app.state.telegram_transport_installed = True
    return app


__all__ = [
    "TelegramConfigurationError",
    "TelegramPermanentError",
    "TelegramRateLimitError",
    "TelegramTransportError",
    "dispatch_telegram_outbox_once",
    "install_telegram_transport",
    "transport_status",
    "validate_private_chat_id",
]
