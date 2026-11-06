from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

import config.telemetry as telemetry
from config.settings import Settings


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "mcp_server_url": "http://mcp.test/mcp",
        "ollama_base_url": "http://ollama.test",
        "ollama_model": "m",
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry, "_shutdown", None)


def test_disabled_telemetry_returns_a_harmless_shutdown():
    # Act
    shutdown = telemetry.setup_telemetry(_settings(otel_enabled=False))

    # Assert
    shutdown()  # must not raise


def test_enabled_telemetry_exports_to_the_otlp_http_traces_path(monkeypatch: pytest.MonkeyPatch):
    # Arrange: replace everything that would touch the global provider or the network
    exporter_class = MagicMock(name="OTLPSpanExporter")
    processor_class = MagicMock(name="BatchSpanProcessor")
    set_provider = MagicMock(name="set_tracer_provider")
    monkeypatch.setattr(telemetry, "OTLPSpanExporter", exporter_class)
    monkeypatch.setattr(telemetry, "BatchSpanProcessor", processor_class)
    monkeypatch.setattr(telemetry.trace, "set_tracer_provider", set_provider)
    settings = _settings(otel_enabled=True, otel_exporter_endpoint="http://jaeger:4318/")

    # Act
    shutdown = telemetry.setup_telemetry(settings)

    # Assert
    exporter_class.assert_called_once_with(endpoint="http://jaeger:4318/v1/traces")
    set_provider.assert_called_once()
    resource = set_provider.call_args.args[0].resource
    assert resource.attributes["service.name"] == "ai-agent-service"
    assert callable(shutdown)


def test_second_call_reuses_the_existing_provider(monkeypatch: pytest.MonkeyPatch):
    # Arrange
    set_provider = MagicMock()
    monkeypatch.setattr(telemetry, "OTLPSpanExporter", MagicMock())
    monkeypatch.setattr(telemetry, "BatchSpanProcessor", MagicMock())
    monkeypatch.setattr(telemetry.trace, "set_tracer_provider", set_provider)
    settings = _settings(otel_enabled=True, otel_exporter_endpoint="http://jaeger:4318")

    # Act
    first = telemetry.setup_telemetry(settings)
    second = telemetry.setup_telemetry(settings)

    # Assert
    assert first is second
    set_provider.assert_called_once()
