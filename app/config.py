from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("HJC_HOST", "127.0.0.1")
    port: int = int(os.getenv("HJC_PORT", "8090"))
    public_base_url: str = os.getenv("HJC_PUBLIC_BASE_URL", "http://127.0.0.1:8090")
    master_login: str = os.getenv("HJC_MASTER_LOGIN", "master")
    admin_pin: str = os.getenv("HJC_ADMIN_PIN", "")
    admin_name: str = os.getenv("HJC_ADMIN_NAME", "Администратор")
    secret_key: str = os.getenv("HJC_SECRET_KEY", "")
    db_path: Path = Path(os.getenv("HJC_DB_PATH", str(BASE_DIR / "data" / "club_tools.sqlite3")))
    secure_cookie: bool = _bool_env("HJC_SECURE_COOKIE", True)
    session_hours: int = int(os.getenv("HJC_SESSION_HOURS", "12"))
    max_upload_mb: int = int(os.getenv("HJC_MAX_UPLOAD_MB", "15"))

    def validate(self) -> None:
        if not self.admin_pin:
            raise RuntimeError("HJC_ADMIN_PIN must be configured")
        if len(self.master_login.strip()) < 3:
            raise RuntimeError("HJC_MASTER_LOGIN must contain at least 3 characters")
        if len(self.secret_key) < 32:
            raise RuntimeError("HJC_SECRET_KEY must contain at least 32 characters")
