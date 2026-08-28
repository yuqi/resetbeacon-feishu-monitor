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
        "intent": ("Codex 重置意图", "orange"),
        "scheduled": ("Codex 重置已安排", "blue"),
        "completed": ("Codex 重置已确认", "green"),
        "policy_change": ("Codex 额度政策变化", "purple"),
    }
    title, template = labels.get(kind, ("Codex 重置事件", "blue"))
    summary = item.get("summaryFull") or item.get("summary") or item.get("operativeSentence")
    source_url = _history_source_url(item)
    lines = [
        f"类型：{kind}",
        f"状态：{item.get('status', 'unknown')}",
        f"发布时间：{format_time(item.get('announcedAt'))}",
        f"内容：{_truncate(str(summary or '（无摘要）'))}",
    ]
    corrections = item.get("corrections")
    if isinstance(corrections, list) and corrections:
        lines.append(f"修正：{len(corrections)} 条")
    return _card(title, template, lines, source_url, "查看原始证据")


def _alert_card(item: dict[str, Any]) -> dict[str, Any]:
    topic = str(item.get("topic") or "alert")
    templates = {
        "likely": "orange",
        "schedule": "blue",
        "action": "green",
        "correction": "red",
    }
    title = str(item.get("title") or "重置提醒")
    if "Codex" not in title:
        title = f"Codex · {title}"
    lines = [
        f"类型：{topic}",
        f"状态：{item.get('state', 'unknown')}",
        f"发布时间：{format_time(item.get('publishedAt'))}",
        f"内容：{_truncate(str(item.get('summary') or '（无摘要）'))}",
    ]
    if not item.get("postId"):
        lines.append("说明：这是模型概率预警，不是 OpenAI 官方承诺。")
    return _card(
        title,
        templates.get(topic, "orange"),
        lines,
        _safe_url(item.get("sourceUrl")),
        "查看来源",
    )


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


def _history_source_url(item: dict[str, Any]) -> str | None:
    sources = item.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict):
                url = _safe_url(source.get("url"))
                if url:
                    return url
    evidence_url = item.get("evidenceUrl")
    if isinstance(evidence_url, str) and evidence_url.startswith("/evidence/"):
        return f"https://resetbeacon.com{evidence_url}"
    return None


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    return value if parsed.scheme == "https" and parsed.netloc else None


def _truncate(value: str, limit: int = 8000) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 14] + "…（内容已截断）"

