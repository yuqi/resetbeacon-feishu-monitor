from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .db import StateDB, notification_key
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
        self._fetch("health", self.api.fetch_health, summary, dry_run)

        if history is not None:
            self._ingest_history(history, summary, dry_run)
        if alerts is not None:
            self._ingest_alerts(alerts, summary, dry_run)

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
        seen_in_run: set[str] = set()
        for item in sorted(items, key=lambda value: str(value.get("announcedAt") or "")):
            item_id = str(item.get("id") or "")
            if not item_id:
                summary.errors.append("history: item without id")
                continue
            payload = history_payload(item)
            key = notification_key("history", item_id, payload)
            if dry_run:
                first_seen = (
                    key not in seen_in_run
                    and not self.db.has_notification_key("history", key)
                )
                seen_in_run.add(key)
                if not initialized:
                    baselined += first_seen
                elif first_seen:
                    summary.previews.append(render_message("history", payload))
                continue
            notify = initialized
            _, first_seen = self.db.observe_once(
                "history", item_id, payload, key=key, notify=notify
            )
            if first_seen:
                if notify:
                    summary.queued += 1
                else:
                    baselined += 1
        if not dry_run and not initialized:
            self.db.mark_initialized("history")
        if baselined:
            summary.baselined["history"] = baselined

    def _ingest_alerts(
        self,
        items: list[dict[str, Any]],
        summary: RunSummary,
        dry_run: bool,
    ) -> None:
        initialized = self.db.is_initialized("alerts")
        baselined = 0
        seen_in_run: set[str] = set()
        for item in sorted(items, key=lambda value: str(value.get("publishedAt") or "")):
            item_id = str(item.get("id") or "")
            if not item_id:
                summary.errors.append("alerts: item without id")
                continue
            payload = alert_payload(item)
            key = notification_key("alerts", item_id, payload)
            if dry_run:
                first_seen = (
                    key not in seen_in_run
                    and not self.db.has_notification_key("alerts", key)
                )
                seen_in_run.add(key)
                if not initialized:
                    baselined += first_seen
                elif first_seen:
                    summary.previews.append(render_message("alerts", payload))
                continue
            notify = initialized
            _, first_seen = self.db.observe_once(
                "alerts", item_id, payload, key=key, notify=notify
            )
            if first_seen:
                if notify:
                    summary.queued += 1
                else:
                    baselined += 1
        if not dry_run and not initialized:
            self.db.mark_initialized("alerts")
        if baselined:
            summary.baselined["alerts"] = baselined

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
                "每个 Post 在 History、Alerts 内各发送一次；接口故障另行通知。",
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
                        "每个 Post 在 History、Alerts 内各发送一次；接口故障另行通知。",
                    ],
                },
            )
        )

    def _deliver_pending(self, summary: RunSummary) -> None:
        if self.sender is None:
            summary.errors.append("delivery: sender is not configured")
            return
        for delivery in self.db.pending_deliveries():
            if delivery.source == "health" and delivery.item_id == "service-health":
                self.db.mark_delivered(delivery.id)
                summary.suppressed += 1
                continue
            try:
                self.sender.send(render_message(delivery.source, delivery.payload))
                self.db.mark_delivered(delivery.id)
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
