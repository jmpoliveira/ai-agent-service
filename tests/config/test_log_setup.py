from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace

from config.log_setup import (
    ConsoleFormatter,
    JsonFormatter,
    TraceContextFilter,
    configure_logging,
    load_logging_config,
)

Captured = tuple[logging.Logger, io.StringIO]


@pytest.fixture
def captured() -> Iterator[Captured]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(TraceContextFilter())
    logger = logging.getLogger("test.json")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    yield logger, stream
    logger.removeHandler(handler)


def _last(stream: io.StringIO) -> dict[str, Any]:
    return json.loads(stream.getvalue().strip().splitlines()[-1])


def test_each_record_is_one_json_object_with_the_core_fields(captured: Captured):
    # Arrange
    logger, stream = captured

    # Act
    logger.info("hello %s", "world")

    # Assert
    entry = _last(stream)
    assert entry["message"] == "hello world"
    assert entry["level"] == "INFO"
    assert entry["logger"] == "test.json"
    assert entry["timestamp"].endswith("+00:00")
    assert entry["trace_id"] is None
    assert entry["span_id"] is None


def test_extra_fields_are_included(captured: Captured):
    logger, stream = captured

    logger.info("run done", extra={"steps": 3, "provider": "ollama"})

    entry = _last(stream)
    assert (entry["steps"], entry["provider"]) == (3, "ollama")


def test_logs_inside_a_span_carry_its_trace_and_span_ids(captured: Captured):
    # Arrange
    logger, stream = captured

    # Act
    with trace.get_tracer("test").start_as_current_span("work") as span:
        logger.info("inside")

    # Assert
    context = span.get_span_context()
    entry = _last(stream)
    assert entry["trace_id"] == f"{context.trace_id:032x}"
    assert entry["span_id"] == f"{context.span_id:016x}"


def test_sensitive_extra_fields_are_redacted(captured: Captured):
    logger, stream = captured

    logger.info("call", extra={"api_key": "abc", "Authorization": "Bearer x", "tool": "search"})

    entry = _last(stream)
    assert entry["api_key"] == "[REDACTED]"
    assert entry["Authorization"] == "[REDACTED]"
    assert entry["tool"] == "search"


@pytest.mark.parametrize("key", ["access_token", "X-API-Key", "client_secret", "Cookie"])
def test_other_secret_field_names_are_redacted(captured: Captured, key: str):
    logger, stream = captured

    logger.info("call", extra={key: "value"})

    assert _last(stream)[key] == "[REDACTED]"


def test_token_counts_are_not_mistaken_for_secrets(captured: Captured):
    # Regression: matching "token" as a substring used to hide these fields.
    logger, stream = captured

    logger.info("done", extra={"input_tokens": 812, "output_tokens": 96, "max_tokens": 1024})

    entry = _last(stream)
    assert (entry["input_tokens"], entry["output_tokens"], entry["max_tokens"]) == (812, 96, 1024)


def _console_line(**extra: Any) -> str:
    formatter = ConsoleFormatter(fmt="%(levelname)s %(message)s")
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "Agent run finished", (), None)
    record.__dict__.update(extra)
    return formatter.format(record)


def test_console_lines_show_extra_fields_as_key_value():
    line = _console_line(provider="ollama", steps=2, input_tokens=812)

    assert line == "INFO Agent run finished provider=ollama steps=2 input_tokens=812"


def test_console_lines_quote_text_with_spaces_or_line_breaks():
    line = _console_line(content="Ada has\ntwo tickets")

    assert line == 'INFO Agent run finished content="Ada has\\ntwo tickets"'
    assert "\n" not in line.replace("\\n", "")


def test_uvicorn_colour_copy_of_the_message_is_left_out():
    assert _console_line(color_message="\x1b[1mrunning\x1b[0m") == "INFO Agent run finished"


def test_console_lines_redact_secrets_and_keep_lines_without_extras_unchanged():
    assert _console_line(api_key="abc") == "INFO Agent run finished api_key=[REDACTED]"
    assert _console_line() == "INFO Agent run finished"


def test_console_lines_still_show_the_traceback():
    formatter = ConsoleFormatter(fmt="%(message)s")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "failed", (), __import__("sys").exc_info()
        )
    record.__dict__["tool"] = "search"

    line = formatter.format(record)

    assert line.startswith("failed tool=search\n")
    assert "RuntimeError: boom" in line


def test_exceptions_are_a_structured_field_not_buried_in_the_message(captured: Captured):
    # Arrange
    logger, stream = captured

    # Act
    try:
        raise ValueError("boom")
    except ValueError:
        logger.error("failed", exc_info=True)

    # Assert
    entry = _last(stream)
    assert entry["message"] == "failed"
    assert "ValueError: boom" in entry["exception"]


def test_non_serialisable_extras_do_not_break_logging(captured: Captured):
    logger, stream = captured

    logger.info("x", extra={"thing": object()})

    assert "object object" in _last(stream)["thing"]


@pytest.fixture
def restore_root_logger() -> Iterator[None]:
    """``dictConfig`` replaces the root handlers; put the test runner's back afterwards."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


def _formatter_of(config: dict[str, Any]) -> str:
    return config["handlers"]["stdout"]["formatter"]


@pytest.mark.parametrize(
    ("app_env", "formatter", "root_level"),
    [
        ("development", "console", "INFO"),
        ("test", "console", "WARNING"),
        ("production", "json", "INFO"),
    ],
)
def test_each_profile_merges_over_the_base(app_env: str, formatter: str, root_level: str):
    # Act
    config = load_logging_config(app_env)

    # Assert
    assert _formatter_of(config) == formatter
    assert config["root"]["level"] == root_level
    assert config["handlers"]["stdout"]["filters"] == ["trace_context"]
    assert config["loggers"]["httpx"]["level"] == "WARNING"


def test_development_profile_raises_only_our_own_loggers_to_debug():
    config = load_logging_config("development")

    assert config["loggers"]["src"]["level"] == "DEBUG"
    assert config["root"]["level"] == "INFO"


def test_merging_a_profile_does_not_change_the_base():
    # Arrange
    load_logging_config("development")

    # Act
    production = load_logging_config("production")

    # Assert
    assert _formatter_of(production) == "json"
    assert "src" not in production["loggers"]


def test_unknown_profile_is_rejected_with_the_known_names():
    with pytest.raises(ValueError, match=r"Unknown logging profile 'staging'.*production"):
        load_logging_config("staging")


def test_invalid_json_is_reported(tmp_path: Path):
    path = tmp_path / "logging.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match="not valid JSON"):
        load_logging_config("production", path)


def test_missing_file_is_reported(tmp_path: Path):
    with pytest.raises(ValueError, match="not found"):
        load_logging_config("production", tmp_path / "missing.json")


@pytest.mark.parametrize("content", ["[]", '{"base": {}}', '{"base": [], "profiles": {}}'])
def test_wrong_structure_is_reported(tmp_path: Path, content: str):
    path = tmp_path / "logging.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="Logging configuration"):
        load_logging_config("production", path)


@pytest.mark.usefixtures("restore_root_logger")
def test_invalid_profile_content_is_reported(tmp_path: Path):
    path = tmp_path / "logging.json"
    path.write_text(
        json.dumps(
            {"base": {"version": 1}, "profiles": {"x": {"handlers": {"h": {"class": "no.Such"}}}}}
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Logging profile 'x' is invalid"):
        configure_logging("x", config_path=path)


@pytest.mark.usefixtures("restore_root_logger")
def test_production_profile_writes_json_lines_to_stdout(capsys: pytest.CaptureFixture[str]):
    # Arrange
    configure_logging("production")

    # Act
    logging.getLogger("src.example").info("ready", extra={"api_key": "s3cret"})

    # Assert
    entry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert entry["message"] == "ready"
    assert entry["api_key"] == "[REDACTED]"


@pytest.mark.usefixtures("restore_root_logger")
def test_development_profile_writes_readable_lines_with_trace_ids(
    capsys: pytest.CaptureFixture[str],
):
    # Arrange
    configure_logging("development")

    # Act
    with trace.get_tracer("test").start_as_current_span("work"):
        logging.getLogger("src.example").debug("step done")

    # Assert
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert "DEBUG" in line
    assert "step done" in line
    assert "trace_id=" in line


@pytest.mark.usefixtures("restore_root_logger")
def test_level_override_wins_over_the_profile_and_reconfiguring_is_safe():
    # Act
    configure_logging("production", "ERROR")
    configure_logging("production", "DEBUG")

    # Assert
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert sum(1 for h in root.handlers if isinstance(h.formatter, JsonFormatter)) == 1
