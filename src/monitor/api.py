from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx


class APIError(RuntimeError):
    """A Reset Beacon response could not be fetched or validated."""


class ResetBeaconClient:
    def __init__(
        self,
        *,
        history_url: str,
        alerts_url: str,
        health_url: str,
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.history_url = history_url
        self.alerts_url = alerts_url
        self.health_url = health_url
        self.max_retries = max_retries
        self.sleep = sleep
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10.0)),
            follow_redirects=True,
            headers={"User-Agent": "resetbeacon-feishu-monitor/0.1"},
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "ResetBeaconClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def fetch_history(self) -> list[dict[str, Any]]:
        return self._fetch_items(self.history_url, "history")

    def fetch_alerts(self) -> list[dict[str, Any]]:
        return self._fetch_items(self.alerts_url, "alerts")

    def fetch_health(self) -> dict[str, Any]:
        payload = self._get_json(self.health_url)
        if not isinstance(payload.get("status"), str):
            raise APIError("health response is missing status")
        return payload

    def _fetch_items(self, url: str, name: str) -> list[dict[str, Any]]:
        payload = self._get_json(url)
        items = payload.get("items")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise APIError(f"{name} response is missing an items array")
        return items

    def _get_json(self, url: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self.client.get(url)
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"temporary HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise APIError("response JSON must be an object")
                return payload
            except (httpx.HTTPError, ValueError, APIError) as exc:
                last_error = exc
                permanent_http_error = (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code < 500
                    and exc.response.status_code != 429
                )
                if permanent_http_error or attempt + 1 >= self.max_retries:
                    break
                self.sleep(0.5 * (2**attempt))
        raise APIError(f"request failed after {self.max_retries} attempt(s): {last_error}")

