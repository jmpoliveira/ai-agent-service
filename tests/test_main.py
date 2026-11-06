"""Composition-root tests: wiring only, no network."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from config.settings import Settings
from main import build_chat_models, build_health_checks, build_run_agent, create_app
from src.infrastructure.llm.resilience import FallbackChatModel, RetryingChatModel


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "mcp_server_url": "http://mcp.test:3000/mcp",
        "mcp_health_url": "http://mcp.test:3000/health",
        "ollama_base_url": "http://ollama.test:11434",
        "ollama_model": "local-model",
        "gemini_api_key": "g-key",
        "gemini_model": "g-model",
        "anthropic_api_key": "a-key",
        "anthropic_model": "a-model",
        "llm_default_provider": "ollama",
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[call-arg]


@pytest.fixture
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_every_enabled_provider_gets_a_retrying_model():
    # Act
    models = build_chat_models(_settings())

    # Assert
    assert set(models) == {"ollama", "gemini", "anthropic"}
    assert all(isinstance(m, RetryingChatModel) for m in models.values())
    assert models["anthropic"].model == "a-model"


def test_only_configured_providers_are_built():
    models = build_chat_models(
        _settings(
            gemini_api_key=None, gemini_model=None, anthropic_api_key=None, anthropic_model=None
        )
    )
    assert set(models) == {"ollama"}


def test_fallback_chains_wrap_the_primary_only():
    # Act
    models = build_chat_models(_settings(llm_fallbacks={"gemini": ["ollama", "anthropic"]}))

    # Assert
    assert isinstance(models["gemini"], FallbackChatModel)
    assert isinstance(models["ollama"], RetryingChatModel)
    assert models["gemini"].provider == "gemini"


def test_run_agent_exposes_the_configured_providers():
    assert build_run_agent(_settings()).providers == ("ollama", "gemini", "anthropic")


def test_health_checks_cover_ollama_and_the_mcp_server():
    assert [c.name for c in build_health_checks(_settings())] == ["ollama", "mcp"]


def test_health_checks_skip_dependencies_that_are_not_configured():
    settings = _settings(
        mcp_health_url=None,
        ollama_base_url=None,
        ollama_model=None,
        llm_default_provider="gemini",
    )
    assert build_health_checks(settings) == []


@pytest.mark.usefixtures("_restore_logging")
def test_created_app_serves_liveness_and_requires_the_key_for_runs():
    # Arrange
    app = create_app(_settings(api_key="s3cret"))

    # Act
    with TestClient(app) as client:
        live = client.get("/health/live")
        denied = client.post("/v1/agent/runs", json={"prompt": "hi"})

    # Assert
    assert live.status_code == 200
    assert denied.status_code == 401


def test_conversation_logging_is_wired_only_when_enabled():
    enabled = build_run_agent(_settings(log_llm_content=True))
    disabled = build_run_agent(_settings())

    assert enabled._conversation_log is not None  # pyright: ignore[reportPrivateUsage]
    assert disabled._conversation_log is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.usefixtures("_restore_logging")
def test_startup_warns_when_conversation_content_is_logged(capsys: pytest.CaptureFixture[str]):
    create_app(_settings(log_llm_content=True))

    assert "LOG_LLM_CONTENT is enabled" in capsys.readouterr().out


@pytest.mark.usefixtures("_restore_logging")
def test_documentation_is_hidden_in_production():
    app = create_app(_settings(app_env="production", api_key="s3cret"))

    with TestClient(app) as client:
        assert client.get("/docs").status_code == 404
