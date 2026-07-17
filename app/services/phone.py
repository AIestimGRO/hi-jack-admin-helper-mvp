from __future__ import annotations

import re
from typing import Any


def normalize_phone(value: Any) -> str | None:
    """Return the Russian 10-digit local phone or None."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def full_phone(phone_local: str | None) -> str | None:
    return f"+7{phone_local}" if phone_local else None


def display_phone(phone_local: str | None, raw: str | None = None) -> str:
    if phone_local:
        return f"+7 {phone_local[:3]} {phone_local[3:6]}-{phone_local[6:8]}-{phone_local[8:]}"
    return raw or "—"

