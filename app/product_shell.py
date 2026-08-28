from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request as UrlRequest, urlopen
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

from app.config import BASE_DIR
from app.db import connect, transaction
from app.services.member_accounts import (
    MEMBER_COOKIE_NAME,
    authenticated_member,
)

PROFILE_AVATAR_SIZE = 512
ENGAGEMENT_ICON_SIZE = 512
MAX_AVATAR_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ICON_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_PARTNER_TOURNAMENT_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_PARTNER_TOURNAMENT_ICON_BYTES = 5 * 1024 * 1024
_SCHEMA_LOCK = threading.Lock()
_TOURNAMENT_SYNC_LOCK = threading.Lock()
_TOURNAMENT_SYNC_LAST_ATTEMPT = 0.0


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(str(row[1]) == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _ensure_product_shell_schema(db_path: str | Path) -> None:
    with transaction(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS member_profile_media (
                account_id INTEGER PRIMARY KEY
                    REFERENCES member_accounts(id) ON DELETE CASCADE,
                client_id INTEGER NOT NULL
                    REFERENCES clients(id) ON DELETE CASCADE,
                avatar_path TEXT,
                avatar_kind TEXT CHECK(avatar_kind IN ('photo', 'sticker')),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_member_profile_media_client
            ON member_profile_media(client_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS club_tournaments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                format_text TEXT NOT NULL DEFAULT '',
                buy_in_text TEXT NOT NULL DEFAULT '',
                max_slots INTEGER NOT NULL DEFAULT 0 CHECK(max_slots >= 0),
                registration_open INTEGER NOT NULL DEFAULT 1,
                is_published INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK(status IN ('draft', 'scheduled', 'registration_closed', 'completed', 'cancelled')),
                created_by_admin_id INTEGER REFERENCES admins(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_club_tournaments_public_schedule
            ON club_tournaments(is_published, status, starts_at)
            """
        )
        tournament_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(club_tournaments)")
        }
        for column, declaration in (
            ("source_kind", "TEXT"),
            ("external_id", "TEXT"),
            ("external_url", "TEXT"),
            ("external_icon_url", "TEXT"),
            ("source_synced_at", "TEXT"),
        ):
            if column not in tournament_columns:
                conn.execute(
                    f"ALTER TABLE club_tournaments ADD COLUMN {column} {declaration}"
                )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_club_tournaments_source
            ON club_tournaments(source_kind, external_id)
            """
        )
        if not _has_column(conn, "title_definitions", "icon_path"):
            conn.execute("ALTER TABLE title_definitions ADD COLUMN icon_path TEXT")
        if not _has_column(conn, "achievement_definitions", "icon_path"):
            conn.execute("ALTER TABLE achievement_definitions ADD COLUMN icon_path TEXT")


def _media_root(settings: Any) -> Path:
    root = Path(settings.db_path).resolve().parent / "reward-media"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _member_media_dir(settings: Any) -> Path:
    path = _media_root(settings) / "profile-avatars"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _engagement_media_dir(settings: Any) -> Path:
    path = _media_root(settings) / "engagement-icons"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _csrf_token(request: Request) -> str:
    token = str(request.session.get("csrf") or "")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def _check_csrf(request: Request, token: str) -> None:
    expected = str(request.session.get("csrf") or "")
    if not expected or not hmac.compare_digest(expected, str(token or "")):
        raise HTTPException(status_code=403, detail="invalid_csrf_token")


def _current_member(request: Request, *, required: bool = True) -> sqlite3.Row | None:
    settings = request.app.state.settings
    token = str(request.cookies.get(MEMBER_COOKIE_NAME) or "")
    with transaction(settings.db_path) as conn:
        member = authenticated_member(
            conn,
            secret_key=settings.secret_key,
            token=token,
        )
    if required and not member:
        raise HTTPException(
            status_code=303,
            detail="member_login_required",
            headers={"Location": "/account/login"},
        )
    return member


def _require_master(request: Request) -> None:
    if not request.session.get("authenticated") or request.session.get("admin_role") != "master_admin":
        raise HTTPException(status_code=403, detail="Доступ только для мастер-администратора")
    admin_id = request.session.get("admin_id")
    with connect(request.app.state.settings.db_path) as conn:
        row = conn.execute(
            "SELECT role,is_active,session_version FROM admins WHERE id=?",
            (admin_id,),
        ).fetchone()
    if not row or not row["is_active"] or row["role"] != "master_admin":
        raise HTTPException(status_code=403, detail="Доступ только для мастер-администратора")
    if int(row["session_version"]) != int(request.session.get("admin_session_version") or -1):
        raise HTTPException(status_code=401, detail="authentication_required")


def _member_redirect(message: str, *, error: bool = False, tab: str = "profile") -> RedirectResponse:
    key = "error" if error else "ok"
    return RedirectResponse(
        f"/account?{urlencode({'tab': tab, key: message})}",
        status_code=303,
    )


def _admin_icon_redirect(message: str, *, error: bool = False) -> RedirectResponse:
    key = "error" if error else "ok"
    return RedirectResponse(
        f"/master/engagement-icons?{urlencode({key: message})}",
        status_code=303,
    )


def _safe_back_path(value: str) -> str:
    candidate = str(value or "").strip()
    if not candidate.startswith("/account") or candidate.startswith("//"):
        return "/account"
    return candidate[:1000]


def _normalized_nickname(value: str) -> str:
    clean = " ".join(str(value or "").split())
    if not clean:
        return ""
    if len(clean) > 40:
        raise ValueError("Прозвище должно быть не длиннее 40 символов")
    if any(ord(character) < 32 for character in clean):
        raise ValueError("Прозвище содержит недопустимые символы")
    return clean


async def _read_upload(upload: UploadFile, *, max_bytes: int) -> bytes:
    data = await upload.read(max_bytes + 1)
    if not data:
        raise ValueError("Выберите изображение")
    if len(data) > max_bytes:
        raise ValueError("Файл слишком большой")
    return data


def _open_image(data: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("Файл не является поддерживаемым изображением") from exc
    if image.width < 32 or image.height < 32:
        raise ValueError("Изображение слишком маленькое")
    if image.width > 8192 or image.height > 8192:
        raise ValueError("Изображение слишком большое")
    return image


def _prepare_avatar(data: bytes, kind: str) -> tuple[bytes, str, str]:
    image = _open_image(data)
    if kind == "sticker":
        if image.format not in {"PNG", "WEBP"}:
            raise ValueError("Для стикера используйте PNG или WEBP")
        image = image.convert("RGBA")
        image.thumbnail((PROFILE_AVATAR_SIZE, PROFILE_AVATAR_SIZE), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (PROFILE_AVATAR_SIZE, PROFILE_AVATAR_SIZE), (0, 0, 0, 0))
        canvas.alpha_composite(
            image,
            ((PROFILE_AVATAR_SIZE - image.width) // 2, (PROFILE_AVATAR_SIZE - image.height) // 2),
        )
        output = io.BytesIO()
        canvas.save(output, format="PNG", optimize=True)
        return output.getvalue(), ".png", "image/png"

    image = ImageOps.exif_transpose(image).convert("RGB")
    image = ImageOps.fit(
        image,
        (PROFILE_AVATAR_SIZE, PROFILE_AVATAR_SIZE),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=88, optimize=True, progressive=True)
    return output.getvalue(), ".jpg", "image/jpeg"


def _prepare_engagement_icon(data: bytes) -> bytes:
    image = _open_image(data).convert("RGBA")
    image.thumbnail((ENGAGEMENT_ICON_SIZE, ENGAGEMENT_ICON_SIZE), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (ENGAGEMENT_ICON_SIZE, ENGAGEMENT_ICON_SIZE), (0, 0, 0, 0))
    canvas.alpha_composite(
        image,
        ((ENGAGEMENT_ICON_SIZE - image.width) // 2, (ENGAGEMENT_ICON_SIZE - image.height) // 2),
    )
    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _delete_managed_file(settings: Any, web_path: str | None, folder: str) -> None:
    value = str(web_path or "")
    prefix = f"/reward-media/{folder}/"
    if not value.startswith(prefix):
        return
    filename = Path(value).name
    target = _media_root(settings) / folder / filename
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


def _avatar_record(conn: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM member_profile_media WHERE account_id=?",
        (account_id,),
    ).fetchone()


def _partner_setting(
    settings: Any,
    attribute: str,
    environment: str,
    default: str,
) -> str:
    configured = getattr(settings, attribute, None)
    if configured is not None:
        return str(configured).strip()
    return os.getenv(environment, default).strip()


def _partner_tournaments_url(settings: Any) -> str:
    return _partner_setting(
        settings,
        "partner_tournaments_url",
        "HJC_PARTNER_TOURNAMENTS_URL",
        "https://hi-jack-club.matthew-0203.ru/api/tournaments/partner/tournaments/?status=IN_QUEUE",
    )


def _partner_api_key(settings: Any) -> str:
    return _partner_setting(
        settings,
        "partner_api_key",
        "HJC_PARTNER_API_KEY",
        "",
    )


def _partner_launch_url_template(settings: Any) -> str:
    return _partner_setting(
        settings,
        "partner_tournament_launch_url_template",
        "HJC_PARTNER_TOURNAMENT_LAUNCH_URL_TEMPLATE",
        "https://t.me/HJCapp_bot/app?startapp=tournament_{id}",
    )


def _partner_sync_seconds(settings: Any) -> int:
    raw = _partner_setting(
        settings,
        "partner_tournament_sync_seconds",
        "HJC_PARTNER_TOURNAMENT_SYNC_SECONDS",
        "60",
    )
    try:
        value = int(raw)
    except ValueError:
        value = 60
    return max(10, min(value, 3600))


def _partner_timeout_seconds(settings: Any) -> float:
    raw = _partner_setting(
        settings,
        "partner_tournament_timeout_seconds",
        "HJC_PARTNER_TOURNAMENT_TIMEOUT_SECONDS",
        "4",
    )
    try:
        value = float(raw)
    except ValueError:
        value = 4.0
    return max(0.5, min(value, 15.0))


def _partner_payload_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("results", "data", "tournaments"):
            items = payload.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    raise ValueError("partner_tournament_payload_invalid")


def _normalized_partner_start(value: Any, timezone_name: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError("partner_tournament_start_missing")
    try:
        parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("partner_tournament_start_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _partner_icon_value(item: dict[str, Any]) -> str:
    for key in ("icon_url", "icon", "image_url", "image", "logo_url", "logo"):
        value = item.get(key)
        if isinstance(value, dict):
            value = value.get("url") or value.get("src") or value.get("file")
        clean = str(value or "").strip()
        if clean:
            return clean
    return ""


def _partner_icon_url(item: dict[str, Any], settings: Any) -> str:
    raw = _partner_icon_value(item)
    if not raw:
        return ""
    candidate = urljoin(_partner_tournaments_url(settings), raw)
    parsed = urlparse(candidate)
    if parsed.scheme != "https" or not parsed.netloc:
        return ""
    return candidate[:1000]


def _partner_icon_is_same_partner_origin(icon_url: str, settings: Any) -> bool:
    icon = urlparse(str(icon_url or "").strip())
    partner = urlparse(_partner_tournaments_url(settings))
    if icon.scheme != "https" or not icon.hostname or not partner.hostname:
        return False
    if icon.hostname.lower() != partner.hostname.lower():
        return False
    icon_port = icon.port or (443 if icon.scheme == "https" else None)
    partner_port = partner.port or (443 if partner.scheme == "https" else 80)
    return icon_port == partner_port


def _partner_icon_proxy_url(external_id: str, source_url: str) -> str:
    version = hashlib.sha256(str(source_url).encode("utf-8")).hexdigest()[:12]
    return (
        f"/api/account/tournaments/{quote(str(external_id), safe='')}/icon"
        f"?v={version}"
    )


def _fetch_partner_tournament_icon_png(
    settings: Any,
    icon_url: str,
    *,
    urlopen_func: Any = urlopen,
) -> bytes:
    if not _partner_icon_is_same_partner_origin(icon_url, settings):
        raise ValueError("partner_tournament_icon_origin_invalid")

    headers = {
        "Accept": "image/*",
        "User-Agent": "HiJackScheduleSync/1.0",
    }
    api_key = _partner_api_key(settings)
    if api_key:
        headers["X-Partner-Api-Key"] = api_key

    request = UrlRequest(
        str(icon_url),
        headers=headers,
        method="GET",
    )
    with urlopen_func(
        request,
        timeout=_partner_timeout_seconds(settings),
    ) as response:
        raw = response.read(MAX_PARTNER_TOURNAMENT_ICON_BYTES + 1)
    if not raw:
        raise ValueError("partner_tournament_icon_empty")
    if len(raw) > MAX_PARTNER_TOURNAMENT_ICON_BYTES:
        raise ValueError("partner_tournament_icon_too_large")
    return _prepare_engagement_icon(raw)


def _partner_tournament_rows(payload: Any, settings: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in _partner_payload_items(payload):
        external_id = str(item.get("id") or "").strip()
        title = " ".join(str(item.get("title") or "").split())
        if not external_id or not title:
            continue
        status = str(item.get("status") or "IN_QUEUE").strip().upper()
        if status != "IN_QUEUE":
            continue
        try:
            starts_at = _normalized_partner_start(
                item.get("started_at"),
                settings.timezone_name,
            )
        except ValueError:
            continue

        location = " ".join(str(item.get("location") or "").split())
        features = item.get("features")
        feature_items = (
            [" ".join(str(value).split()) for value in features if str(value).strip()]
            if isinstance(features, list)
            else []
        )
        meta = feature_items[:2]
        max_slots_raw = item.get("max_participants") or 0
        try:
            max_slots = max(0, int(max_slots_raw))
        except (TypeError, ValueError):
            max_slots = 0
        template = _partner_launch_url_template(settings)
        try:
            external_url = template.format(id=external_id) if template else ""
        except (KeyError, ValueError):
            external_url = ""
        external_icon_url = _partner_icon_url(item, settings)

        rows.append(
            {
                "external_id": external_id,
                "title": title[:100],
                "starts_at": starts_at,
                "description": location[:300],
                "format_text": " · ".join(meta)[:300],
                "max_slots": max_slots,
                "external_url": external_url[:1000],
                "external_icon_url": external_icon_url,
            }
        )
    return rows


def _fetch_partner_tournament_payload(
    settings: Any,
    *,
    urlopen_func: Any = urlopen,
) -> Any:
    url = _partner_tournaments_url(settings)
    api_key = _partner_api_key(settings)
    if not url or not api_key:
        return None

    request = UrlRequest(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "HiJackScheduleSync/1.0",
            "X-Partner-Api-Key": api_key,
        },
        method="GET",
    )
    with urlopen_func(
        request,
        timeout=_partner_timeout_seconds(settings),
    ) as response:
        raw = response.read(MAX_PARTNER_TOURNAMENT_PAYLOAD_BYTES + 1)
    if len(raw) > MAX_PARTNER_TOURNAMENT_PAYLOAD_BYTES:
        raise ValueError("partner_tournament_payload_too_large")
    return json.loads(raw.decode("utf-8"))


def _upsert_partner_tournaments(
    db_path: str | Path,
    rows: list[dict[str, Any]],
) -> int:
    _ensure_product_shell_schema(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            """
            UPDATE club_tournaments
            SET is_published=0,
                registration_open=0,
                status='registration_closed',
                updated_at=CURRENT_TIMESTAMP
            WHERE source_kind='miniapp'
            """
        )
        for item in rows:
            conn.execute(
                """
                INSERT INTO club_tournaments(
                    title,starts_at,description,format_text,buy_in_text,max_slots,
                    registration_open,is_published,status,source_kind,external_id,
                    external_url,external_icon_url,source_synced_at
                ) VALUES (?,?,?,?,?,?,
                          1,1,'scheduled','miniapp',?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(source_kind,external_id) DO UPDATE SET
                    title=excluded.title,
                    starts_at=excluded.starts_at,
                    description=excluded.description,
                    format_text=excluded.format_text,
                    buy_in_text=excluded.buy_in_text,
                    max_slots=excluded.max_slots,
                    registration_open=1,
                    is_published=1,
                    status='scheduled',
                    external_url=excluded.external_url,
                    external_icon_url=excluded.external_icon_url,
                    source_synced_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    item["title"],
                    item["starts_at"],
                    item["description"],
                    item["format_text"],
                    "",
                    item["max_slots"],
                    item["external_id"],
                    item["external_url"],
                    item["external_icon_url"],
                ),
            )
    return len(rows)


def _sync_partner_tournaments(settings: Any) -> int | None:
    payload = _fetch_partner_tournament_payload(settings)
    if payload is None:
        return None
    rows = _partner_tournament_rows(payload, settings)
    return _upsert_partner_tournaments(settings.db_path, rows)


def _maybe_sync_partner_tournaments(settings: Any) -> int | None:
    global _TOURNAMENT_SYNC_LAST_ATTEMPT
    if not _partner_api_key(settings):
        return None
    now = time.monotonic()
    with _TOURNAMENT_SYNC_LOCK:
        if now - _TOURNAMENT_SYNC_LAST_ATTEMPT < _partner_sync_seconds(settings):
            return None
        _TOURNAMENT_SYNC_LAST_ATTEMPT = now
        try:
            return _sync_partner_tournaments(settings)
        except Exception:
            logging.exception("partner tournament sync failed")
            return None


def _public_tournaments(
    conn: sqlite3.Connection,
    *,
    settings: Any | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT * FROM club_tournaments
        WHERE is_published=1
          AND status='scheduled'
          AND starts_at>=?
        ORDER BY starts_at,id
        LIMIT ?
        """,
        (now, max(1, min(int(limit), 50))),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        source_url = str(item.get("external_icon_url") or "").strip()
        external_id = str(item.get("external_id") or "").strip()
        if (
            settings is not None
            and item.get("source_kind") == "miniapp"
            and external_id
            and source_url
            and _partner_icon_is_same_partner_origin(source_url, settings)
        ):
            item["external_icon_url"] = _partner_icon_proxy_url(
                external_id,
                source_url,
            )
        result.append(item)
    return result


def install_product_shell(app: FastAPI) -> FastAPI:
    if getattr(app.state, "product_shell_installed", False):
        return app
    app.state.product_shell_installed = True
    app.state.product_shell_schema_ready = False
    settings = app.state.settings
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

    @app.middleware("http")
    async def product_shell_schema_middleware(request: Request, call_next):
        if not request.app.state.product_shell_schema_ready:
            with _SCHEMA_LOCK:
                if not request.app.state.product_shell_schema_ready:
                    _ensure_product_shell_schema(settings.db_path)
                    _member_media_dir(settings)
                    _engagement_media_dir(settings)
                    request.app.state.product_shell_schema_ready = True
        return await call_next(request)

    @app.get("/api/account/tournaments/{external_id}/icon")
    async def account_partner_tournament_icon(request: Request, external_id: str):
        _current_member(request, required=True)
        with connect(settings.db_path) as conn:
            tournament = conn.execute(
                """
                SELECT external_icon_url
                FROM club_tournaments
                WHERE source_kind='miniapp' AND external_id=?
                  AND is_published=1
                LIMIT 1
                """,
                (str(external_id),),
            ).fetchone()
        source_url = (
            str(tournament["external_icon_url"] or "").strip()
            if tournament
            else ""
        )
        if not source_url or not _partner_icon_is_same_partner_origin(
            source_url,
            settings,
        ):
            raise HTTPException(status_code=404, detail="tournament_icon_not_found")
        try:
            image = await run_in_threadpool(
                _fetch_partner_tournament_icon_png,
                settings,
                source_url,
            )
        except Exception:
            logging.exception(
                "partner tournament icon fetch failed for external_id=%s",
                external_id,
            )
            raise HTTPException(
                status_code=404,
                detail="tournament_icon_unavailable",
            ) from None
        return Response(
            image,
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=300"},
        )

    @app.get("/api/account/product-shell")
    async def account_product_shell(request: Request):
        member = _current_member(request, required=True)
        await run_in_threadpool(_maybe_sync_partner_tournaments, settings)
        with connect(settings.db_path) as conn:
            avatar = _avatar_record(conn, int(member["id"]))
            tournaments = _public_tournaments(conn, settings=settings)
        nickname = str(member["nickname"] or "").strip()
        return JSONResponse(
            {
                "profile": {
                    "nickname": nickname,
                    "avatar_url": "/account/avatar" if avatar and avatar["avatar_path"] else None,
                    "avatar_kind": str(avatar["avatar_kind"] or "") if avatar else None,
                },
                "tournaments": tournaments,
                "nearest_tournament": tournaments[0] if tournaments else None,
                "chat": {"unread": 0},
            }
        )

    @app.post("/account/profile/update")
    async def account_profile_update(
        request: Request,
        nickname: str = Form(""),
        csrf_token: str = Form(...),
    ):
        member = _current_member(request, required=True)
        _check_csrf(request, csrf_token)
        try:
            clean = _normalized_nickname(nickname)
        except ValueError as exc:
            return _member_redirect(str(exc), error=True)
        with transaction(settings.db_path) as conn:
            conn.execute(
                "UPDATE clients SET nickname=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (clean or None, int(member["client_id"])),
            )
        return _member_redirect("Прозвище обновлено")

    @app.post("/account/profile/avatar")
    async def account_profile_avatar(
        request: Request,
        avatar_kind: str = Form("photo"),
        csrf_token: str = Form(...),
        avatar: UploadFile = File(...),
    ):
        member = _current_member(request, required=True)
        _check_csrf(request, csrf_token)
        kind = str(avatar_kind or "photo").strip().lower()
        if kind not in {"photo", "sticker"}:
            return _member_redirect("Выберите тип аватарки", error=True)
        try:
            raw = await _read_upload(avatar, max_bytes=MAX_AVATAR_UPLOAD_BYTES)
            normalized, suffix, _mime = _prepare_avatar(raw, kind)
        except ValueError as exc:
            return _member_redirect(str(exc), error=True)

        filename = f"account-{int(member['id'])}-{secrets.token_hex(8)}{suffix}"
        target = _member_media_dir(settings) / filename
        target.write_bytes(normalized)
        web_path = f"/reward-media/profile-avatars/{filename}"
        old_path = None
        try:
            with transaction(settings.db_path) as conn:
                old = _avatar_record(conn, int(member["id"]))
                old_path = str(old["avatar_path"] or "") if old else None
                conn.execute(
                    """
                    INSERT INTO member_profile_media(account_id,client_id,avatar_path,avatar_kind)
                    VALUES (?,?,?,?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        client_id=excluded.client_id,
                        avatar_path=excluded.avatar_path,
                        avatar_kind=excluded.avatar_kind,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (int(member["id"]), int(member["client_id"]), web_path, kind),
                )
        except Exception:
            target.unlink(missing_ok=True)
            raise
        _delete_managed_file(settings, old_path, "profile-avatars")
        return _member_redirect("Аватарка обновлена")

    @app.post("/account/profile/avatar/remove")
    async def account_profile_avatar_remove(
        request: Request,
        csrf_token: str = Form(...),
    ):
        member = _current_member(request, required=True)
        _check_csrf(request, csrf_token)
        old_path = None
        with transaction(settings.db_path) as conn:
            row = _avatar_record(conn, int(member["id"]))
            old_path = str(row["avatar_path"] or "") if row else None
            conn.execute("DELETE FROM member_profile_media WHERE account_id=?", (int(member["id"]),))
        _delete_managed_file(settings, old_path, "profile-avatars")
        return _member_redirect("Аватарка удалена")

    @app.get("/account/avatar")
    async def account_avatar(request: Request):
        member = _current_member(request, required=True)
        with connect(settings.db_path) as conn:
            row = _avatar_record(conn, int(member["id"]))
        if not row or not row["avatar_path"]:
            raise HTTPException(status_code=404, detail="avatar_not_found")
        value = str(row["avatar_path"])
        prefix = "/reward-media/profile-avatars/"
        if not value.startswith(prefix):
            raise HTTPException(status_code=404, detail="avatar_not_found")
        target = _member_media_dir(settings) / Path(value).name
        if not target.is_file():
            raise HTTPException(status_code=404, detail="avatar_not_found")
        media_type = "image/png" if target.suffix.lower() == ".png" else "image/jpeg"
        return FileResponse(target, media_type=media_type, headers={"Cache-Control": "private, no-store"})

    @app.get("/account/chats", response_class=HTMLResponse)
    async def account_chats(request: Request, back: str = "/account"):
        member = _current_member(request, required=True)
        safe_back = _safe_back_path(back)
        return templates.TemplateResponse(
            request,
            "member_chats.html",
            {
                "request": request,
                "member": member,
                "back_url": safe_back,
                "csrf_token": _csrf_token(request),
            },
        )

    @app.get("/master/engagement-icons", response_class=HTMLResponse)
    async def master_engagement_icons(
        request: Request,
        ok: str = "",
        error: str = "",
    ):
        _require_master(request)
        with connect(settings.db_path) as conn:
            titles = conn.execute(
                "SELECT * FROM title_definitions ORDER BY title_type,priority,id"
            ).fetchall()
            achievements = conn.execute(
                "SELECT * FROM achievement_definitions ORDER BY position,id"
            ).fetchall()
        return templates.TemplateResponse(
            request,
            "engagement_icons.html",
            {
                "request": request,
                "titles": titles,
                "achievements": achievements,
                "ok": ok,
                "error": error,
                "csrf_token": _csrf_token(request),
                "admin_name": request.session.get("admin_name", "Администратор"),
                "admin_role": request.session.get("admin_role", ""),
                "asset_version": "product-shell-1",
            },
        )

    @app.post("/api/master/engagement-icons/{kind}/{definition_id:int}")
    async def master_engagement_icon_upload(
        request: Request,
        kind: str,
        definition_id: int,
        csrf_token: str = Form(...),
        icon: UploadFile = File(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        kind = str(kind).strip().lower()
        if kind not in {"title", "achievement"}:
            raise HTTPException(status_code=404, detail="definition_not_found")
        table = "title_definitions" if kind == "title" else "achievement_definitions"
        label = "звания" if kind == "title" else "достижения"
        try:
            raw = await _read_upload(icon, max_bytes=MAX_ICON_UPLOAD_BYTES)
            normalized = _prepare_engagement_icon(raw)
        except ValueError as exc:
            return _admin_icon_redirect(str(exc), error=True)
        with connect(settings.db_path) as conn:
            row = conn.execute(
                f"SELECT id,icon_path FROM {table} WHERE id=?",
                (definition_id,),
            ).fetchone()
        if not row:
            return _admin_icon_redirect("Элемент не найден", error=True)

        filename = f"{kind}-{definition_id}-{secrets.token_hex(8)}.png"
        target = _engagement_media_dir(settings) / filename
        target.write_bytes(normalized)
        web_path = f"/reward-media/engagement-icons/{filename}"
        old_path = str(row["icon_path"] or "")
        try:
            with transaction(settings.db_path) as conn:
                conn.execute(
                    f"UPDATE {table} SET icon_path=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (web_path, definition_id),
                )
        except sqlite3.OperationalError:
            with transaction(settings.db_path) as conn:
                conn.execute(
                    f"UPDATE {table} SET icon_path=? WHERE id=?",
                    (web_path, definition_id),
                )
        _delete_managed_file(settings, old_path, "engagement-icons")
        return _admin_icon_redirect(f"Иконка {label} обновлена")

    @app.post("/api/master/engagement-icons/{kind}/{definition_id:int}/remove")
    async def master_engagement_icon_remove(
        request: Request,
        kind: str,
        definition_id: int,
        csrf_token: str = Form(...),
    ):
        _require_master(request)
        _check_csrf(request, csrf_token)
        kind = str(kind).strip().lower()
        if kind not in {"title", "achievement"}:
            raise HTTPException(status_code=404, detail="definition_not_found")
        table = "title_definitions" if kind == "title" else "achievement_definitions"
        with transaction(settings.db_path) as conn:
            row = conn.execute(
                f"SELECT id,icon_path FROM {table} WHERE id=?",
                (definition_id,),
            ).fetchone()
            if not row:
                return _admin_icon_redirect("Элемент не найден", error=True)
            old_path = str(row["icon_path"] or "")
            try:
                conn.execute(
                    f"UPDATE {table} SET icon_path=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (definition_id,),
                )
            except sqlite3.OperationalError:
                conn.execute(
                    f"UPDATE {table} SET icon_path=NULL WHERE id=?",
                    (definition_id,),
                )
        _delete_managed_file(settings, old_path, "engagement-icons")
        return _admin_icon_redirect("Кастомная иконка удалена")

    return app


__all__ = [
    "install_product_shell",
    "_ensure_product_shell_schema",
    "_partner_tournament_rows",
    "_fetch_partner_tournament_icon_png",
    "_public_tournaments",
    "_upsert_partner_tournaments",
]
