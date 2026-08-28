from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections.abc import Callable
from typing import Any

import httpx


class FeishuDeliveryError(RuntimeError):
    """Feishu did not confirm that a message was accepted."""


def generate_sign(secret: str, timestamp: int) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


class FeishuSender:
    def __init__(
        self,
        webhook_url: str,
        *,
        signing_secret: str | None = None,
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self.webhook_url = webhook_url
        self.signing_secret = signing_secret
        self.max_retries = max_retries
        self.sleep = sleep
        self.clock = clock
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "FeishuSender":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def send(self, message: dict[str, Any]) -> None:
        payload = dict(message)
        if self.signing_secret:
            timestamp = int(self.clock())
            payload["timestamp"] = timestamp
            payload["sign"] = generate_sign(self.signing_secret, timestamp)

        last_error = "unknown error"
        for attempt in range(self.max_retries):
            try:
                response = self.client.post(self.webhook_url, json=payload)
                if response.status_code == 429 or response.status_code >= 500:
                    raise FeishuDeliveryError(f"temporary HTTP {response.status_code}")
                response.raise_for_status()
                body = response.json()
                self._validate_success(body)
                return
            except (httpx.HTTPError, ValueError, FeishuDeliveryError) as exc:
                last_error = _safe_error(exc)
                if attempt + 1 >= self.max_retries:
                    break
                self.sleep(0.5 * (2**attempt))
        raise FeishuDeliveryError(f"delivery failed after {self.max_retries} attempt(s): {last_error}")

    @staticmethod
    def _validate_success(body: Any) -> None:
        if not isinstance(body, dict):
            raise FeishuDeliveryError("response JSON must be an object")
        code = body.get("code")
        status_code = body.get("StatusCode")
        if code is None and status_code is None:
            raise FeishuDeliveryError("response has no business success code")
        if code not in (None, 0):
            raise FeishuDeliveryError(f"Feishu business code {code}: {body.get('msg', 'unknown error')}")
        if status_code not in (None, 0):
            raise FeishuDeliveryError(
                f"Feishu status code {status_code}: {body.get('StatusMessage', 'unknown error')}"
            )


def _safe_error(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    if isinstance(error, httpx.HTTPError):
        return f"network error ({type(error).__name__})"
    return str(error)
