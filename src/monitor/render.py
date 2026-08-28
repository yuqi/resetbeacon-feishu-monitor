from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


LOCAL_ZONE = ZoneInfo("Asia/Shanghai")
UTC_ZONE = ZoneInfo("UTC")


def render_message(source: str, payload: dict[str, Any]) -> dict[str, Any]:
    if source == "history":
        return _history_card(payload)
    if source == "alerts":
        return _alert_card(payload)
    if source == "health":
        return _health_card(payload)
    if source == "system":
        return _system_card(payload)
    raise ValueError(f"unsupported notification source: {source}")


def _history_card(item: dict[str, Any]) -> dict[str, Any]:
    kind = str(item.get("eventKind") or "unknown")
    labels = {
        "intent": ("⚠️ Codex 重置意图", "orange"),
        "scheduled": ("🗓️ Codex 重置已安排", "blue"),
        "completed": ("✅ Codex 重置已确认", "green"),
        "policy_change": ("📋 Codex 额度政策变化", "purple"),
    }
    title, template = labels.get(kind, ("Codex 重置事件", "blue"))
    summary = item.get("summaryFull") or item.get("summary") or item.get("operativeSentence")
    details: list[str] = []
    corrections = item.get("corrections")
    if isinstance(corrections, list) and corrections:
        details.append(f"修正：{len(corrections)} 条")
    return _event_card(
        title=title,
        template=template,
        summary=_truncate(str(summary or "（无摘要）")),
        fields=[
            ("类型", _history_kind_label(kind)),
            ("状态", _status_label(str(item.get("status") or "unknown"))),
        ],
        published_at=item.get("announcedAt"),
        details=details,
        actions=_history_actions(item),
    )


def _alert_card(item: dict[str, Any]) -> dict[str, Any]:
    topic = str(item.get("topic") or "alert")
    labels = {
        "likely": ("⚠️ Codex 重置概率预警", "orange"),
        "schedule": ("🗓️ Codex 重置已安排", "blue"),
        "action": ("✅ Codex 重置已确认", "green"),
        "correction": ("❌ Codex 重置信息修正", "red"),
    }
    title, template = labels.get(topic, ("Codex 重置提醒", "orange"))
    details: list[str] = []
    if not item.get("postId"):
        details.append("⚠️ 模型预测，不是 OpenAI 官方承诺")
    return _event_card(
        title=title,
        template=template,
        summary=_alert_summary(item),
        fields=[
            ("类型", _alert_topic_label(topic)),
            ("状态", _alert_state_label(str(item.get("state") or "unknown"))),
        ],
        published_at=item.get("publishedAt"),
        details=details,
        actions=_alert_actions(item),
    )


def _event_card(
    *,
    title: str,
    template: str,
    summary: str,
    fields: list[tuple[str, str]],
    published_at: Any,
    details: list[str],
    actions: list[tuple[str, str]],
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {"tag": "plain_text", "content": summary},
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "fields": [
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{label}**\n{value}",
                    },
                }
                for label, value in fields
            ],
        },
    ]
    note_parts = [*details, f"🕒 {format_local_time(published_at)}"]
    elements.append(
        {
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": "  ·  ".join(note_parts)},
            ],
        }
    )
    if actions:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": label},
                        "type": "primary" if index == 0 else "default",
                        "url": url,
                    }
                    for index, (label, url) in enumerate(actions)
                ],
            }
        )
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": elements,
        },
    }


def _health_card(item: dict[str, Any]) -> dict[str, Any]:
    status = str(item.get("status") or "unknown")
    title = "Codex Reset Beacon 已恢复" if status in {"ok", "recovered"} else "Codex Reset Beacon 数据源异常"
    template = "green" if status in {"ok", "recovered"} else "red"
    lines = [f"状态：{status}"]
    source = item.get("source")
    if source:
        lines.append(f"来源：{source}")
    reasons = item.get("reasons")
    if isinstance(reasons, list) and reasons:
        lines.append(f"原因：{', '.join(map(str, reasons))}")
    failures = item.get("consecutiveFailures")
    if failures:
        lines.append(f"连续失败：{failures} 次")
    return _card(title, template, lines, None, None)


def _system_card(item: dict[str, Any]) -> dict[str, Any]:
    lines = [str(line) for line in item.get("lines", [])]
    return _card(
        str(item.get("title") or "Codex 重置监控"),
        str(item.get("template") or "blue"),
        lines,
        None,
        None,
    )


def _card(
    title: str,
    template: str,
    lines: list[str],
    url: str | None,
    button_text: str | None,
) -> dict[str, Any]:
    if "Codex" not in title:
        title = f"Codex · {title}"
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": "\n".join(lines),
            },
        }
    ]
    if url and button_text:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": button_text},
                        "type": "primary",
                        "url": url,
                    }
                ],
            }
        )
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": elements,
        },
    }


def format_time(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "未知"
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    local = instant.astimezone(LOCAL_ZONE).strftime("%Y-%m-%d %H:%M:%S UTC+8")
    utc = instant.astimezone(UTC_ZONE).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"{local}（{utc}）"


def format_local_time(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "时间未知"
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return instant.astimezone(LOCAL_ZONE).strftime("%Y-%m-%d %H:%M:%S UTC+8")


def _history_kind_label(kind: str) -> str:
    return {
        "intent": "重置意图",
        "scheduled": "时间安排",
        "completed": "操作确认",
        "policy_change": "额度政策",
    }.get(kind, kind)


def _status_label(status: str) -> str:
    return {
        "active": "生效中",
        "scheduled": "已安排",
        "completed": "已确认",
        "recorded": "已记录",
        "superseded": "已取代",
        "expired": "已过期",
    }.get(status, status)


def _alert_topic_label(topic: str) -> str:
    return {
        "likely": "概率预警",
        "schedule": "时间安排",
        "action": "操作确认",
        "correction": "信息修正",
    }.get(topic, topic)


def _alert_state_label(state: str) -> str:
    return {
        "likely_forecast": "模型预测",
        "likely_official_intent": "官方意图",
        "schedule_claimed": "已安排",
        "action_claimed": "已确认",
        "correction_issued": "已修正",
    }.get(state, state)


def _alert_summary(item: dict[str, Any]) -> str:
    summary = str(item.get("summary") or item.get("title") or "（无摘要）")
    if not item.get("postId"):
        suffix = " This is a forecast, not an OpenAI announcement."
        if summary.endswith(suffix):
            summary = summary[: -len(suffix)]
    return _truncate(summary)


def _history_actions(item: dict[str, Any]) -> list[tuple[str, str]]:
    actions: list[tuple[str, str]] = []
    evidence_value: Any = item.get("evidenceUrl")
    sources = item.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict):
                url = _safe_url(source.get("url"))
                if url:
                    label = "查看 X 原帖" if urlparse(url).netloc in {"x.com", "www.x.com"} else "查看来源"
                    actions.append((label, url))
                    evidence_value = evidence_value or source.get("evidenceUrl")
                    break
    evidence_url = _evidence_url(evidence_value)
    if evidence_url and all(url != evidence_url for _, url in actions):
        actions.append(("查看证据", evidence_url))
    return actions


def _alert_actions(item: dict[str, Any]) -> list[tuple[str, str]]:
    url = _safe_url(item.get("sourceUrl"))
    if not url:
        return []
    host = urlparse(url).netloc
    if host in {"x.com", "www.x.com"}:
        label = "查看 X 原帖"
    elif not item.get("postId"):
        label = "查看预测依据"
    else:
        label = "查看来源"
    return [(label, url)]


def _evidence_url(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith("/evidence/"):
        return f"https://resetbeacon.com{value}"
    return _safe_url(value)


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    return value if parsed.scheme == "https" and parsed.netloc else None


def _truncate(value: str, limit: int = 8000) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 14] + "…（内容已截断）"
