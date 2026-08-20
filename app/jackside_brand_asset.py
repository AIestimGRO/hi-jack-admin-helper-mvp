from __future__ import annotations

from pathlib import Path


JACKSIDE_LOGO_WEBP = (
    Path(__file__).resolve().parent / "static" / "img" / "brand" / "jackside-logo.webp"
).read_bytes()
