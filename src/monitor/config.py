from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    history_url: str
    alerts_url: str
    health_url: str
    webhook_url: str | None
    signing_secret: str | None
    db_path: Path
    timeout_seconds: float = 15.0
    max_retries: int = 3
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            history_url=os.getenv(
                "RESET_BEACON_HISTORY_URL",
                "https://resetbeacon.com/api/history",
            ),
            alerts_url=os.getenv(
                "RESET_BEACON_ALERTS_URL",
                "https://resetbeacon.com/api/alerts",
            ),
            health_url=os.getenv(
                "RESET_BEACON_HEALTH_URL",
                "https://resetbeacon.com/api/v1/health",
            ),
            webhook_url=os.getenv("FEISHU_WEBHOOK_URL") or None,
            signing_secret=os.getenv("FEISHU_SIGNING_SECRET") or None,
            db_path=Path(os.getenv("STATE_DB_PATH", "./data/monitor.sqlite3")),
            timeout_seconds=_positive_float("HTTP_TIMEOUT_SECONDS", 15.0),
            max_retries=_positive_int("HTTP_MAX_RETRIES", 3),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )

    def require_delivery_config(self) -> None:
        if not self.webhook_url:
            raise ConfigError("FEISHU_WEBHOOK_URL is required for delivery")
        if not self.webhook_url.startswith("https://"):
            raise ConfigError("FEISHU_WEBHOOK_URL must use HTTPS")


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        value = float(raw) if raw is not None else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive")
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive")
    return value

