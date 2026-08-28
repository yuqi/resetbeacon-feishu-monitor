from __future__ import annotations

import base64
import hashlib
import hmac

import httpx
import pytest

from monitor.feishu import FeishuDeliveryError, FeishuSender, generate_sign


def test_generate_sign_matches_feishu_algorithm() -> None:
    timestamp = 1_700_000_000
    secret = "test-secret"
    key = f"{timestamp}\n{secret}".encode()
    expected = base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest()).decode()
    assert generate_sign(secret, timestamp) == expected


def test_http_200_business_failure_is_rejected() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"code": 19024, "msg": "Key Words Not Found"})
    )
    sender = FeishuSender(
        "https://example.invalid/hook",
        client=httpx.Client(transport=transport),
        max_retries=1,
    )
    with pytest.raises(FeishuDeliveryError, match="19024"):
        sender.send({"msg_type": "text", "content": {"text": "Codex test"}})


def test_success_response_is_accepted() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"StatusCode": 0, "StatusMessage": "success"},
        )
    )
    sender = FeishuSender(
        "https://example.invalid/hook",
        client=httpx.Client(transport=transport),
        max_retries=1,
    )
    sender.send({"msg_type": "text", "content": {"text": "Codex test"}})


def test_transport_error_does_not_expose_webhook_url() -> None:
    webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/very-secret-token"

    def fail(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=_request)

    sender = FeishuSender(
        webhook,
        client=httpx.Client(transport=httpx.MockTransport(fail)),
        max_retries=1,
    )
    with pytest.raises(FeishuDeliveryError) as caught:
        sender.send({"msg_type": "text", "content": {"text": "Codex test"}})
    assert webhook not in str(caught.value)
