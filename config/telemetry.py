"""OpenTelemetry bootstrap: tracer provider and OTLP/HTTP exporter."""

from __future__ import annotations

import logging
from collections.abc import Callable

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from config.settings import Settings

logger = logging.getLogger(__name__)

_OTLP_TRACES_PATH = "/v1/traces"

# The global TracerProvider can only be set once per process.
_shutdown: Callable[[], None] | None = None


def _noop() -> None:
    return None


def setup_telemetry(settings: Settings) -> Callable[[], None]:
    """Configure tracing. Returns a callable that flushes pending spans on shutdown."""
    global _shutdown  # noqa: PLW0603 - process-wide singleton by design

    if not settings.otel_enabled or settings.otel_exporter_endpoint is None:
        logger.info("OpenTelemetry tracing is disabled")
        return _noop

    if _shutdown is not None:
        return _shutdown

    base = str(settings.otel_exporter_endpoint).rstrip("/")
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.otel_service_name or settings.app_name,
                "deployment.environment": settings.app_env,
            }
        )
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=base + _OTLP_TRACES_PATH))
    )
    trace.set_tracer_provider(provider)
    _shutdown = provider.shutdown
    logger.info("OpenTelemetry tracing enabled", extra={"exporter": base})
    return _shutdown
