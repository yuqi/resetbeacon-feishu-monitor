import pytest

from monitor.config import Config, ConfigError


@pytest.fixture(autouse=True)
def clean_translation_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_REASONING_EFFORT", "TRANSLATION_TIMEOUT_SECONDS", "TRANSLATION_MAX_RETRIES"):
        monkeypatch.delenv(name, raising=False)


def test_translation_is_disabled_without_openai_configuration() -> None:
    assert not Config.from_env().translation_enabled
    assert Config.from_env().translation_reasoning_effort is None


@pytest.mark.parametrize("raw,expected", [("", None), ("  ", None), (" HIGH ", "high"), ("medium", "medium"), ("none", "none"), ("minimal", "minimal"), ("low", "low"), ("xhigh", "xhigh"), ("max", "max")])
def test_optional_reasoning_effort_does_not_enable_translation(monkeypatch: pytest.MonkeyPatch, raw: str, expected: str | None) -> None:
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", raw)
    config = Config.from_env()
    assert config.translation_reasoning_effort == expected
    assert not config.translation_enabled


def test_invalid_reasoning_effort_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "deep")
    with pytest.raises(ConfigError, match="OPENAI_REASONING_EFFORT"):
        Config.from_env()


def test_complete_configuration_enables_translation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://provider.invalid/v1/")
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-key")
    monkeypatch.setenv("OPENAI_MODEL", "configured-model")
    config = Config.from_env()
    assert config.translation_enabled
    assert config.translation_base_url == "https://provider.invalid/v1"
    assert "private-test-key" not in repr(config)


@pytest.mark.parametrize("missing", ["OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL"])
def test_partial_configuration_is_rejected(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    for name, value in (("OPENAI_BASE_URL", "https://provider.invalid/v1"), ("OPENAI_API_KEY", "key"), ("OPENAI_MODEL", "model")):
        if name != missing:
            monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=missing):
        Config.from_env()


@pytest.mark.parametrize("url", ["provider.invalid", "https:///v1", "https://user:secret@provider.invalid/v1", "https://provider.invalid/v1?key=secret", "https://provider.invalid/v1#fragment", "https://provider.invalid/v1/chat/completions", "https://[broken/v1", "https://provider.invalid:invalid/v1"])
def test_invalid_base_url_is_rejected(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", url)
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("OPENAI_MODEL", "model")
    with pytest.raises(ConfigError, match="OPENAI_BASE_URL"):
        Config.from_env()


@pytest.mark.parametrize("name,value", [("TRANSLATION_TIMEOUT_SECONDS", "0"), ("TRANSLATION_TIMEOUT_SECONDS", "nan"), ("TRANSLATION_TIMEOUT_SECONDS", "inf"), ("TRANSLATION_MAX_RETRIES", "-1"), ("TRANSLATION_MAX_RETRIES", "1.5")])
def test_invalid_request_limits_are_rejected(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=name):
        Config.from_env()
