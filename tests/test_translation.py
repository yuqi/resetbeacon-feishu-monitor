from __future__ import annotations

import json

import httpx
import pytest

from monitor.translation import OpenAITranslator, TranslationError


def completion(content: object = "明天重置概率为 58%。", finish_reason: str = "stop") -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


@pytest.mark.parametrize("effort", [None, "medium", "high"])
def test_compatible_request_and_chinese_response(effort: str | None) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://provider.invalid/api/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-secret"
        payload = json.loads(request.content)
        assert payload["model"] == "configured-model"
        assert payload["stream"] is False
        if effort is None:
            assert "reasoning_effort" not in payload
        else:
            assert payload["reasoning_effort"] == effort
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1] == {"role": "user", "content": "The odds reached 58% by tomorrow."}
        return httpx.Response(200, json=completion())

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        translator = OpenAITranslator(
            base_url="https://provider.invalid/api/v1/", api_key="test-secret",
            model="configured-model", client=client,
            reasoning_effort=effort,
        )
        assert translator.translate("The odds reached 58% by tomorrow.") == "明天重置概率为 58%。"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_http_error_retries(status: int) -> None:
    responses = [httpx.Response(status), httpx.Response(200, json=completion())]
    sleeps = []
    with httpx.Client(transport=httpx.MockTransport(lambda _: responses.pop(0))) as client:
        translator = OpenAITranslator(
            base_url="https://provider.invalid/v1", api_key="secret", model="model",
            client=client, sleep=sleeps.append,
        )
        assert translator.translate("Forecast") == "明天重置概率为 58%。"
    assert sleeps == [0.5]


@pytest.mark.parametrize("payload", [
    {}, {"choices": []}, {"choices": [None]}, completion(None), completion("  "),
    completion("半截译文", "length"), completion("拒绝", "content_filter"),
    {"choices": [{"message": {"content": "拒绝", "refusal": "refused"}, "finish_reason": "stop"}]},
])
def test_invalid_or_incomplete_output_is_not_sent(payload: object) -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        translator = OpenAITranslator(
            base_url="https://provider.invalid/v1", api_key="secret", model="model",
            client=client, max_retries=1,
        )
        with pytest.raises(TranslationError, match="incomplete"):
            translator.translate("Original")


@pytest.mark.parametrize("status", [400, 401])
def test_permanent_http_error_does_not_retry_or_leak_secrets(status: int) -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert json.loads(request.content)["reasoning_effort"] == "high"
        return httpx.Response(status, text="sensitive-provider-response secret-key")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        translator = OpenAITranslator(
            base_url="https://provider.invalid/secret-path", api_key="secret-key", model="model",
            reasoning_effort="high",
            client=client, sleep=lambda _: pytest.fail("must not retry"),
        )
        with pytest.raises(TranslationError) as caught:
            translator.translate("Original")
    assert len(requests) == 1
    assert str(caught.value) == f"translation failed after 1 attempt(s): HTTP {status}"


def test_timeout_retries_without_leaking_url() -> None:
    calls = []

    def fail(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("secret URL and credentials", request=request)

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        translator = OpenAITranslator(
            base_url="https://provider.invalid/secret", api_key="secret", model="model",
            client=client, max_retries=2, sleep=lambda _: None,
        )
        with pytest.raises(TranslationError) as caught:
            translator.translate("Original")
    assert len(calls) == 2
    assert str(caught.value) == "translation failed after 2 attempt(s): ReadTimeout"
