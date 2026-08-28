from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from PIL import Image

from app.db import connect, init_db
from app.product_shell import (
    _ensure_product_shell_schema,
    _fetch_partner_tournament_icon_png,
    _partner_tournament_rows,
    _prepare_avatar,
    _prepare_engagement_icon,
    _public_tournaments,
    _upsert_partner_tournaments,
    install_product_shell,
)


def _image_bytes(
    *,
    mode: str = "RGBA",
    size: tuple[int, int] = (180, 120),
    image_format: str = "PNG",
) -> bytes:
    fill = (180, 90, 40, 180) if mode == "RGBA" else (180, 90, 40)
    image = Image.new(mode, size, fill)
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def test_product_shell_schema_is_additive_and_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "product-shell.sqlite3"
    init_db(db_path)

    _ensure_product_shell_schema(db_path)
    _ensure_product_shell_schema(db_path)

    with connect(db_path) as conn:
        profile_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='member_profile_media'"
        ).fetchone()
        tournament_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='club_tournaments'"
        ).fetchone()
        title_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(title_definitions)")
        }
        achievement_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(achievement_definitions)")
        }
        tournament_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(club_tournaments)")
        }

    assert profile_table is not None
    assert tournament_table is not None
    assert "icon_path" in title_columns
    assert "icon_path" in achievement_columns
    assert "max_slots" in tournament_columns
    assert "registration_open" in tournament_columns
    assert "source_kind" in tournament_columns
    assert "external_id" in tournament_columns
    assert "external_url" in tournament_columns
    assert "external_icon_url" in tournament_columns
    assert "source_synced_at" in tournament_columns


def test_sticker_avatar_is_normalized_to_transparent_512_png() -> None:
    normalized, suffix, mime = _prepare_avatar(
        _image_bytes(size=(220, 90)),
        "sticker",
    )
    image = Image.open(io.BytesIO(normalized))

    assert suffix == ".png"
    assert mime == "image/png"
    assert image.size == (512, 512)
    assert image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] == 0


def test_photo_avatar_is_center_cropped_to_512_jpeg() -> None:
    normalized, suffix, mime = _prepare_avatar(
        _image_bytes(mode="RGB", size=(700, 350), image_format="JPEG"),
        "photo",
    )
    image = Image.open(io.BytesIO(normalized))

    assert suffix == ".jpg"
    assert mime == "image/jpeg"
    assert image.size == (512, 512)
    assert image.mode == "RGB"


def test_engagement_icon_keeps_transparent_safe_canvas() -> None:
    normalized = _prepare_engagement_icon(_image_bytes(size=(300, 120)))
    image = Image.open(io.BytesIO(normalized))

    assert image.size == (512, 512)
    assert image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] == 0


def test_product_shell_registers_expected_routes(tmp_path: Path) -> None:
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        db_path=tmp_path / "routes.sqlite3",
        secret_key="x" * 64,
    )
    install_product_shell(app)

    paths = {route.path for route in app.routes}

    assert "/api/account/product-shell" in paths
    assert "/api/account/tournaments/{external_id}/icon" in paths
    assert "/account/profile/update" in paths
    assert "/account/profile/avatar" in paths
    assert "/account/chats" in paths
    assert "/master/engagement-icons" in paths
    assert "/api/master/engagement-icons/{kind}/{definition_id:int}" in paths

def _partner_settings(db_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        db_path=db_path,
        timezone_name="Europe/Moscow",
        partner_tournament_launch_url_template=(
            "https://t.me/HJCapp_bot/app?startapp=tournament_{id}"
        ),
    )


def test_partner_tournament_payload_maps_to_schedule_rows(tmp_path: Path) -> None:
    settings = _partner_settings(tmp_path / "partner-map.sqlite3")
    rows = _partner_tournament_rows(
        {
            "results": [
                {
                    "id": 77,
                    "title": "Friday Deepstack",
                    "location": "Hi, Jack Club",
                    "started_at": "2026-09-04T19:30:00+03:00",
                    "max_participants": 42,
                    "status": "IN_QUEUE",
                    "features": ["Freezeout", "20 min levels"],
                    "icon": "/media/tournaments/icons/freezeout.png",
                },
                {
                    "id": 78,
                    "title": "Already active",
                    "started_at": "2026-09-04T18:00:00+03:00",
                    "status": "ACTIVE",
                },
            ]
        },
        settings,
    )

    assert len(rows) == 1
    assert rows[0]["external_id"] == "77"
    assert rows[0]["title"] == "Friday Deepstack"
    assert rows[0]["starts_at"] == "2026-09-04T16:30:00+00:00"
    assert rows[0]["max_slots"] == 42
    assert rows[0]["external_url"].endswith("tournament_77")
    assert rows[0]["external_icon_url"] == (
        "https://hi-jack-club.matthew-0203.ru/media/tournaments/icons/freezeout.png"
    )
    assert rows[0]["description"] == "Hi, Jack Club"
    assert rows[0]["format_text"] == "Freezeout · 20 min levels"


def test_partner_tournament_sync_is_idempotent_and_hides_stale_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "partner-sync.sqlite3"
    init_db(db_path)
    _ensure_product_shell_schema(db_path)

    first = [
        {
            "external_id": "10",
            "title": "First",
            "starts_at": "2030-01-01T17:00:00+00:00",
            "description": "Club",
            "format_text": "Club",
            "max_slots": 60,
            "external_url": "https://t.me/HJCapp_bot/app?startapp=tournament_10",
            "external_icon_url": "https://cdn.example.test/tournaments/10.png",
        },
        {
            "external_id": "20",
            "title": "Second",
            "starts_at": "2030-01-02T17:00:00+00:00",
            "description": "Club",
            "format_text": "Club",
            "max_slots": 40,
            "external_url": "https://t.me/HJCapp_bot/app?startapp=tournament_20",
            "external_icon_url": "https://cdn.example.test/tournaments/20.png",
        },
    ]
    assert _upsert_partner_tournaments(db_path, first) == 2

    with connect(db_path) as conn:
        first_id = int(
            conn.execute(
                """
                SELECT id FROM club_tournaments
                WHERE source_kind='miniapp' AND external_id='10'
                """
            ).fetchone()[0]
        )

    second = [
        {
            **first[0],
            "title": "First updated",
            "max_slots": 72,
            "external_icon_url": "https://cdn.example.test/tournaments/10-v2.png",
        }
    ]
    assert _upsert_partner_tournaments(db_path, second) == 1

    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id,external_id,title,max_slots,external_icon_url,is_published,status
            FROM club_tournaments
            WHERE source_kind='miniapp'
            ORDER BY external_id
            """
        ).fetchall()

    assert len(rows) == 2
    assert int(rows[0]["id"]) == first_id
    assert rows[0]["title"] == "First updated"
    assert int(rows[0]["max_slots"]) == 72
    assert rows[0]["external_icon_url"].endswith("10-v2.png")
    assert int(rows[0]["is_published"]) == 1
    assert rows[0]["status"] == "scheduled"
    assert int(rows[1]["is_published"]) == 0
    assert rows[1]["status"] == "registration_closed"

def test_partner_tournament_icon_accepts_object_url_and_rejects_unsafe_scheme(
    tmp_path: Path,
) -> None:
    settings = _partner_settings(tmp_path / "partner-icons.sqlite3")
    rows = _partner_tournament_rows(
        {
            "results": [
                {
                    "id": 91,
                    "title": "Object icon",
                    "started_at": "2030-01-01T19:00:00+03:00",
                    "status": "IN_QUEUE",
                    "icon": {"url": "https://cdn.example.test/tournaments/91.webp"},
                },
                {
                    "id": 92,
                    "title": "Unsafe icon",
                    "started_at": "2030-01-02T19:00:00+03:00",
                    "status": "IN_QUEUE",
                    "icon_url": "javascript:alert(1)",
                },
            ]
        },
        settings,
    )

    assert rows[0]["external_icon_url"] == (
        "https://cdn.example.test/tournaments/91.webp"
    )
    assert rows[1]["external_icon_url"] == ""

def test_neon_tournament_fallback_uses_jackside_brand_asset() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    asset = repo_root / "app/static/img/brand/jackside-logo.webp"
    javascript = (repo_root / "app/static/js/product-shell.js").read_text(
        encoding="utf-8"
    )

    assert asset.is_file()
    assert '/static/img/brand/jackside-logo.webp' in javascript

def test_partner_icon_is_delivered_through_same_origin_account_route(
    tmp_path: Path,
) -> None:
    settings = _partner_settings(tmp_path / "partner-proxy.sqlite3")
    settings.partner_tournaments_url = (
        "https://hi-jack-club.matthew-0203.ru/"
        "api/tournaments/partner/tournaments/?status=IN_QUEUE"
    )
    db_path = settings.db_path
    init_db(db_path)
    _ensure_product_shell_schema(db_path)
    _upsert_partner_tournaments(
        db_path,
        [
            {
                "external_id": "82",
                "title": "JACK CODE: WHITE",
                "starts_at": "2030-01-03T17:00:00+00:00",
                "description": "Club",
                "format_text": "",
                "max_slots": 60,
                "external_url": "https://t.me/HJCapp_bot/app?startapp=tournament_82",
                "external_icon_url": (
                    "https://hi-jack-club.matthew-0203.ru/storage/"
                    "tournaments/icons/white.png"
                ),
            }
        ],
    )

    with connect(db_path) as conn:
        rows = _public_tournaments(conn, settings=settings)

    assert len(rows) == 1
    assert rows[0]["external_icon_url"].startswith(
        "/api/account/tournaments/82/icon?v="
    )
    assert "hi-jack-club.matthew-0203.ru" not in rows[0]["external_icon_url"]


def test_partner_icon_proxy_fetches_with_partner_key_and_normalizes_png(
    tmp_path: Path,
) -> None:
    settings = _partner_settings(tmp_path / "partner-fetch.sqlite3")
    settings.partner_tournaments_url = (
        "https://hi-jack-club.matthew-0203.ru/"
        "api/tournaments/partner/tournaments/?status=IN_QUEUE"
    )
    settings.partner_api_key = "test-partner-key"
    settings.partner_tournament_timeout_seconds = 4
    source = (
        "https://hi-jack-club.matthew-0203.ru/storage/"
        "tournaments/icons/white.png"
    )
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _limit: int) -> bytes:
            return _image_bytes(size=(320, 180))

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["key"] = request.get_header("X-partner-api-key")
        captured["accept"] = request.get_header("Accept")
        captured["timeout"] = timeout
        return FakeResponse()

    normalized = _fetch_partner_tournament_icon_png(
        settings,
        source,
        urlopen_func=fake_urlopen,
    )
    image = Image.open(io.BytesIO(normalized))

    assert captured["url"] == source
    assert captured["key"] == "test-partner-key"
    assert captured["accept"] == "image/*"
    assert float(captured["timeout"]) == 4.0
    assert image.size == (512, 512)
    assert image.mode == "RGBA"


def test_partner_tournament_location_is_not_duplicated_in_meta(tmp_path: Path) -> None:
    settings = _partner_settings(tmp_path / "partner-location.sqlite3")
    rows = _partner_tournament_rows(
        [
            {
                "id": 101,
                "title": "Location once",
                "location": "Люсиновская 53 к2",
                "started_at": "2030-01-03T19:00:00+03:00",
                "status": "IN_QUEUE",
                "features": [],
            }
        ],
        settings,
    )

    assert len(rows) == 1
    assert rows[0]["description"] == "Люсиновская 53 к2"
    assert rows[0]["format_text"] == ""

