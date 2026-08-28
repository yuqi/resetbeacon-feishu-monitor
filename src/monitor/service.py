from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .db import StateDB
from .render import render_message


class API(Protocol):
    def fetch_history(self) -> list[dict[str, Any]]: ...
    def fetch_alerts(self) -> list[dict[str, Any]]: ...
    def fetch_health(self) -> dict[str, Any]: ...


class Sender(Protocol):
    def send(self, message: dict[str, Any]) -> None: ...


@dataclass
class RunSummary:
    dry_run: bool
    fetched: dict[str, int] = field(default_factory=dict)
    baselined: dict[str, int] = field(default_factory=dict)
    queued: int = 0
    suppressed: int = 0
    delivered: int = 0
    pending: int = 0
    errors: list[str] = field(default_factory=list)
    previews: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MonitorService:
    def __init__(self, db: StateDB, api: API, sender: Sender | None):
        self.db = db
        self.api = api
        self.sender = sender

    def run_once(self, *, dry_run: bool = False) -> RunSummary:
        summary = RunSummary(dry_run=dry_run)
        history = self._fetch("history", self.api.fetch_history, summary, dry_run)
        alerts = self._fetch("alerts", self.api.fetch_alerts, summary, dry_run)
        health = self._fetch("health", self.api.fetch_health, summary, dry_run)

        history_ids = {
            str(item.get("id")) for item in (history or []) if item.get("id") is not None
        }
        if history is not None:
            self._ingest_history(history, summary, dry_run)
        if alerts is not None:
            self._ingest_alerts(alerts, history_ids, summary, dry_run)
        if health is not None:
            self._ingest_health(health, summary, dry_run)

        if not dry_run:
            self._queue_startup_if_ready(history, alerts)
            self._deliver_pending(summary)
        else:
            self._preview_startup_if_ready(history, alerts, summary)
            self._preview_pending(summary)

        summary.pending = len(self.db.pending_deliveries())
        return summary

    def _fetch(
        self,
        source: str,
        operation: Any,
        summary: RunSummary,
        dry_run: bool,
    ) -> Any | None:
        try:
            result = operation()
            summary.fetched[source] = len(result) if isinstance(result, list) else 1
            if not dry_run:
                recovered = self.db.record_source_success(source)
                if recovered is not None:
                    payload = {
                        "status": "recovered",
                        "source": source,
                        "reasons": [f"outage #{recovered} recovered"],
                    }
                    self.db.observe(
                        "health",
                        f"source-recovery:{source}:{recovered}",
                        payload,
                        notify=True,
                    )
            return result
        except Exception as exc:  # source isolation is intentional
            error = f"{source}: {exc}"
            summary.errors.append(error)
            if not dry_run:
                failures, outage, should_alert = self.db.record_source_failure(source, str(exc))
                if should_alert:
                    payload = {
                        "status": "unavailable",
                        "source": source,
                        "reasons": ["three consecutive requests failed"],
                        "consecutiveFailures": failures,
                    }
                    self.db.observe(
                        "health",
                        f"source-outage:{source}:{outage}",
                        payload,
                        notify=True,
                    )
            return None

    def _ingest_history(
        self,
        items: list[dict[str, Any]],
        summary: RunSummary,
        dry_run: bool,
    ) -> None:
        initialized = self.db.is_initialized("history")
        baselined = 0
        for item in sorted(items, key=lambda value: str(value.get("announcedAt") or "")):
            item_id = str(item.get("id") or "")
            if not item_id:
                summary.errors.append("history: item without id")
                continue
            payload = history_payload(item)
            state = self.db.detect("history", item_id, payload)
            previously_delivered_by_alert = state == "new" and self.db.was_post_delivered(item_id)
            if dry_run:
                if not initialized:
                    baselined += state != "same"
                elif state != "same" and not previously_delivered_by_alert:
                    summary.previews.append(render_message("history", payload))
                elif state != "same" and previously_delivered_by_alert:
                    summary.suppressed += 1
                continue
            notify = initialized and not previously_delivered_by_alert
            result = self.db.observe("history", item_id, payload, notify=notify)
            if result != "same":
                if notify:
                    summary.queued += 1
                elif previously_delivered_by_alert and initialized:
                    summary.suppressed += 1
                else:
                    baselined += 1
        if not dry_run and not initialized:
            self.db.mark_initialized("history")
        if baselined:
            summary.baselined["history"] = baselined

    def _ingest_alerts(
        self,
        items: list[dict[str, Any]],
        current_history_ids: set[str],
        summary: RunSummary,
        dry_run: bool,
    ) -> None:
        initialized = self.db.is_initialized("alerts")
        baselined = 0
        for item in sorted(items, key=lambda value: str(value.get("publishedAt") or "")):
            item_id = str(item.get("id") or "")
            if not item_id:
                summary.errors.append("alerts: item without id")
                continue
            payload = alert_payload(item)
            state = self.db.detect("alerts", item_id, payload)
            post_id = str(item.get("postId")) if item.get("postId") is not None else None
            duplicate = bool(
                post_id
                and (
                    post_id in current_history_ids
                    or self.db.has_item("history", post_id)
                    or self.db.was_post_delivered(post_id)
                )
            )
            if dry_run:
                if not initialized:
                    baselined += state != "same"
                elif state != "same" and not duplicate:
                    summary.previews.append(render_message("alerts", payload))
                elif state != "same" and duplicate:
                    summary.suppressed += 1
                continue
            notify = initialized and not duplicate
            result = self.db.observe("alerts", item_id, payload, notify=notify)
            if result != "same":
                if notify:
                    summary.queued += 1
                elif duplicate and initialized:
                    summary.suppressed += 1
                else:
                    baselined += 1
        if not dry_run and not initialized:
            self.db.mark_initialized("alerts")
        if baselined:
            summary.baselined["alerts"] = baselined

    def _ingest_health(
        self,
        health: dict[str, Any],
        summary: RunSummary,
        dry_run: bool,
    ) -> None:
        payload = {
            "status": health.get("status"),
            "reasons": health.get("reasons") or [],
        }
        initialized = self.db.is_initialized("service-health")
        state = self.db.detect("health", "service-health", payload)
        if dry_run:
            if not initialized:
                summary.baselined["health"] = 1
            elif state != "same":
                summary.previews.append(render_message("health", payload))
            return
        result = self.db.observe(
            "health",
            "service-health",
            payload,
            notify=initialized,
        )
        if initialized and result != "same":
            summary.queued += 1
        if not initialized:
            self.db.mark_initialized("service-health")
            summary.baselined["health"] = 1

    def _queue_startup_if_ready(
        self,
        history: list[dict[str, Any]] | None,
        alerts: list[dict[str, Any]] | None,
    ) -> None:
        if not (
            self.db.is_initialized("history")
            and self.db.is_initialized("alerts")
            and not self.db.has_item("system", "startup:v1")
        ):
            return
        payload = {
            "title": "Codex 重置监控已启动",
            "template": "blue",
            "lines": [
                f"History 基线：{len(history or [])} 条",
                f"Alerts 基线：{len(alerts or [])} 条",
                "后续只发送新事件、实质更新和健康状态转换。",
            ],
        }
        self.db.observe("system", "startup:v1", payload, notify=True)

    def _preview_startup_if_ready(
        self,
        history: list[dict[str, Any]] | None,
        alerts: list[dict[str, Any]] | None,
        summary: RunSummary,
    ) -> None:
        if self.db.has_item("system", "startup:v1"):
            return
        if history is None or alerts is None:
            return
        summary.previews.append(
            render_message(
                "system",
                {
                    "title": "Codex 重置监控已启动",
                    "template": "blue",
                    "lines": [
                        f"History 基线：{len(history)} 条",
                        f"Alerts 基线：{len(alerts)} 条",
                        "后续只发送新事件、实质更新和健康状态转换。",
                    ],
                },
            )
        )

    def _deliver_pending(self, summary: RunSummary) -> None:
        if self.sender is None:
            summary.errors.append("delivery: sender is not configured")
            return
        for delivery in self.db.pending_deliveries():
            try:
                self.sender.send(render_message(delivery.source, delivery.payload))
                self.db.mark_delivered(delivery.id)
                post_id = _delivery_post_id(delivery.source, delivery.payload)
                if post_id:
                    self.db.mark_post_delivered(post_id)
                summary.delivered += 1
            except Exception as exc:
                self.db.mark_failed(delivery.id, str(exc))
                summary.errors.append(f"delivery {delivery.id}: {exc}")

    def _preview_pending(self, summary: RunSummary) -> None:
        for delivery in self.db.pending_deliveries():
            summary.previews.append(render_message(delivery.source, delivery.payload))


def history_payload(item: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id",
        "date",
        "kind",
        "phase",
        "announcedAt",
        "scope",
        "eventKind",
        "status",
        "resolvedAt",
        "targetAt",
        "supersededBy",
        "summary",
        "summaryFull",
        "operativeSentence",
        "classificationQuote",
        "publicNote",
        "evidenceUrl",
        "contextUrl",
        "sources",
        "corrections",
    )
    return {field: item.get(field) for field in fields}


def alert_payload(item: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id",
        "eventId",
        "postId",
        "correctsPostId",
        "classificationQuote",
        "targetAt",
        "topic",
        "state",
        "revision",
        "title",
        "summary",
        "sourceUrl",
        "evidenceId",
        "modelVersion",
        "sourcePublishedAt",
        "publishedAt",
    )
    return {field: item.get(field) for field in fields}


def _delivery_post_id(source: str, payload: dict[str, Any]) -> str | None:
    if source == "history" and payload.get("id") is not None:
        return str(payload["id"])
    if source == "alerts" and payload.get("postId") is not None:
        return str(payload["postId"])
    return None
