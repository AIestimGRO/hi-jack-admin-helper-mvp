from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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
    quiz_public_base_url: str = os.getenv("HJC_QUIZ_PUBLIC_BASE_URL", "http://127.0.0.1:8090")
    master_login: str = os.getenv("HJC_MASTER_LOGIN", "master")
    admin_pin: str = os.getenv("HJC_ADMIN_PIN", "")
    admin_name: str = os.getenv("HJC_ADMIN_NAME", "Администратор")
    secret_key: str = os.getenv("HJC_SECRET_KEY", "")
    db_path: Path = Path(os.getenv("HJC_DB_PATH", str(BASE_DIR / "data" / "club_tools.sqlite3")))
    secure_cookie: bool = _bool_env("HJC_SECURE_COOKIE", True)
    session_hours: int = int(os.getenv("HJC_SESSION_HOURS", "12"))
    max_upload_mb: int = int(os.getenv("HJC_MAX_UPLOAD_MB", "15"))
    timezone_name: str = os.getenv("HJC_TIMEZONE", "Europe/Moscow")
    telegram_client_id: str = os.getenv("HJC_TELEGRAM_CLIENT_ID", "").strip()
    telegram_client_secret: str = os.getenv("HJC_TELEGRAM_CLIENT_SECRET", "").strip()
    telegram_notifications_enabled: bool = _bool_env(
        "HJC_TELEGRAM_NOTIFICATIONS_ENABLED", False
    )
    telegram_transport_url: str = os.getenv("HJC_TELEGRAM_TRANSPORT_URL", "").strip()
    telegram_bridge_secret: str = os.getenv("HJC_TELEGRAM_BRIDGE_SECRET", "").strip()
    telegram_transport_timeout_seconds: float = float(
        os.getenv("HJC_TELEGRAM_TRANSPORT_TIMEOUT_SECONDS", "5")
    )
    smtp_host: str = os.getenv("HJC_SMTP_HOST", "").strip()
    smtp_port: int = int(os.getenv("HJC_SMTP_PORT", "587"))
    smtp_username: str = os.getenv("HJC_SMTP_USERNAME", "").strip()
    smtp_password: str = os.getenv("HJC_SMTP_PASSWORD", "")
    smtp_from: str = os.getenv("HJC_SMTP_FROM", "").strip()
    smtp_starttls: bool = _bool_env("HJC_SMTP_STARTTLS", True)
    email_code_minutes: int = int(os.getenv("HJC_EMAIL_CODE_MINUTES", "10"))
    quiz_detail_retention_days: int = int(os.getenv("HJC_QUIZ_DETAIL_RETENTION_DAYS", "7"))
    reward_retention_days: int = int(os.getenv("HJC_REWARD_RETENTION_DAYS", "14"))
    action_log_retention_days: int = int(os.getenv("HJC_ACTION_LOG_RETENTION_DAYS", "31"))
    member_portal_enabled: bool = _bool_env("HJC_MEMBER_PORTAL_ENABLED", False)
    member_session_days: int = int(os.getenv("HJC_MEMBER_SESSION_DAYS", "30"))
    vault_activation_minutes: int = int(
        os.getenv("HJC_VAULT_ACTIVATION_MINUTES", "10")
    )

    def validate(self) -> None:
        if not self.admin_pin:
            raise RuntimeError("HJC_ADMIN_PIN must be configured")
        if len(self.master_login.strip()) < 3:
            raise RuntimeError("HJC_MASTER_LOGIN must contain at least 3 characters")
        if len(self.secret_key) < 32:
            raise RuntimeError("HJC_SECRET_KEY must contain at least 32 characters")
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise RuntimeError("HJC_TIMEZONE must contain a valid IANA timezone") from exc
        if not 1 <= self.email_code_minutes <= 60:
            raise RuntimeError("HJC_EMAIL_CODE_MINUTES must be between 1 and 60")
        if self.quiz_detail_retention_days < 1 or self.reward_retention_days < 1:
            raise RuntimeError("Quiz retention settings must be positive")
        if not 1 <= self.member_session_days <= 365:
            raise RuntimeError("HJC_MEMBER_SESSION_DAYS must be between 1 and 365")
        if not 1 <= self.vault_activation_minutes <= 120:
            raise RuntimeError(
                "HJC_VAULT_ACTIVATION_MINUTES must be between 1 and 120"
            )
        if not 0.5 <= self.telegram_transport_timeout_seconds <= 30:
            raise RuntimeError(
                "HJC_TELEGRAM_TRANSPORT_TIMEOUT_SECONDS must be between 0.5 and 30"
            )
