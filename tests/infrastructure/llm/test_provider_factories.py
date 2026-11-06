"""The factories build real LangChain clients (no network happens at construction)."""

from __future__ import annotations

from pydantic import SecretStr

from src.infrastructure.llm.claude_chat import create_claude_chat_model
from src.infrastructure.llm.gemini_chat import create_gemini_chat_model
from src.infrastructure.llm.ollama_chat import create_ollama_chat_model


def test_ollama_factory_builds_a_model_with_the_given_identity():
    # Act
    model = create_ollama_chat_model(
        base_url="http://ollama.test:11434",
        model="tool-capable-model",
        timeout_seconds=5,
        max_output_tokens=64,
        temperature=0.0,
        reasoning=False,
    )

    # Assert
    assert (model.provider, model.model) == ("ollama", "tool-capable-model")


def test_gemini_factory_builds_a_model_with_the_given_identity():
    model = create_gemini_chat_model(
        api_key=SecretStr("test-key"),
        model="gemini-test",
        timeout_seconds=5,
        max_output_tokens=64,
        temperature=0.0,
    )
    assert (model.provider, model.model) == ("gemini", "gemini-test")


def test_claude_factory_builds_a_model_with_the_given_identity():
    model = create_claude_chat_model(
        api_key=SecretStr("test-key"),
        model="claude-test",
        timeout_seconds=5,
        max_output_tokens=64,
        temperature=0.0,
    )
    assert (model.provider, model.model) == ("anthropic", "claude-test")
