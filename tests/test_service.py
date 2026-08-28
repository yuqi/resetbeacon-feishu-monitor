from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from monitor.db import StateDB
from monitor.service import MonitorService


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeAPI:
    def __init__(self) -> None:
        self.history: Any = fixture("history.json")["items"]
        self.alerts: Any = fixture("alerts.json")["items"]
        self.health: Any = fixture("health.json")

    def fetch_history(self) -> list[dict[str, Any]]:
        if isinstance(self.history, Exception):
            raise self.history
        return copy.deepcopy(self.history)

    def fetch_alerts(self) -> list[dict[str, Any]]:
        if isinstance(self.alerts, Exception):
            raise self.alerts
        return copy.deepcopy(self.alerts)

    def fetch_health(self) -> dict[str, Any]:
        if isinstance(self.health, Exception):
            raise self.health
        return copy.deepcopy(self.health)


class FakeSender:
    def __init__(self, fail_count: int = 0) -> None:
        self.messages: list[dict[str, Any]] = []
        self.fail_count = fail_count

    def send(self, message: dict[str, Any]) -> None:
        if self.fail_count:
            self.fail_count -= 1
            raise RuntimeError("temporary delivery failure")
        self.messages.append(message)


def test_first_run_baselines_and_only_sends_startup(tmp_path: Path) -> None:
    api = FakeAPI()
    sender = FakeSender()
    with StateDB(tmp_path / "state.sqlite3") as db:
        summary = MonitorService(db, api, sender).run_once()
        assert summary.baselined == {"history": 1, "alerts": 2, "health": 1}
        assert summary.delivered == 1
        assert len(sender.messages) == 1
        assert "Codex 重置监控已启动" in sender.messages[0]["card"]["header"]["title"]["content"]

        again = MonitorService(db, api, sender).run_once()
        assert again.delivered == 0
        assert len(sender.messages) == 1


def test_new_history_wins_over_alert_with_same_post(tmp_path: Path) -> None:
    api = FakeAPI()
    sender = FakeSender()
    with StateDB(tmp_path / "state.sqlite3") as db:
        MonitorService(db, api, sender).run_once()
        sender.messages.clear()

        new_item = copy.deepcopy(api.history[0])
        new_item["id"] = "2093014447833116908"
        new_item["eventKind"] = "completed"
        new_item["status"] = "completed"
        new_item["summaryFull"] = "Brand new usage for all ChatGPT Work and Codex users."
        new_item["sources"][0]["url"] = "https://x.com/thsottiaux/status/2093014447833116908"
        api.history.append(new_item)

        duplicate_alert = copy.deepcopy(api.alerts[0])
        duplicate_alert["id"] = "event:action:2093014447833116908:rev1"
        duplicate_alert["postId"] = "2093014447833116908"
        duplicate_alert["topic"] = "action"
        api.alerts.append(duplicate_alert)

        summary = MonitorService(db, api, sender).run_once()
        assert summary.delivered == 1
        assert summary.suppressed == 1
        assert sender.messages[0]["card"]["header"]["title"]["content"] == "completed"


def test_forecast_only_alert_is_delivered(tmp_path: Path) -> None:
    api = FakeAPI()
    api.alerts = api.alerts[:1]
    sender = FakeSender()
    with StateDB(tmp_path / "state.sqlite3") as db:
        MonitorService(db, api, sender).run_once()
        sender.messages.clear()
        api.alerts.append(fixture("alerts.json")["items"][1])

        summary = MonitorService(db, api, sender).run_once()
        assert summary.delivered == 1
        card = sender.messages[0]["card"]
        assert card["header"]["title"]["content"] == "The odds reached 58% by tomorrow."
        note = card["elements"][3]["elements"][0]["content"]
        assert "模型预测，不是 OpenAI 官方承诺" in note


def test_history_change_is_delivered_once(tmp_path: Path) -> None:
    api = FakeAPI()
    sender = FakeSender()
    with StateDB(tmp_path / "state.sqlite3") as db:
        MonitorService(db, api, sender).run_once()
        sender.messages.clear()
        api.history[0]["status"] = "expired"

        first = MonitorService(db, api, sender).run_once()
        second = MonitorService(db, api, sender).run_once()
        assert first.delivered == 1
        assert second.delivered == 0


def test_health_transition_is_delivered_once(tmp_path: Path) -> None:
    api = FakeAPI()
    sender = FakeSender()
    with StateDB(tmp_path / "state.sqlite3") as db:
        MonitorService(db, api, sender).run_once()
        sender.messages.clear()
        api.health = {
            "status": "degraded",
            "checkedAt": "2026-08-28T04:00:00.000Z",
            "reasons": ["own_reader_missed"],
        }

        degraded = MonitorService(db, api, sender).run_once()
        repeated = MonitorService(db, api, sender).run_once()
        assert degraded.delivered == 1
        assert repeated.delivered == 0
        assert "数据源异常" in sender.messages[0]["card"]["header"]["title"]["content"]

        api.health = fixture("health.json")
        recovered = MonitorService(db, api, sender).run_once()
        assert recovered.delivered == 1
        assert "已恢复" in sender.messages[-1]["card"]["header"]["title"]["content"]


def test_failed_delivery_remains_pending_and_retries(tmp_path: Path) -> None:
    api = FakeAPI()
    sender = FakeSender()
    with StateDB(tmp_path / "state.sqlite3") as db:
        MonitorService(db, api, sender).run_once()
        api.history[0]["status"] = "expired"
        failing = FakeSender(fail_count=1)

        failed = MonitorService(db, api, failing).run_once()
        assert failed.pending == 1
        assert failed.errors

        recovered = MonitorService(db, api, sender).run_once()
        assert recovered.delivered == 1
        assert recovered.pending == 0


def test_one_source_failure_does_not_block_another(tmp_path: Path) -> None:
    api = FakeAPI()
    sender = FakeSender()
    with StateDB(tmp_path / "state.sqlite3") as db:
        MonitorService(db, api, sender).run_once()
        sender.messages.clear()
        api.history = RuntimeError("history unavailable")
        api.alerts.append(
            {
                **fixture("alerts.json")["items"][1],
                "id": "event:likely:forecast:rev2",
                "revision": "rev2",
            }
        )

        summary = MonitorService(db, api, sender).run_once()
        assert any(error.startswith("history:") for error in summary.errors)
        assert summary.delivered == 1


def test_dry_run_does_not_initialize_or_send(tmp_path: Path) -> None:
    api = FakeAPI()
    sender = FakeSender()
    with StateDB(tmp_path / "state.sqlite3") as db:
        summary = MonitorService(db, api, sender).run_once(dry_run=True)
        assert not db.is_initialized("history")
        assert not db.is_initialized("alerts")
        assert db.stats()["items"] == 0
        assert sender.messages == []
        assert summary.baselined == {"history": 1, "alerts": 2, "health": 1}
        assert len(summary.previews) == 1


def test_alert_sent_during_history_outage_suppresses_late_history(tmp_path: Path) -> None:
    api = FakeAPI()
    sender = FakeSender()
    with StateDB(tmp_path / "state.sqlite3") as db:
        MonitorService(db, api, sender).run_once()
        sender.messages.clear()

        post_id = "2093999999999999999"
        api.history = RuntimeError("history unavailable")
        official_alert = copy.deepcopy(fixture("alerts.json")["items"][0])
        official_alert["id"] = f"event:action:{post_id}:rev1"
        official_alert["postId"] = post_id
        official_alert["topic"] = "action"
        official_alert["sourceUrl"] = f"https://x.com/thsottiaux/status/{post_id}"
        api.alerts.append(official_alert)

        alert_run = MonitorService(db, api, sender).run_once()
        assert alert_run.delivered == 1

        late_history = copy.deepcopy(fixture("history.json")["items"][0])
        late_history["id"] = post_id
        late_history["eventKind"] = "completed"
        late_history["status"] = "completed"
        late_history["sources"][0]["url"] = f"https://x.com/thsottiaux/status/{post_id}"
        api.history = [*fixture("history.json")["items"], late_history]

        history_run = MonitorService(db, api, sender).run_once()
        assert history_run.delivered == 0
        assert history_run.suppressed == 1
