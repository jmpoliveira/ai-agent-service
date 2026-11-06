"""Composition root: the only module that knows every layer and wires them together.

Run with ``python main.py`` (host and port come from the settings) or point an ASGI
server at the factory: ``uvicorn main:create_app --factory``.
"""

from __future__ import annotations

import logging

import uvicorn
from fastapi import FastAPI

from config.log_setup import configure_logging
from config.settings import Settings, get_settings
from config.telemetry import setup_telemetry
from src.application.catalog import ModelCatalog
from src.application.conversation_log import ConversationLogger
from src.application.ports import ChatModel, HealthCheck
from src.application.run_agent import AgentConfig, RunAgent
from src.domain.agent import AgentBudget
from src.infrastructure.health.http_check import HttpHealthCheck
from src.infrastructure.llm.claude_chat import create_claude_chat_model
from src.infrastructure.llm.gemini_chat import create_gemini_chat_model
from src.infrastructure.llm.ollama_chat import create_ollama_chat_model
from src.infrastructure.llm.resilience import FallbackChatModel, RetryingChatModel, RetryPolicy
from src.infrastructure.mcp.client import McpToolGatewayProvider
from src.interface.api.app import create_api

logger = logging.getLogger(__name__)

_OLLAMA_HEALTH_PATH = "/api/tags"  # part of the Ollama HTTP API, not configuration


def build_chat_models(settings: Settings) -> dict[str, ChatModel]:
    """Create one resilient chat model per enabled provider, then apply the fallback chains."""
    raw: dict[str, ChatModel] = {}
    timeout = settings.llm_request_timeout_seconds
    max_tokens = settings.llm_max_output_tokens
    temperature = settings.llm_temperature

    if settings.ollama_base_url and settings.ollama_model:
        raw["ollama"] = create_ollama_chat_model(
            base_url=str(settings.ollama_base_url),
            model=settings.ollama_model,
            reasoning=settings.ollama_reasoning,
            timeout_seconds=timeout,
            max_output_tokens=max_tokens,
            temperature=temperature,
        )
    if settings.gemini_api_key and settings.gemini_model:
        raw["gemini"] = create_gemini_chat_model(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            timeout_seconds=timeout,
            max_output_tokens=max_tokens,
            temperature=temperature,
        )
    if settings.anthropic_api_key and settings.anthropic_model:
        raw["anthropic"] = create_claude_chat_model(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            timeout_seconds=timeout,
            max_output_tokens=max_tokens,
            temperature=temperature,
        )

    policy = RetryPolicy(
        max_attempts=settings.llm_max_attempts,
        base_delay_seconds=settings.llm_retry_base_delay_seconds,
        max_delay_seconds=settings.llm_retry_max_delay_seconds,
    )
    resilient: dict[str, ChatModel] = {
        alias: RetryingChatModel(model, policy) for alias, model in raw.items()
    }

    models: dict[str, ChatModel] = dict(resilient)
    for primary, fallbacks in settings.llm_fallbacks.items():
        models[primary] = FallbackChatModel(
            resilient[primary], [resilient[name] for name in fallbacks]
        )
    return models


def build_run_agent(settings: Settings) -> RunAgent:
    catalog = ModelCatalog(build_chat_models(settings), default=settings.default_provider)
    tools = McpToolGatewayProvider(
        str(settings.mcp_server_url),
        connect_timeout_seconds=settings.mcp_connect_timeout_seconds,
        tool_timeout_seconds=settings.mcp_tool_timeout_seconds,
    )
    allowlist = settings.agent_tool_allowlist
    config = AgentConfig(
        name=settings.agent_name,
        system_prompt=settings.agent_system_prompt,
        max_prompt_length=settings.agent_max_prompt_length,
        max_tool_output_chars=settings.agent_max_tool_output_chars,
        budget=AgentBudget(
            max_steps=settings.agent_max_steps,
            max_total_tokens=settings.agent_max_total_tokens,
            timeout_seconds=settings.agent_run_timeout_seconds,
            max_identical_tool_calls=settings.agent_max_identical_tool_calls,
        ),
        tool_allowlist=frozenset(allowlist) if allowlist else None,
    )
    conversation_log = (
        ConversationLogger(settings.log_llm_content_max_chars) if settings.log_llm_content else None
    )
    return RunAgent(catalog, tools, config, conversation_log)


def build_health_checks(settings: Settings) -> list[HealthCheck]:
    timeout = settings.health_check_timeout_seconds
    checks: list[HealthCheck] = []
    if settings.ollama_base_url and settings.ollama_model:
        url = str(settings.ollama_base_url).rstrip("/") + _OLLAMA_HEALTH_PATH
        checks.append(HttpHealthCheck("ollama", url, timeout))
    if settings.mcp_health_url:
        checks.append(HttpHealthCheck("mcp", str(settings.mcp_health_url), timeout))
    return checks


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory. Pass ``settings`` explicitly in tests."""
    settings = settings or get_settings()
    configure_logging(settings.app_env, settings.log_level)
    if settings.log_llm_content:
        logger.warning(
            "LOG_LLM_CONTENT is enabled: prompts, model replies and tool results are logged"
        )
    shutdown_telemetry = setup_telemetry(settings)
    return create_api(
        title=settings.app_name,
        run_agent=build_run_agent(settings),
        health_checks=build_health_checks(settings),
        api_key=settings.api_key,
        on_shutdown=shutdown_telemetry,
        expose_docs=settings.app_env != "production",
    )


def main() -> None:
    settings = get_settings()
    # log_config=None keeps our logging profile: uvicorn's loggers propagate to the root logger.
    uvicorn.run(
        create_app(settings), host=settings.http_host, port=settings.http_port, log_config=None
    )


if __name__ == "__main__":
    main()
