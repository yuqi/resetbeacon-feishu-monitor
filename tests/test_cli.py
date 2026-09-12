import copy
import json
from pathlib import Path

import httpx
import pytest

from monitor.cli import main


@pytest.mark.parametrize("configured,dry_run", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("effort", ["", "high"])
@pytest.mark.parametrize("language", ["", "en", "zh"])
def test_cli_automatically_translates_only_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    configured: bool, dry_run: bool,
    effort: str,
    language: str,
) -> None:
    fixtures = Path(__file__).parent / "fixtures"
    history = json.loads((fixtures / "history.json").read_text())
    alerts = json.loads((fixtures / "alerts.json").read_text())
    health = json.loads((fixtures / "health.json").read_text())
    sent = []
    translated = []
    for name, value in {
        "RESET_BEACON_HISTORY_URL": "https://source.invalid/history",
        "RESET_BEACON_ALERTS_URL": "https://source.invalid/alerts",
        "RESET_BEACON_HEALTH_URL": "https://source.invalid/health",
        "FEISHU_WEBHOOK_URL": "https://feishu.invalid/hook",
        "FEISHU_SIGNING_SECRET": "",
        "STATE_DB_PATH": str(tmp_path / "state.sqlite3"),
        "OPENAI_BASE_URL": "https://provider.invalid/v1" if configured else "",
        "OPENAI_API_KEY": "private-test-key" if configured else "",
        "OPENAI_MODEL": "configured-model" if configured else "",
        "OPENAI_REASONING_EFFORT": effort,
        "SEND_LANGUAGE": language,
        "TRANSLATION_TIMEOUT_SECONDS": "20",
        "TRANSLATION_MAX_RETRIES": "1",
    }.items():
        monkeypatch.setenv(name, value)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "source.invalid":
            return httpx.Response(200, json={"/history": history, "/alerts": alerts, "/health": health}[request.url.path])
        if request.url.host == "feishu.invalid":
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"code": 0})
        assert request.url == "https://provider.invalid/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == "configured-model"
        if effort:
            assert payload["reasoning_effort"] == effort
        else:
            assert "reasoning_effort" not in payload
        assert request.headers["Authorization"] == "Bearer private-test-key"
        translated.append(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": "新的重置消息。"}, "finish_reason": "stop"}]})

    client_class = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_class(transport=httpx.MockTransport(handle), **kwargs))
    if language == "zh" and not configured:
        assert main(["run-once"]) == 2
        assert sent == translated == []
        return
    assert main(["run-once"]) == 0
    assert len(sent) == 1  # Startup never invokes the model.
    assert translated == []
    sent.clear()
    item = copy.deepcopy(history["items"][0])
    item["id"] = "new-post"
    history["items"].append(item)

    assert main(["run-once", *(["--dry-run"] if dry_run else [])]) == 0
    expected_translations = int(configured and language != "en")
    expected_messages = int(language != "zh") + expected_translations
    assert len(translated) == (0 if dry_run else expected_translations)
    assert len(sent) == (0 if dry_run else expected_messages)
    if expected_translations and not dry_run:
        assert sent[-1]["card"]["elements"][0]["text"]["content"] == "新的重置消息。"
    if not dry_run:
        assert main(["run-once"]) == 0
        assert len(translated) == expected_translations
        assert len(sent) == expected_messages
