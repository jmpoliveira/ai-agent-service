from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from config.settings import Settings

_BASE: dict[str, Any] = {
    "mcp_server_url": "http://mcp.test:3000/mcp",
    "ollama_base_url": "http://ollama.test:11434",
    "ollama_model": "local-model",
}


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must not depend on the developer's shell or .env file."""
    for name in list(__import__("os").environ):
        if name.startswith(
            ("OLLAMA_", "GEMINI_", "ANTHROPIC_", "LLM_", "MCP_", "OTEL_", "AGENT_", "API_KEY")
        ) or name in {"APP_ENV", "LOG_LEVEL", "LOG_LLM_CONTENT", "LOG_LLM_CONTENT_MAX_CHARS"}:
            monkeypatch.delenv(name)


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **{**_BASE, **overrides})  # type: ignore[call-arg]


def test_single_provider_becomes_the_default():
    # Act
    settings = _settings()

    # Assert
    assert settings.enabled_providers == ("ollama",)
    assert settings.default_provider == "ollama"


def test_all_providers_can_be_enabled_together():
    # Act
    settings = _settings(
        gemini_api_key="g-key",
        gemini_model="g-model",
        anthropic_api_key="a-key",
        anthropic_model="a-model",
        llm_default_provider="anthropic",
    )

    # Assert
    assert settings.enabled_providers == ("ollama", "gemini", "anthropic")
    assert settings.default_provider == "anthropic"


def test_a_default_is_required_when_several_providers_are_enabled():
    with pytest.raises(ValidationError, match="LLM_DEFAULT_PROVIDER is required"):
        _settings(gemini_api_key="k", gemini_model="m")


def test_the_default_must_be_an_enabled_provider():
    with pytest.raises(ValidationError, match="is not enabled"):
        _settings(llm_default_provider="gemini")


def test_at_least_one_provider_is_required():
    with pytest.raises(ValidationError, match="No LLM provider"):
        Settings(_env_file=None, mcp_server_url="http://mcp.test/mcp")  # type: ignore[call-arg]


def test_a_half_configured_provider_is_an_error_not_a_silent_skip():
    with pytest.raises(ValidationError, match="partially configured"):
        _settings(anthropic_api_key="a-key")


def test_the_mcp_server_url_is_required():
    with pytest.raises(ValidationError, match="mcp_server_url"):
        Settings(_env_file=None, ollama_base_url="http://o.test", ollama_model="m")  # type: ignore[call-arg]


def test_fallbacks_must_reference_enabled_providers():
    with pytest.raises(ValidationError, match="not enabled"):
        _settings(llm_fallbacks={"ollama": ["gemini"]})


def test_fallbacks_must_not_include_the_provider_itself():
    with pytest.raises(ValidationError, match="must not include itself"):
        _settings(llm_fallbacks={"ollama": ["ollama"]})


def test_valid_fallbacks_are_accepted():
    settings = _settings(
        gemini_api_key="k",
        gemini_model="m",
        llm_default_provider="gemini",
        llm_fallbacks={"gemini": ["ollama"]},
    )
    assert settings.llm_fallbacks == {"gemini": ["ollama"]}


def test_retry_delays_must_be_ordered():
    with pytest.raises(ValidationError, match="LLM_RETRY_MAX_DELAY_SECONDS"):
        _settings(llm_retry_base_delay_seconds=5, llm_retry_max_delay_seconds=1)


def test_production_requires_an_api_key():
    with pytest.raises(ValidationError, match="API_KEY is required"):
        _settings(app_env="production")
    assert _settings(app_env="production", api_key="k").app_env == "production"


def test_conversation_content_logging_is_off_by_default_and_refused_in_production():
    assert _settings().log_llm_content is False
    assert _settings(log_llm_content=True).log_llm_content is True
    with pytest.raises(ValidationError, match="LOG_LLM_CONTENT must not be enabled"):
        _settings(app_env="production", api_key="k", log_llm_content=True)


def test_telemetry_requires_an_endpoint_when_enabled():
    with pytest.raises(ValidationError, match="OTEL_EXPORTER_ENDPOINT"):
        _settings(otel_enabled=True)


def test_secrets_never_appear_in_repr():
    # Act
    settings = _settings(
        gemini_api_key="super-secret-g",
        gemini_model="m",
        anthropic_api_key="super-secret-a",
        anthropic_model="m",
        api_key="super-secret-inbound",
        llm_default_provider="ollama",
    )

    # Assert
    text = repr(settings) + str(settings)
    assert "super-secret" not in text
    assert settings.gemini_api_key is not None
    assert settings.gemini_api_key.get_secret_value() == "super-secret-g"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("a,b", ["a", "b"]), (" a , ,b ", ["a", "b"]), ("", None), (",", None)],
)
def test_tool_allowlist_accepts_comma_separated_values(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[str] | None
):
    monkeypatch.setenv("AGENT_TOOL_ALLOWLIST", raw)
    assert _settings().agent_tool_allowlist == expected


def test_values_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    # Arrange
    monkeypatch.setenv("MCP_SERVER_URL", "http://env-mcp:3000/mcp")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://env-ollama:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "env-model")
    monkeypatch.setenv("AGENT_MAX_STEPS", "3")

    # Act
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    # Assert
    assert str(settings.mcp_server_url) == "http://env-mcp:3000/mcp"
    assert settings.ollama_model == "env-model"
    assert settings.agent_max_steps == 3


def test_empty_environment_variables_count_as_unset(monkeypatch: pytest.MonkeyPatch):
    # Arrange: `GEMINI_API_KEY=` is how .env.example documents a disabled provider
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GEMINI_MODEL", "")

    # Act
    settings = _settings()

    # Assert
    assert settings.enabled_providers == ("ollama",)


@pytest.mark.parametrize(
    "override",
    [{"agent_max_steps": 0}, {"http_port": 70000}, {"llm_max_attempts": 0}, {"llm_temperature": 5}],
)
def test_numeric_limits_are_enforced(override: dict[str, Any]):
    with pytest.raises(ValidationError):
        _settings(**override)
