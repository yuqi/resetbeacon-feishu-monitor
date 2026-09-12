from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


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
    send_language: str | None = None
    translation_base_url: str = ""
    translation_api_key: str = field(default="", repr=False)
    translation_model: str = ""
    translation_reasoning_effort: str | None = None
    translation_timeout_seconds: float = 60.0
    translation_max_retries: int = 3

    @classmethod
    def from_env(cls) -> "Config":
        config = cls(
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
            send_language=_send_language(),
            translation_base_url=os.getenv("OPENAI_BASE_URL", "").strip().rstrip("/"),
            translation_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            translation_model=os.getenv("OPENAI_MODEL", "").strip(),
            translation_reasoning_effort=_reasoning_effort(),
            translation_timeout_seconds=_positive_float("TRANSLATION_TIMEOUT_SECONDS", 60.0),
            translation_max_retries=_positive_int("TRANSLATION_MAX_RETRIES", 3),
        )
        config.require_translation_config()
        return config

    @property
    def translation_enabled(self) -> bool:
        return self.send_language != "en" and bool(
            self.translation_base_url and self.translation_api_key and self.translation_model
        )

    def require_translation_config(self) -> None:
        if self.send_language == "en":
            return
        if self.send_language != "zh" and not any((
            self.translation_base_url, self.translation_api_key, self.translation_model,
        )):
            return
        for name, value in (
            ("OPENAI_BASE_URL", self.translation_base_url),
            ("OPENAI_API_KEY", self.translation_api_key),
            ("OPENAI_MODEL", self.translation_model),
        ):
            if not value:
                raise ConfigError(f"{name} is required when translation is enabled")
        try:
            parsed = urlparse(self.translation_base_url)
            parsed.port  # Validate a supplied port as well as bracketed hosts.
        except ValueError:
            raise ConfigError("OPENAI_BASE_URL is not a valid HTTP(S) base URL") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigError("OPENAI_BASE_URL must be an HTTP(S) base URL without credentials, query or fragment")
        if parsed.path.endswith("/chat/completions"):
            raise ConfigError("OPENAI_BASE_URL must omit /chat/completions")

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
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be positive")
    return value


def _reasoning_effort() -> str | None:
    value = os.getenv("OPENAI_REASONING_EFFORT", "").strip().lower()
    if not value:
        return None
    if value not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
        raise ConfigError("OPENAI_REASONING_EFFORT must be none, minimal, low, medium, high, xhigh or max")
    return value


def _send_language() -> str | None:
    value = os.getenv("SEND_LANGUAGE", "").strip().lower()
    if value not in {"", "en", "zh"}:
        raise ConfigError("SEND_LANGUAGE must be en, zh or empty")
    return value or None


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive")
    return value
