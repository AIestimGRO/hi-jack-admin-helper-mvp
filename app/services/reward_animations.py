from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path


MAX_VECTOR_BYTES = 1_000_000
MAX_WEBP_BYTES = 1_500_000
MAX_GIF_BYTES = 2_000_000
MAX_PNG_BYTES = 1_000_000


@dataclass(frozen=True)
class RewardAnimation:
    key: str
    title: str
    collection: str
    path: str


REWARD_ANIMATIONS = (
    RewardAnimation("casino_chips", "Poker Chips", "Casino", "/static/animations/rewards/casino_chips.json"),
    RewardAnimation("royal_cards", "Royal Cards", "Casino", "/static/animations/rewards/royal_cards.json"),
    RewardAnimation("lucky_crown", "Lucky Crown", "Casino", "/static/animations/rewards/lucky_crown.json"),
    RewardAnimation("champion_cup", "Champion Cup", "Achievement", "/static/animations/rewards/champion_cup.json"),
    RewardAnimation("winner_badge", "Winner Badge", "Achievement", "/static/animations/rewards/winner_badge.json"),
    RewardAnimation("premium_gem", "Premium Gem", "3D Achievement", "/static/animations/rewards/premium_gem.json"),
    RewardAnimation("laurel_star", "Laurel Star", "3D Achievement", "/static/animations/rewards/laurel_star.json"),
    RewardAnimation("jackcoin_stack", "JACKCOIN Stack", "Money & Coins", "/static/animations/rewards/jackcoin_stack.json"),
    RewardAnimation("coffee_cup", "Coffee Cup", "Food & Drinks", "/static/animations/rewards/coffee_cup.json"),
    RewardAnimation("club_cocktail", "Club Cocktail", "Food & Drinks", "/static/animations/rewards/club_cocktail.json"),
)
REWARD_ANIMATION_BY_KEY = {item.key: item for item in REWARD_ANIMATIONS}


def animation_url(*, animation_key: str | None, animation_path: str | None) -> str | None:
    key = str(animation_key or "").strip()
    if key:
        item = REWARD_ANIMATION_BY_KEY.get(key)
        return item.path if item else None
    path = str(animation_path or "").strip()
    return path if path.startswith("/reward-media/") else None


def validate_animation_key(value: str | None) -> str | None:
    key = str(value or "").strip()
    if not key:
        return None
    if key not in REWARD_ANIMATION_BY_KEY:
        raise ValueError("invalid_animation_key")
    return key


def _validate_lottie_json(content: bytes) -> None:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_animation_file") from exc
    required = {"v", "fr", "ip", "op", "w", "h", "layers"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise ValueError("invalid_animation_file")
    if not isinstance(payload["layers"], list) or not payload["layers"]:
        raise ValueError("invalid_animation_file")
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    if any(marker in serialized for marker in ("http://", "https://", "javascript:")):
        raise ValueError("invalid_animation_file")
    width = int(payload.get("w") or 0)
    height = int(payload.get("h") or 0)
    if not 64 <= width <= 2_048 or not 64 <= height <= 2_048:
        raise ValueError("invalid_animation_dimensions")


def _validate_dotlottie(content: bytes) -> None:
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            files = archive.infolist()
            if len(files) > 100:
                raise ValueError("invalid_animation_file")
            if sum(item.file_size for item in files) > 5_000_000:
                raise ValueError("invalid_animation_file")
            names = {item.filename for item in files}
            if any(
                name.startswith(("/", "\\")) or ".." in Path(name).parts
                for name in names
            ):
                raise ValueError("invalid_animation_file")
            animation_names = [
                name
                for name in names
                if name.startswith("animations/") and name.endswith(".json")
            ]
            if "manifest.json" not in names or not animation_names:
                raise ValueError("invalid_animation_file")
            try:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid_animation_file") from exc
            if not isinstance(manifest, dict):
                raise ValueError("invalid_animation_file")
            for name in animation_names:
                _validate_lottie_json(archive.read(name))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("invalid_animation_file") from exc


def validate_animation_upload(filename: str, content: bytes) -> tuple[str, str]:
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix == ".json":
        if not content or len(content) > MAX_VECTOR_BYTES:
            raise ValueError("animation_file_too_large")
        _validate_lottie_json(content)
        return suffix, "application/json"
    if suffix == ".lottie":
        if not content or len(content) > MAX_VECTOR_BYTES:
            raise ValueError("animation_file_too_large")
        _validate_dotlottie(content)
        return suffix, "application/zip"
    if suffix == ".webp":
        if not content or len(content) > MAX_WEBP_BYTES or not content.startswith(b"RIFF") or content[8:12] != b"WEBP":
            raise ValueError("invalid_animation_file")
        return suffix, "image/webp"
    if suffix == ".gif":
        if not content or len(content) > MAX_GIF_BYTES or content[:6] not in {b"GIF87a", b"GIF89a"}:
            raise ValueError("invalid_animation_file")
        return suffix, "image/gif"
    if suffix == ".png":
        if not content or len(content) > MAX_PNG_BYTES or not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("invalid_animation_file")
        return suffix, "image/png"
    raise ValueError("invalid_animation_format")


def save_animation_upload(directory: Path, filename: str, content: bytes) -> tuple[str, str]:
    suffix, mime = validate_animation_upload(filename, content)
    import secrets

    directory.mkdir(parents=True, exist_ok=True)
    stored_name = f"reward-{secrets.token_hex(16)}{suffix}"
    (directory / stored_name).write_bytes(content)
    return f"/reward-media/{stored_name}", mime
