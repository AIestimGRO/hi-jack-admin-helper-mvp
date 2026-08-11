from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.db import connect, transaction
from app.product_shell import _check_csrf, _current_member
from app.services.client_phone_aliases import (
    add_phone_alias,
    assert_phone_available,
    ensure_phone_alias_schema,
    remove_phone_aliases,
)
from app.services.member_accounts import MEMBER_COOKIE_NAME, generate_email_code
from app.services.phone import display_phone, full_phone, normalize_phone
from app.services.quiz_identity import normalize_email
from app.services.quiz_mail import send_member_email_code

_SCHEMA_LOCK = threading.Lock()
_CODE_KINDS = {"change_email", "change_phone", "delete_account"}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def ensure_account_security_schema(conn: sqlite3.Connection) -> None:
    ensure_phone_alias_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS member_account_security_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES member_accounts(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK(kind IN ('change_email','change_phone','delete_account')),
            target_value TEXT NOT NULL DEFAULT '',
            code_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            attempts_left INTEGER NOT NULL DEFAULT 5,
            used_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_member_account_security_codes_lookup
        ON member_account_security_codes(account_id,kind,created_at DESC)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS member_account_security_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES member_accounts(id) ON DELETE CASCADE,
            client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_member_account_security_events_account
        ON member_account_security_events(account_id,created_at DESC)
        """
    )
    conn.execute(
        """
        DELETE FROM member_account_security_codes
        WHERE used_at IS NOT NULL
           OR expires_at < ?
        """,
        ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds"),),
    )


def _code_hash(
    secret_key: str, *, account_id: int, kind: str, target_value: str, code: str
) -> str:
    message = f"member-security:{account_id}:{kind}:{target_value}:{code}".encode("utf-8")
    return hmac.new(secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _expires_at(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(
        timespec="seconds"
    )


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _too_soon(conn: sqlite3.Connection, *, account_id: int, kind: str) -> bool:
    row = conn.execute(
        """
        SELECT created_at FROM member_account_security_codes
        WHERE account_id=? AND kind=?
        ORDER BY id DESC LIMIT 1
        """,
        (account_id, kind),
    ).fetchone()
    if not row:
        return False
    try:
        created = _parse_time(row["created_at"])
    except ValueError:
        return False
    return datetime.now(timezone.utc) - created < timedelta(seconds=60)


def _store_code(
    conn: sqlite3.Connection,
    *,
    secret_key: str,
    account_id: int,
    kind: str,
    target_value: str,
    code: str,
    expires_minutes: int,
) -> None:
    if kind not in _CODE_KINDS:
        raise ValueError("Недопустимый тип подтверждения")
    ensure_account_security_schema(conn)
    conn.execute(
        """
        UPDATE member_account_security_codes
        SET used_at=CURRENT_TIMESTAMP
        WHERE account_id=? AND kind=? AND used_at IS NULL
        """,
        (account_id, kind),
    )
    conn.execute(
        """
        INSERT INTO member_account_security_codes(
            account_id,kind,target_value,code_hash,expires_at
        ) VALUES (?,?,?,?,?)
        """,
        (
            account_id,
            kind,
            target_value,
            _code_hash(
                secret_key,
                account_id=account_id,
                kind=kind,
                target_value=target_value,
                code=code,
            ),
            _expires_at(expires_minutes),
        ),
    )


def _consume_code(
    conn: sqlite3.Connection,
    *,
    secret_key: str,
    account_id: int,
    kind: str,
    code: str,
) -> str:
    ensure_account_security_schema(conn)
    row = conn.execute(
        """
        SELECT * FROM member_account_security_codes
        WHERE account_id=? AND kind=? AND used_at IS NULL
        ORDER BY id DESC LIMIT 1
        """,
        (account_id, kind),
    ).fetchone()
    if not row:
        raise ValueError("Сначала запросите новый код")
    if int(row["attempts_left"] or 0) <= 0:
        raise ValueError("Код заблокирован. Запросите новый")
    try:
        expired = _parse_time(row["expires_at"]) <= datetime.now(timezone.utc)
    except ValueError:
        expired = True
    if expired:
        conn.execute(
            "UPDATE member_account_security_codes SET used_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(row["id"]),),
        )
        raise ValueError("Код истёк. Запросите новый")
    target_value = str(row["target_value"] or "")
    expected = _code_hash(
        secret_key,
        account_id=account_id,
        kind=kind,
        target_value=target_value,
        code=str(code or "").strip(),
    )
    if not hmac.compare_digest(expected, str(row["code_hash"])):
        conn.execute(
            """
            UPDATE member_account_security_codes
            SET attempts_left=MAX(attempts_left-1,0)
            WHERE id=?
            """,
            (int(row["id"]),),
        )
        raise ValueError("Неверный код подтверждения")
    conn.execute(
        "UPDATE member_account_security_codes SET used_at=CURRENT_TIMESTAMP WHERE id=?",
        (int(row["id"]),),
    )
    return target_value


def _event(
    conn: sqlite3.Connection, *, account_id: int, client_id: int, action: str
) -> None:
    ensure_account_security_schema(conn)
    conn.execute(
        """
        INSERT INTO member_account_security_events(account_id,client_id,action)
        VALUES (?,?,?)
        """,
        (account_id, client_id, action),
    )


def apply_email_change(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    client_id: int,
    new_email: str,
    current_session_id: int | None = None,
) -> None:
    normalized = normalize_email(new_email)
    if not normalized:
        raise ValueError("Укажите новую почту")
    conflict = conn.execute(
        "SELECT id FROM member_accounts WHERE email_normalized=? AND id<>? LIMIT 1",
        (normalized, account_id),
    ).fetchone()
    if conflict:
        raise ValueError("Эта почта уже используется другим аккаунтом")
    current = conn.execute(
        "SELECT email_normalized FROM member_accounts WHERE id=? AND is_active=1",
        (account_id,),
    ).fetchone()
    if not current:
        raise ValueError("Аккаунт не найден")
    if str(current["email_normalized"] or "") == normalized:
        raise ValueError("Это уже ваша текущая почта")
    old_email = str(current["email_normalized"] or "")
    conn.execute(
        """
        UPDATE member_accounts
        SET email=?,email_normalized=?,email_verified_at=CURRENT_TIMESTAMP,
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (normalized, normalized, account_id),
    )
    conn.execute(
        """
        UPDATE clients SET email=?,email_normalized=?,updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (normalized, normalized, client_id),
    )
    if current_session_id:
        conn.execute(
            """
            UPDATE member_sessions SET revoked_at=CURRENT_TIMESTAMP
            WHERE account_id=? AND id<>? AND revoked_at IS NULL
            """,
            (account_id, current_session_id),
        )
    if old_email:
        conn.execute("DELETE FROM member_email_codes WHERE email_normalized=?", (old_email,))
    _event(conn, account_id=account_id, client_id=client_id, action="email_changed")


def apply_phone_change(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    client_id: int,
    new_phone: str,
) -> None:
    phone_local = normalize_phone(new_phone)
    if not phone_local:
        raise ValueError("Введите корректный российский номер телефона")
    assert_phone_available(conn, phone_local=phone_local, client_id=client_id)
    current = conn.execute(
        "SELECT phone_local,phone_full,phone_raw FROM clients WHERE id=?",
        (client_id,),
    ).fetchone()
    if not current:
        raise ValueError("Профиль игрока не найден")
    old_phone = normalize_phone(
        current["phone_local"] or current["phone_full"] or current["phone_raw"]
    )
    if old_phone == phone_local:
        raise ValueError("Это уже ваш текущий номер")
    if old_phone:
        add_phone_alias(conn, client_id=client_id, phone_value=old_phone)
    full = full_phone(phone_local)
    conn.execute(
        """
        UPDATE clients
        SET phone_raw=?,phone_full=?,phone_local=?,updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (full, full, phone_local, client_id),
    )
    _event(conn, account_id=account_id, client_id=client_id, action="phone_changed")


def anonymize_account(
    conn: sqlite3.Connection, *, account_id: int, client_id: int
) -> str | None:
    ensure_account_security_schema(conn)
    account = conn.execute(
        "SELECT email_normalized FROM member_accounts WHERE id=?",
        (account_id,),
    ).fetchone()
    if not account:
        raise ValueError("Аккаунт не найден")
    old_email = str(account["email_normalized"] or "")
    avatar_path: str | None = None
    if _table_exists(conn, "member_profile_media"):
        avatar = conn.execute(
            "SELECT avatar_path FROM member_profile_media WHERE account_id=?",
            (account_id,),
        ).fetchone()
        avatar_path = str(avatar["avatar_path"] or "") if avatar else None
        conn.execute("DELETE FROM member_profile_media WHERE account_id=?", (account_id,))

    tombstone = f"deleted-{account_id}-{secrets.token_hex(6)}@deleted.invalid"
    conn.execute(
        """
        UPDATE member_accounts
        SET email=?,email_normalized=?,password_hash='deleted',is_active=0,
            session_version=session_version+1,updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (tombstone, tombstone, account_id),
    )
    conn.execute(
        """
        UPDATE clients SET
            app_user_id=NULL,telegram_id=NULL,referrer_app_user_id=NULL,
            nickname='Удалённый игрок',username=NULL,first_name=NULL,
            phone_raw=NULL,phone_full=NULL,phone_local=NULL,
            telegram_user_id=NULL,email=NULL,email_normalized=NULL,
            client_status='deleted',comment='',updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (client_id,),
    )
    conn.execute("DELETE FROM member_sessions WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM quiz_device_tokens WHERE client_id=?", (client_id,))
    conn.execute(
        "UPDATE member_consents SET ip_hash='deleted',user_agent=NULL WHERE account_id=?",
        (account_id,),
    )
    if _table_exists(conn, "jackside_rules_acceptances"):
        conn.execute(
            """
            UPDATE jackside_rules_acceptances
            SET ip_hash='deleted',user_agent=NULL WHERE account_id=?
            """,
            (account_id,),
        )
    conn.execute(
        "UPDATE quiz_attempts SET ip_hash='deleted',user_agent=NULL WHERE client_id=?",
        (client_id,),
    )
    conn.execute(
        "UPDATE quiz_submissions SET phone_raw='',phone_local='' WHERE client_id=?",
        (client_id,),
    )
    for table in ("hi_jack_rating_entries", "hi_jack_rating_baseline_entries"):
        if _table_exists(conn, table):
            conn.execute(
                f"UPDATE {table} SET phone_local=NULL,phone_raw='' WHERE client_id=?",
                (client_id,),
            )
    remove_phone_aliases(conn, client_id=client_id)
    conn.execute("DELETE FROM member_account_security_codes WHERE account_id=?", (account_id,))
    if old_email:
        conn.execute("DELETE FROM member_email_codes WHERE email_normalized=?", (old_email,))
        conn.execute("DELETE FROM quiz_email_codes WHERE email_normalized=?", (old_email,))
    _event(conn, account_id=account_id, client_id=client_id, action="account_deleted")
    return avatar_path


def _delete_avatar_file(settings, avatar_path: str | None) -> None:
    value = str(avatar_path or "")
    prefix = "/reward-media/profile-avatars/"
    if not value.startswith(prefix):
        return
    target = Path(settings.db_path).resolve().parent / "reward-media" / "profile-avatars" / Path(value).name
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


def _redirect(message: str, *, error: bool = False) -> RedirectResponse:
    key = "error" if error else "ok"
    return RedirectResponse(
        f"/account?{urlencode({'tab': 'profile', key: message})}", status_code=303
    )


def _send_code(settings, *, recipient: str, code: str, purpose: str) -> None:
    if not settings.smtp_host or not settings.smtp_from:
        raise ValueError("Отправка почты сейчас недоступна")
    send_member_email_code(
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        sender=settings.smtp_from,
        starttls=settings.smtp_starttls,
        recipient=recipient,
        code=code,
        purpose=purpose,
        expires_minutes=settings.email_code_minutes,
    )


def install_account_security(app: FastAPI) -> FastAPI:
    if getattr(app.state, "account_security_installed", False):
        return app
    app.state.account_security_installed = True
    app.state.account_security_schema_ready = False
    settings = app.state.settings

    @app.middleware("http")
    async def account_security_schema_middleware(request: Request, call_next):
        if not request.app.state.account_security_schema_ready:
            with _SCHEMA_LOCK:
                if not request.app.state.account_security_schema_ready:
                    with transaction(settings.db_path) as conn:
                        ensure_account_security_schema(conn)
                    request.app.state.account_security_schema_ready = True
        return await call_next(request)

    @app.get("/api/account/security-state")
    async def account_security_state(request: Request):
        member = _current_member(request, required=True)
        return JSONResponse(
            {
                "email": str(member["email"] or ""),
                "phone": display_phone(member["phone_local"], member["phone_raw"]),
            }
        )

    @app.post("/account/security/email/request")
    async def request_email_change(
        request: Request,
        new_email: str = Form(...),
        csrf_token: str = Form(...),
    ):
        member = _current_member(request, required=True)
        _check_csrf(request, csrf_token)
        try:
            normalized = normalize_email(new_email)
            if not normalized:
                raise ValueError("Укажите новую почту")
        except ValueError:
            return _redirect("Введите корректный адрес электронной почты", error=True)
        account_id = int(member["id"])
        with connect(settings.db_path) as conn:
            conflict = conn.execute(
                "SELECT id FROM member_accounts WHERE email_normalized=? AND id<>? LIMIT 1",
                (normalized, account_id),
            ).fetchone()
            if conflict:
                return _redirect("Эта почта уже используется другим аккаунтом", error=True)
            if normalized == str(member["email_normalized"] or ""):
                return _redirect("Это уже ваша текущая почта", error=True)
            if _too_soon(conn, account_id=account_id, kind="change_email"):
                return _redirect("Новый код можно запросить через минуту", error=True)
        code = generate_email_code()
        try:
            _send_code(settings, recipient=normalized, code=code, purpose="change_email")
        except Exception:
            return _redirect("Не удалось отправить код на новую почту", error=True)
        with transaction(settings.db_path) as conn:
            conflict = conn.execute(
                "SELECT id FROM member_accounts WHERE email_normalized=? AND id<>? LIMIT 1",
                (normalized, account_id),
            ).fetchone()
            if conflict:
                return _redirect("Эта почта уже используется другим аккаунтом", error=True)
            _store_code(
                conn,
                secret_key=settings.secret_key,
                account_id=account_id,
                kind="change_email",
                target_value=normalized,
                code=code,
                expires_minutes=settings.email_code_minutes,
            )
        return _redirect("Код отправлен на новую почту")

    @app.post("/account/security/email/confirm")
    async def confirm_email_change(
        request: Request,
        code: str = Form(...),
        csrf_token: str = Form(...),
    ):
        member = _current_member(request, required=True)
        _check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                target = _consume_code(
                    conn,
                    secret_key=settings.secret_key,
                    account_id=int(member["id"]),
                    kind="change_email",
                    code=code,
                )
                apply_email_change(
                    conn,
                    account_id=int(member["id"]),
                    client_id=int(member["client_id"]),
                    new_email=target,
                    current_session_id=int(member["member_session_id"]),
                )
        except ValueError as exc:
            return _redirect(str(exc), error=True)
        return _redirect("Почта изменена")

    @app.post("/account/security/phone/request")
    async def request_phone_change(
        request: Request,
        new_phone: str = Form(...),
        csrf_token: str = Form(...),
    ):
        member = _current_member(request, required=True)
        _check_csrf(request, csrf_token)
        phone_local = normalize_phone(new_phone)
        if not phone_local:
            return _redirect("Введите корректный российский номер телефона", error=True)
        if phone_local == normalize_phone(member["phone_local"] or member["phone_raw"]):
            return _redirect("Это уже ваш текущий номер", error=True)
        account_id = int(member["id"])
        try:
            with connect(settings.db_path) as conn:
                assert_phone_available(
                    conn, phone_local=phone_local, client_id=int(member["client_id"])
                )
                if _too_soon(conn, account_id=account_id, kind="change_phone"):
                    return _redirect("Новый код можно запросить через минуту", error=True)
        except ValueError as exc:
            return _redirect(str(exc), error=True)
        code = generate_email_code()
        try:
            _send_code(
                settings,
                recipient=str(member["email"]),
                code=code,
                purpose="change_phone",
            )
        except Exception:
            return _redirect("Не удалось отправить код на привязанную почту", error=True)
        with transaction(settings.db_path) as conn:
            assert_phone_available(
                conn, phone_local=phone_local, client_id=int(member["client_id"])
            )
            _store_code(
                conn,
                secret_key=settings.secret_key,
                account_id=account_id,
                kind="change_phone",
                target_value=phone_local,
                code=code,
                expires_minutes=settings.email_code_minutes,
            )
        return _redirect("Код подтверждения отправлен на текущую почту")

    @app.post("/account/security/phone/confirm")
    async def confirm_phone_change(
        request: Request,
        code: str = Form(...),
        csrf_token: str = Form(...),
    ):
        member = _current_member(request, required=True)
        _check_csrf(request, csrf_token)
        try:
            with transaction(settings.db_path) as conn:
                target = _consume_code(
                    conn,
                    secret_key=settings.secret_key,
                    account_id=int(member["id"]),
                    kind="change_phone",
                    code=code,
                )
                apply_phone_change(
                    conn,
                    account_id=int(member["id"]),
                    client_id=int(member["client_id"]),
                    new_phone=target,
                )
        except ValueError as exc:
            return _redirect(str(exc), error=True)
        return _redirect("Номер телефона изменён")

    @app.post("/account/security/delete/request")
    async def request_account_delete(
        request: Request,
        csrf_token: str = Form(...),
    ):
        member = _current_member(request, required=True)
        _check_csrf(request, csrf_token)
        account_id = int(member["id"])
        with connect(settings.db_path) as conn:
            if _too_soon(conn, account_id=account_id, kind="delete_account"):
                return _redirect("Новый код можно запросить через минуту", error=True)
        code = generate_email_code()
        try:
            _send_code(
                settings,
                recipient=str(member["email"]),
                code=code,
                purpose="delete_account",
            )
        except Exception:
            return _redirect("Не удалось отправить код удаления", error=True)
        with transaction(settings.db_path) as conn:
            _store_code(
                conn,
                secret_key=settings.secret_key,
                account_id=account_id,
                kind="delete_account",
                target_value="",
                code=code,
                expires_minutes=settings.email_code_minutes,
            )
        return _redirect("Код удаления отправлен на привязанную почту")

    @app.post("/account/security/delete/confirm")
    async def confirm_account_delete(
        request: Request,
        code: str = Form(...),
        confirmation: str = Form(...),
        csrf_token: str = Form(...),
    ):
        member = _current_member(request, required=True)
        _check_csrf(request, csrf_token)
        if " ".join(str(confirmation or "").split()).upper() != "УДАЛИТЬ":
            return _redirect("Для удаления введите слово УДАЛИТЬ", error=True)
        try:
            with transaction(settings.db_path) as conn:
                _consume_code(
                    conn,
                    secret_key=settings.secret_key,
                    account_id=int(member["id"]),
                    kind="delete_account",
                    code=code,
                )
                avatar_path = anonymize_account(
                    conn,
                    account_id=int(member["id"]),
                    client_id=int(member["client_id"]),
                )
        except ValueError as exc:
            return _redirect(str(exc), error=True)
        _delete_avatar_file(settings, avatar_path)
        response = RedirectResponse("/account/login?deleted=1", status_code=303)
        response.delete_cookie(MEMBER_COOKIE_NAME, path="/")
        return response

    return app


__all__ = [
    "anonymize_account",
    "apply_email_change",
    "apply_phone_change",
    "ensure_account_security_schema",
    "install_account_security",
]
