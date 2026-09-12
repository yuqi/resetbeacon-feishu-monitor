from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from monitor.render import message_body, render_message, render_translation
from monitor.service import alert_payload, history_payload


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def test_full_body_is_translated_before_card_truncation() -> None:
    for source, payload in (
        ("history", {"summaryFull": "English " * 1500}),
        ("alerts", {"summary": "English " * 1500}),
    ):
        assert len(message_body(source, payload)) > 8000
        original = render_message(source, payload)
        translated = render_translation(source, payload, "中文" * 5000)
        assert len(original["card"]["elements"][0]["text"]["content"]) <= 8000
        assert len(translated["card"]["elements"][0]["text"]["content"]) <= 8000


def test_history_uses_structured_fields_and_separate_actions() -> None:
    item = fixture("history.json")["items"][0]

    message = render_message("history", history_payload(item))

    card = message["card"]
    assert card["header"]["title"]["content"] == "Codex Reset · intent"
    assert card["elements"][0]["text"]["content"] == item["summaryFull"]
    assert card["elements"][1] == {"tag": "hr"}
    assert [field["text"]["content"] for field in card["elements"][2]["fields"]] == [
        "**类别**\nspecial_global",
        "**阶段**\nofficial_intent",
        "**状态**\nactive",
        "**范围**\nall",
    ]
    note = card["elements"][3]["elements"][0]["content"]
    assert note == "🕒 2026-08-27 14:31:31 UTC+8"
    actions = card["elements"][4]["actions"]
    assert [action["text"]["content"] for action in actions] == ["查看 X 原帖", "查看证据"]


def test_unknown_history_classifications_fall_back_to_raw_values() -> None:
    item = fixture("history.json")["items"][0]
    item.update(kind="future_kind", phase="future_phase", status="future_status", scope="future_scope")

    message = render_message("history", history_payload(item))

    fields = message["card"]["elements"][2]["fields"]
    assert [field["text"]["content"] for field in fields] == [
        "**类别**\nfuture_kind",
        "**阶段**\nfuture_phase",
        "**状态**\nfuture_status",
        "**范围**\nfuture_scope",
    ]


def test_forecast_alert_has_human_labels_and_warning() -> None:
    item = fixture("alerts.json")["items"][1]

    message = render_message("alerts", alert_payload(item))

    card = message["card"]
    assert card["header"]["title"]["content"] == "The odds reached 58% by tomorrow."
    assert card["elements"][0]["text"]["content"] == "The odds reached 58% by tomorrow."
    assert [field["text"]["content"] for field in card["elements"][2]["fields"]] == [
        "**类型**\nlikely",
        "**状态**\nlikely_forecast",
    ]
    note = card["elements"][3]["elements"][0]["content"]
    assert note == "⚠️ 模型预测，不是 OpenAI 官方承诺  ·  🕒 2026-08-27 18:15:05 UTC+8"
    assert card["elements"][4]["actions"][0]["text"]["content"] == "查看预测依据"
