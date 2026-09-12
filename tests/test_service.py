from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any

from monitor.db import StateDB
from monitor.service import MonitorService
from monitor.render import message_body, render_message


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


class FakeTranslator:
    def __init__(self, fail: bool = False) -> None:
        self.bodies: list[str] = []
        self.fail = fail

    def translate(self, body: str) -> str:
        self.bodies.append(body)
        if self.fail:
            raise RuntimeError("temporary translation failure")
        return "所有 Codex 用户的用量即将重置。"


def queue_event(db: StateDB, api: FakeAPI, sender: FakeSender, source: str = "history") -> dict:
    MonitorService(db, api, sender).run_once()
    sender.messages.clear()
    payload = copy.deepcopy(api.history[0] if source == "history" else api.alerts[1])
    payload["id"] = "new-post"
    db.observe_once(source, "new-post", payload, key="post:new-post", notify=True)
    return payload


def test_translation_follows_original_and_keeps_card_metadata(tmp_path: Path) -> None:
    api, sender, translator = FakeAPI(), FakeSender(), FakeTranslator()
    with StateDB(tmp_path / "state.sqlite3") as db:
        payload = queue_event(db, api, sender)
        summary = MonitorService(db, api, sender, translator).run_once()
        assert summary.delivered == 1
        assert summary.translation_delivered == 1
        assert summary.translation_pending == 0
        assert len(sender.messages) == 2
        assert sender.messages[0] == render_message("history", payload)
        original, translated = [message["card"] for message in sender.messages]
        assert translated["elements"][0]["text"]["content"] == "所有 Codex 用户的用量即将重置。"
        assert translated["elements"][1:] == original["elements"][1:]
        assert "中文译文" in translated["header"]["title"]["content"]
        assert translator.bodies == [message_body("history", payload)]
        assert db.stats()["translation_delivered"] == 1
        again = MonitorService(db, api, sender, translator).run_once()
        assert again.delivered == again.translation_delivered == 0
        assert len(translator.bodies) == 1


def test_translation_failure_retries_after_restart_without_resending_original(tmp_path: Path) -> None:
    api, sender = FakeAPI(), FakeSender()
    path = tmp_path / "state.sqlite3"
    with StateDB(path) as db:
        queue_event(db, api, sender)
        failed = MonitorService(db, api, sender, FakeTranslator(fail=True)).run_once()
        assert failed.delivered == 1
        assert failed.pending == 0
        assert failed.translation_pending == 1
        assert failed.errors
        assert len(sender.messages) == 1
    sender.messages.clear()
    with StateDB(path) as db:
        recovered = MonitorService(db, api, sender, FakeTranslator()).run_once()
        assert recovered.delivered == 0
        assert recovered.translation_delivered == 1
        assert recovered.translation_pending == 0
        assert len(sender.messages) == 1
        assert "中文译文" in sender.messages[0]["card"]["header"]["title"]["content"]


def test_failed_chinese_send_reuses_saved_translation_after_restart(tmp_path: Path) -> None:
    class FailChineseSender(FakeSender):
        def send(self, message: dict[str, Any]) -> None:
            if "中文译文" in message["card"]["header"]["title"]["content"]:
                raise RuntimeError("Chinese delivery failed")
            super().send(message)

    api, sender, translator = FakeAPI(), FailChineseSender(), FakeTranslator()
    path = tmp_path / "state.sqlite3"
    with StateDB(path) as db:
        queue_event(db, api, sender)
        failed = MonitorService(db, api, sender, translator).run_once()
        assert failed.translation_pending == 1
        assert len(translator.bodies) == 1
    with StateDB(path) as db:
        no_calls = FakeTranslator(fail=True)
        recovered = MonitorService(db, api, FakeSender(), no_calls).run_once()
        assert recovered.translation_delivered == 1
        assert recovered.translation_pending == 0
        assert no_calls.bodies == []


def test_failed_original_does_not_translate(tmp_path: Path) -> None:
    api, sender, translator = FakeAPI(), FakeSender(), FakeTranslator()
    with StateDB(tmp_path / "state.sqlite3") as db:
        queue_event(db, api, sender)
        failed = MonitorService(db, api, FakeSender(fail_count=1), translator).run_once()
        assert failed.pending == 1
        assert failed.translation_pending == 0
        assert translator.bodies == []
        recovered = MonitorService(db, api, sender, translator).run_once()
        assert recovered.delivered == recovered.translation_delivered == 1


def test_no_translation_backfill_for_baseline_or_previously_sent_messages(tmp_path: Path) -> None:
    api, sender, translator = FakeAPI(), FakeSender(), FakeTranslator()
    with StateDB(tmp_path / "state.sqlite3") as db:
        baseline = MonitorService(db, api, sender, translator).run_once()
        assert baseline.delivered == 1
        assert translator.bodies == []
        queue_event(db, api, sender)
        without_translation = MonitorService(db, api, sender).run_once()
        assert without_translation.delivered == 1
        assert db.pending_translations() == []
        enabled = MonitorService(db, api, sender, translator).run_once()
        assert enabled.translation_delivered == 0
        assert translator.bodies == []


def test_removing_config_pauses_pending_chinese_send(tmp_path: Path) -> None:
    api, sender = FakeAPI(), FakeSender()
    with StateDB(tmp_path / "state.sqlite3") as db:
        queue_event(db, api, sender)
        MonitorService(db, api, sender, FakeTranslator(fail=True)).run_once()
        sender.messages.clear()
        paused = MonitorService(db, api, sender).run_once()
        assert paused.translation_pending == 1
        assert sender.messages == []
        resumed = MonitorService(db, api, sender, FakeTranslator()).run_once()
        assert resumed.translation_delivered == 1


def test_dry_run_never_calls_translation_or_changes_pending_records(tmp_path: Path) -> None:
    api, sender, translator = FakeAPI(), FakeSender(), FakeTranslator()
    with StateDB(tmp_path / "state.sqlite3") as db:
        queue_event(db, api, sender)
        before = db.stats()
        preview = MonitorService(db, api, sender, translator).run_once(dry_run=True)
        assert preview.previews
        assert translator.bodies == []
        assert sender.messages == []
        assert db.stats() == before


def test_forecast_translation_preserves_warning_and_original_dedup(tmp_path: Path) -> None:
    api, sender, translator = FakeAPI(), FakeSender(), FakeTranslator()
    with StateDB(tmp_path / "state.sqlite3") as db:
        payload = queue_event(db, api, sender, "alerts")
        summary = MonitorService(db, api, sender, translator).run_once()
        assert summary.translation_delivered == 1
        assert translator.bodies == [message_body("alerts", payload)]
        assert "模型预测，不是 OpenAI 官方承诺" in sender.messages[1]["card"]["elements"][3]["elements"][0]["content"]
        # A changed source payload with the same original notification key creates no translation.
        db.observe_once("alerts", "new-post", {**payload, "summary": "Revised"}, key="post:new-post", notify=True)
        again = MonitorService(db, api, sender, translator).run_once()
        assert again.delivered == again.translation_delivered == 0
        assert len(translator.bodies) == 1


def test_old_database_gains_empty_translation_history(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    with StateDB(path) as db:
        queue_event(db, FakeAPI(), FakeSender())
        original_stats = db.stats()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE translation_deliveries")
    with StateDB(path) as db:
        assert db.stats() == original_stats
        assert db.pending_translations() == []


def test_translation_record_and_original_success_are_atomic(tmp_path: Path) -> None:
    with StateDB(tmp_path / "state.sqlite3") as db:
        queue_event(db, FakeAPI(), FakeSender())
        delivery_id = db.pending_deliveries()[0].id
        db.connection.execute("""
            CREATE TRIGGER fail_translation BEFORE INSERT ON translation_deliveries
            BEGIN SELECT RAISE(ABORT, 'simulate failure'); END;
        """)
        try:
            db.mark_delivered(delivery_id, queue_translation=True)
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("expected simulated write failure")
        assert db.pending_deliveries()[0].id == delivery_id
        assert db.pending_translations() == []


def test_first_run_baselines_and_only_sends_startup(tmp_path: Path) -> None:
    api = FakeAPI()
    sender = FakeSender()
    with StateDB(tmp_path / "state.sqlite3") as db:
        summary = MonitorService(db, api, sender).run_once()
        assert summary.baselined == {"history": 1, "alerts": 2}
        assert summary.delivered == 1
        assert len(sender.messages) == 1
        assert "Codex 重置监控已启动" in sender.messages[0]["card"]["header"]["title"]["content"]

        again = MonitorService(db, api, sender).run_once()
        assert again.delivered == 0
        assert len(sender.messages) == 1


def test_history_and_alert_with_same_post_are_both_delivered(tmp_path: Path) -> None:
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

        matching_alert = copy.deepcopy(api.alerts[0])
        matching_alert["id"] = "event:action:2093014447833116908:rev1"
        matching_alert["postId"] = "2093014447833116908"
        matching_alert["topic"] = "action"
        api.alerts.append(matching_alert)

        summary = MonitorService(db, api, sender).run_once()
        assert summary.delivered == 2
        assert summary.suppressed == 0
        assert sender.messages[0]["card"]["header"]["title"]["content"] == "Codex Reset · completed"
        assert sender.messages[1]["card"]["header"]["title"]["content"] == "Codex reset looks likely"


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


def test_history_change_for_seen_post_is_not_delivered_again(tmp_path: Path) -> None:
    api = FakeAPI()
    sender = FakeSender()
    with StateDB(tmp_path / "state.sqlite3") as db:
        MonitorService(db, api, sender).run_once()
        sender.messages.clear()
        api.history[0]["status"] = "expired"

        first = MonitorService(db, api, sender).run_once()
        second = MonitorService(db, api, sender).run_once()
        assert first.delivered == 0
        assert second.delivered == 0
        assert sender.messages == []


def test_new_alert_revision_for_seen_post_is_not_delivered_again(tmp_path: Path) -> None:
    api = FakeAPI()
    sender = FakeSender()
    with StateDB(tmp_path / "state.sqlite3") as db:
        MonitorService(db, api, sender).run_once()
        sender.messages.clear()
        revised = copy.deepcopy(api.alerts[0])
        revised["id"] = "event:likely:2092862554632826968:rev2"
        revised["revision"] = "rev2"
        revised["summary"] = "Updated classification for the same post."
        api.alerts.append(revised)

        summary = MonitorService(db, api, sender).run_once()

        assert summary.delivered == 0
        assert sender.messages == []


def test_upstream_health_transitions_are_not_delivered(tmp_path: Path) -> None:
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
        assert degraded.delivered == 0
        assert sender.messages == []

        api.health = fixture("health.json")
        recovered = MonitorService(db, api, sender).run_once()
        assert recovered.delivered == 0
        assert sender.messages == []


def test_pending_upstream_health_delivery_is_suppressed(tmp_path: Path) -> None:
    api = FakeAPI()
    sender = FakeSender()
    with StateDB(tmp_path / "state.sqlite3") as db:
        MonitorService(db, api, sender).run_once()
        sender.messages.clear()
        db.observe(
            "health",
            "service-health",
            {"status": "degraded", "reasons": ["collect_stale"]},
            notify=True,
        )

        summary = MonitorService(db, api, sender).run_once()

        assert summary.delivered == 0
        assert summary.suppressed == 1
        assert summary.pending == 0
        assert sender.messages == []


def test_health_endpoint_outage_and_recovery_are_still_delivered(tmp_path: Path) -> None:
    api = FakeAPI()
    sender = FakeSender()
    with StateDB(tmp_path / "state.sqlite3") as db:
        MonitorService(db, api, sender).run_once()
        sender.messages.clear()
        api.health = RuntimeError("health unavailable")

        first = MonitorService(db, api, sender).run_once()
        second = MonitorService(db, api, sender).run_once()
        third = MonitorService(db, api, sender).run_once()
        assert first.delivered == 0
        assert second.delivered == 0
        assert third.delivered == 1
        assert "数据源异常" in sender.messages[-1]["card"]["header"]["title"]["content"]

        api.health = fixture("health.json")
        recovered = MonitorService(db, api, sender).run_once()
        assert recovered.delivered == 1
        assert "已恢复" in sender.messages[-1]["card"]["header"]["title"]["content"]


def test_failed_delivery_remains_pending_and_retries(tmp_path: Path) -> None:
    api = FakeAPI()
    sender = FakeSender()
    with StateDB(tmp_path / "state.sqlite3") as db:
        MonitorService(db, api, sender).run_once()
        new_item = copy.deepcopy(api.history[0])
        new_item["id"] = "2093999999999999998"
        new_item["sources"][0]["announcementId"] = new_item["id"]
        new_item["sources"][0]["url"] = f"https://x.com/thsottiaux/status/{new_item['id']}"
        api.history.append(new_item)
        failing = FakeSender(fail_count=1)

        failed = MonitorService(db, api, failing).run_once()
        assert failed.pending == 1
        assert failed.errors

        recovered = MonitorService(db, api, sender).run_once()
        assert recovered.delivered == 1
        assert recovered.pending == 0


def test_existing_items_seed_post_keys_during_migration(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    alert = fixture("alerts.json")["items"][0]
    with StateDB(path) as db:
        db.observe("alerts", alert["id"], alert, notify=False)
        db.connection.execute("DELETE FROM notification_keys")
        db.connection.execute(
            "DELETE FROM metadata WHERE key = 'migration:notification-keys:v1'"
        )
        db.connection.commit()

    with StateDB(path) as db:
        assert db.has_notification_key("alerts", f"post:{alert['postId']}")


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
        assert summary.baselined == {"history": 1, "alerts": 2}
        assert len(summary.previews) == 1


def test_alert_sent_during_history_outage_does_not_suppress_late_history(tmp_path: Path) -> None:
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
        assert history_run.delivered == 1
        assert history_run.suppressed == 0
        assert len(sender.messages) == 2
