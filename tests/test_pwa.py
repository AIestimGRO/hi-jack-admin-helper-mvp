from __future__ import annotations

import io
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.config import BASE_DIR
from app.pwa import install_pwa


def _client() -> TestClient:
    return TestClient(install_pwa(FastAPI()))


def test_manifest_has_installable_chromium_icon_sizes() -> None:
    response = _client().get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")

    manifest = response.json()
    assert manifest["name"] == "Hi, Jack Club"
    assert manifest["short_name"] == "JACKSIDE"
    assert manifest["start_url"] == "/account"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["prefer_related_applications"] is False

    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert "192x192" in sizes
    assert "512x512" in sizes
    assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"])


def test_pwa_icons_are_exact_png_sizes() -> None:
    client = _client()
    for route, expected_size in (
        ("/pwa/icon-192.png", (192, 192)),
        ("/pwa/icon-512.png", (512, 512)),
        ("/pwa/icon-maskable-512.png", (512, 512)),
        ("/pwa/apple-touch-icon.png", (180, 180)),
    ):
        response = client.get(route)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/png")
        with Image.open(io.BytesIO(response.content)) as image:
            assert image.size == expected_size
            assert image.format == "PNG"


def test_favicon_and_service_worker_are_available() -> None:
    client = _client()

    favicon = client.get("/favicon.ico")
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/x-icon")
    assert favicon.content.startswith(b"\x00\x00\x01\x00")

    worker = client.get("/service-worker.js")
    assert worker.status_code == 200
    assert worker.headers["content-type"].startswith("application/javascript")
    assert worker.headers["service-worker-allowed"] == "/"
    assert "no-store" in worker.headers["cache-control"]
    assert "fetch" not in worker.text
    assert "clients.claim" in worker.text


def test_base_templates_reference_manifest_favicon_and_install_script() -> None:
    templates = Path(BASE_DIR) / "app" / "templates"
    for filename in ("base.html", "member_base.html"):
        source = (templates / filename).read_text(encoding="utf-8")
        assert 'rel="icon" href="/favicon.ico"' in source
        assert 'rel="manifest" href="/manifest.webmanifest"' in source
        assert 'rel="apple-touch-icon" href="/pwa/apple-touch-icon.png"' in source
        assert "/js/pwa-install.js" in source
