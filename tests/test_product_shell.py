from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from PIL import Image

from app.db import connect, init_db
from app.product_shell import (
    _ensure_product_shell_schema,
    _prepare_avatar,
    _prepare_engagement_icon,
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
    assert "/account/profile/update" in paths
    assert "/account/profile/avatar" in paths
    assert "/account/chats" in paths
    assert "/master/engagement-icons" in paths
    assert "/api/master/engagement-icons/{kind}/{definition_id}" in paths
