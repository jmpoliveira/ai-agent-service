"""Shared fixtures. One in-memory tracer provider serves the whole test session."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_exporter = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_exporter))
trace.set_tracer_provider(_provider)


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """The exporter, emptied before each test."""
    _exporter.clear()
    yield _exporter
    _exporter.clear()


def span_by_name(exporter: InMemorySpanExporter, name: str) -> ReadableSpan:
    matches = [s for s in exporter.get_finished_spans() if s.name == name]
    assert len(matches) == 1, f"expected one span named {name!r}, found {len(matches)}"
    return matches[0]
