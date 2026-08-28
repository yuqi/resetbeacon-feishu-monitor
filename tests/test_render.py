from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from monitor.render import render_message
from monitor.service import alert_payload, history_payload


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def test_history_uses_structured_fields_and_separate_actions() -> None:
    item = fixture("history.json")["items"][0]

    message = render_message("history", history_payload(item))

    card = message["card"]
    assert card["header"]["title"]["content"] == "⚠️ Codex 重置意图"
    assert card["elements"][0]["text"]["content"] == item["summaryFull"]
    assert card["elements"][1] == {"tag": "hr"}
    assert [field["text"]["content"] for field in card["elements"][2]["fields"]] == [
        "**类型**\n重置意图",
        "**状态**\n生效中",
    ]
    note = card["elements"][3]["elements"][0]["content"]
    assert note == "🕒 2026-08-27 14:31:31 UTC+8"
    actions = card["elements"][4]["actions"]
    assert [action["text"]["content"] for action in actions] == ["查看 X 原帖", "查看证据"]


def test_forecast_alert_has_human_labels_and_warning() -> None:
    item = fixture("alerts.json")["items"][1]

    message = render_message("alerts", alert_payload(item))

    card = message["card"]
    assert card["header"]["title"]["content"] == "⚠️ Codex 重置概率预警"
    assert card["elements"][0]["text"]["content"] == "The odds reached 58% by tomorrow."
    assert [field["text"]["content"] for field in card["elements"][2]["fields"]] == [
        "**类型**\n概率预警",
        "**状态**\n模型预测",
    ]
    note = card["elements"][3]["elements"][0]["content"]
    assert note == "⚠️ 模型预测，不是 OpenAI 官方承诺  ·  🕒 2026-08-27 18:15:05 UTC+8"
    assert card["elements"][4]["actions"][0]["text"]["content"] == "查看预测依据"
