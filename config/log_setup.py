"""Logging profiles from ``config/logging.json``, with OpenTelemetry trace correlation.

``APP_ENV`` selects a profile: readable console lines in development and test, one JSON
object per line in production. Every profile writes to stdout only: container platforms
collect and ship stdout, so the application never manages log files itself.
"""

from __future__ import annotations

import copy
import json
import logging
import logging.config
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Final, cast

from opentelemetry import trace

_CONFIG_FILE: Final = "logging.json"
_REDACTED: Final = "[REDACTED]"
# Exact key names, after lower-casing and turning "-" into "_". Matching substrings instead
# would also hide harmless fields such as ``input_tokens``.
_SENSITIVE_KEYS: Final = frozenset(
    {
        "api_key",
        "apikey",
        "x_api_key",
        "authorization",
        "proxy_authorization",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "bearer",
        "cookie",
        "set_cookie",
    }
)

# Attributes every LogRecord has; anything else was added through ``extra=``.
# ``color_message`` is uvicorn's copy of the message with ANSI colour codes: pure noise here.
_BUILTIN_RECORD_ATTRS: Final = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime", "taskName", "trace_id", "span_id", "color_message"}


class TraceContextFilter(logging.Filter):
    """Copy the active span's ids onto the record so logs can be joined with traces."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = trace.get_current_span().get_span_context()
        valid = context.is_valid
        record.trace_id = f"{context.trace_id:032x}" if valid else None
        record.span_id = f"{context.span_id:016x}" if valid else None
        return True


class JsonFormatter(logging.Formatter):
    """Format a record as one JSON line, including ``extra=`` fields and the traceback."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", None),
            "span_id": getattr(record, "span_id", None),
        }
        for key, value in _extras(record).items():
            payload.setdefault(key, value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """A readable line: the configured format, then ``extra=`` fields as ``key=value``."""

    def formatMessage(self, record: logging.LogRecord) -> str:
        line = super().formatMessage(record)
        fields = " ".join(
            f"{key}={_console_value(value)}" for key, value in _extras(record).items()
        )
        return f"{line} {fields}" if fields else line


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    """The fields passed through ``extra=``, with sensitive ones redacted."""
    return {
        key: _REDACTED if _is_sensitive(key) else value
        for key, value in record.__dict__.items()
        if key not in _BUILTIN_RECORD_ATTRS
    }


def _console_value(value: Any) -> str:
    # Quote anything with spaces or line breaks so one log record stays one readable line.
    if isinstance(value, str) and value and not any(c.isspace() for c in value):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _is_sensitive(key: str) -> bool:
    return key.lower().replace("-", "_") in _SENSITIVE_KEYS


def load_logging_config(app_env: str, config_path: Path | None = None) -> dict[str, Any]:
    """Return the ``dictConfig`` for ``app_env``: the file's ``base`` merged with that profile.

    The default file is read as package data, so it works the same from a source checkout
    and from an installed wheel (the Docker image).
    """
    try:
        if config_path is None:
            raw = resources.files("config").joinpath(_CONFIG_FILE).read_text(encoding="utf-8")
        else:
            raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as err:
        raise ValueError(f"Logging configuration file not found: {_CONFIG_FILE}") from err
    try:
        document: Any = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ValueError(f"Logging configuration is not valid JSON: {err.msg}") from err

    if not isinstance(document, dict):
        raise ValueError("Logging configuration must be a JSON object")
    document = cast(dict[str, Any], document)
    base = document.get("base")
    profiles = document.get("profiles")
    if not isinstance(base, dict) or not isinstance(profiles, dict):
        raise ValueError("Logging configuration needs 'base' and 'profiles' objects")
    profiles = cast(dict[str, Any], profiles)
    profile = profiles.get(app_env)
    if not isinstance(profile, dict):
        known = ", ".join(sorted(profiles))
        raise ValueError(f"Unknown logging profile '{app_env}' (known: {known})")
    return _deep_merge(cast(dict[str, Any], base), cast(dict[str, Any], profile))


def configure_logging(
    app_env: str, level: str | None = None, *, config_path: Path | None = None
) -> None:
    """Apply the logging profile for ``app_env``; ``level`` overrides the root level.

    ``dictConfig`` replaces the root handlers, so calling this again is safe.
    """
    config = load_logging_config(app_env, config_path)
    if level is not None:
        config.setdefault("root", {})["level"] = level
    try:
        logging.config.dictConfig(config)
    except (ValueError, TypeError, AttributeError, ImportError) as err:
        raise ValueError(f"Logging profile '{app_env}' is invalid") from err


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge ``override`` into a copy of ``base``; nested objects merge, other values replace."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(cast(dict[str, Any], current), cast(dict[str, Any], value))
        else:
            merged[key] = copy.deepcopy(value)
    return merged
