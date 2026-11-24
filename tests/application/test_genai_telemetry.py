from __future__ import annotations

import pytest

from src.application.genai_telemetry import provider_name


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("gemini", "gcp.gemini"),
        ("anthropic", "anthropic"),
        ("ollama", "ollama"),
        ("foundry", "azure.ai.openai"),
        ("unknown-alias", "unknown-alias"),
    ],
)
def test_configuration_aliases_map_to_gen_ai_provider_names(alias: str, expected: str):
    assert provider_name(alias) == expected
