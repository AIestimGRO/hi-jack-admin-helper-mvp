from __future__ import annotations

import io
import json
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response
from PIL import Image

from app.config import BASE_DIR

_APP_NAME = "Hi, Jack Club"
_SHORT_NAME = "JACKSIDE"
_THEME_COLOR = "#07110f"
_MARK_PATH = Path(BASE_DIR) / "app" / "static" / "img" / "brand" / "hi-jack-mark.webp"


def _resampling_filter():
    return getattr(Image, "Resampling", Image).LANCZOS


@lru_cache(maxsize=8)
def _png_icon(size: int, *, maskable: bool = False) -> bytes:
    if size not in {180, 192, 512}:
        raise ValueError("unsupported_icon_size")

    with Image.open(_MARK_PATH) as source:
        mark = source.convert("RGBA")

    canvas = Image.new(
        "RGBA",
        (size, size),
        (7, 17, 15, 255) if maskable else (0, 0, 0, 0),
    )
    max_mark_size = int(size * (0.70 if maskable else 0.86))
    mark.thumbnail((max_mark_size, max_mark_size), _resampling_filter())
    left = (size - mark.width) // 2
    top = (size - mark.height) // 2
    canvas.alpha_composite(mark, (left, top))

    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


@lru_cache(maxsize=1)
def _favicon_ico() -> bytes:
    with Image.open(_MARK_PATH) as source:
        mark = source.convert("RGBA")
    mark.thumbnail((64, 64), _resampling_filter())
    canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    canvas.alpha_composite(mark, ((64 - mark.width) // 2, (64 - mark.height) // 2))
    output = io.BytesIO()
    canvas.save(
        output,
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64)],
    )
    return output.getvalue()


def _manifest_payload() -> dict[str, object]:
    return {
        "id": "/account",
        "name": _APP_NAME,
        "short_name": _SHORT_NAME,
        "description": "Личный кабинет и JACKSIDE клуба Hi, Jack!",
        "lang": "ru",
        "start_url": "/account",
        "scope": "/",
        "display": "standalone",
        "background_color": _THEME_COLOR,
        "theme_color": _THEME_COLOR,
        "prefer_related_applications": False,
        "icons": [
            {
                "src": "/pwa/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "/pwa/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "/pwa/icon-maskable-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }


def install_pwa(app: FastAPI) -> FastAPI:
    if getattr(app.state, "pwa_installed", False):
        return app
    app.state.pwa_installed = True

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def web_app_manifest():
        return Response(
            json.dumps(_manifest_payload(), ensure_ascii=False, separators=(",", ":")),
            media_type="application/manifest+json",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(
            _favicon_ico(),
            media_type="image/x-icon",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/pwa/icon-192.png", include_in_schema=False)
    async def pwa_icon_192():
        return Response(
            _png_icon(192),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/pwa/icon-512.png", include_in_schema=False)
    async def pwa_icon_512():
        return Response(
            _png_icon(512),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/pwa/icon-maskable-512.png", include_in_schema=False)
    async def pwa_icon_maskable_512():
        return Response(
            _png_icon(512, maskable=True),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/pwa/apple-touch-icon.png", include_in_schema=False)
    async def apple_touch_icon():
        return Response(
            _png_icon(180),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/service-worker.js", include_in_schema=False)
    async def service_worker():
        script = """'use strict';
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});
"""
        return Response(
            script,
            media_type="application/javascript",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Service-Worker-Allowed": "/",
            },
        )

    return app


__all__ = ["install_pwa"]
