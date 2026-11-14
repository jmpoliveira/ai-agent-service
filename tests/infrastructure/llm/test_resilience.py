from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.domain.errors import (
    LLMAuthError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from src.domain.llm import ChatResult
from src.domain.messages import Message
from src.infrastructure.llm.resilience import (
    FallbackChatModel,
    RateLimitedChatModel,
    RetryingChatModel,
    RetryPolicy,
)
from tests.fakes import ScriptedChatModel, reply

MESSAGES = [Message.user("hi")]


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _retrying(
    script: list[ChatResult | BaseException], policy: RetryPolicy | None = None
) -> tuple[RetryingChatModel, ScriptedChatModel, SleepRecorder]:
    inner = ScriptedChatModel(script)
    sleeper = SleepRecorder()
    model = RetryingChatModel(
        inner,
        policy or RetryPolicy(max_attempts=3, base_delay_seconds=1.0, max_delay_seconds=3.0),
        sleep=sleeper,
        jitter=lambda: 1.0,  # full delay, which makes the arithmetic predictable
    )
    return model, inner, sleeper


# --------------------------------------------------------------------------- retrying


async def test_retries_a_transient_failure_and_then_succeeds():
    # Arrange
    model, inner, sleeper = _retrying([LLMUnavailableError("down"), reply("ok")])

    # Act
    result = await model.chat(MESSAGES, [])

    # Assert
    assert result.message.content == "ok"
    assert len(inner.calls) == 2
    assert sleeper.delays == [1.0]


async def test_backoff_doubles_and_is_capped():
    # Arrange
    policy = RetryPolicy(max_attempts=4, base_delay_seconds=1.0, max_delay_seconds=3.0)
    failures = [LLMUnavailableError("down")] * 3
    model, _, sleeper = _retrying([*failures, reply("ok")], policy)

    # Act
    await model.chat(MESSAGES, [])

    # Assert: 1s, 2s, then capped at 3s (instead of 4s)
    assert sleeper.delays == [1.0, 2.0, 3.0]


async def test_gives_up_after_the_last_attempt_and_raises_the_last_error():
    # Arrange
    errors = [LLMUnavailableError(f"down {i}") for i in range(3)]
    model, inner, sleeper = _retrying(list(errors))

    # Act / Assert
    with pytest.raises(LLMUnavailableError, match="down 2"):
        await model.chat(MESSAGES, [])
    assert len(inner.calls) == 3
    assert len(sleeper.delays) == 2


async def test_non_retryable_errors_are_raised_immediately():
    # Arrange
    model, inner, sleeper = _retrying([LLMAuthError("bad key")])

    # Act / Assert
    with pytest.raises(LLMAuthError):
        await model.chat(MESSAGES, [])
    assert len(inner.calls) == 1
    assert sleeper.delays == []


async def test_a_short_retry_after_hint_raises_the_wait():
    # Arrange
    limited = LLMRateLimitError("slow", retry_after_seconds=2.5)
    model, _, sleeper = _retrying([limited, reply("ok")])

    # Act
    await model.chat(MESSAGES, [])

    # Assert: base delay is 1s, but the provider asked for 2.5s
    assert sleeper.delays == [2.5]


async def test_a_retry_after_hint_longer_than_we_would_wait_is_not_retried():
    # Arrange
    limited = LLMRateLimitError("slow", retry_after_seconds=60)
    model, inner, sleeper = _retrying([limited])

    # Act / Assert
    with pytest.raises(LLMRateLimitError):
        await model.chat(MESSAGES, [])
    assert len(inner.calls) == 1
    assert sleeper.delays == []


async def test_jitter_scales_the_delay_between_half_and_full():
    # Arrange
    inner = ScriptedChatModel([LLMUnavailableError("down"), reply("ok")])
    sleeper = SleepRecorder()
    model = RetryingChatModel(inner, RetryPolicy(2, 2.0, 8.0), sleep=sleeper, jitter=lambda: 0.0)

    # Act
    await model.chat(MESSAGES, [])

    # Assert: ceiling 2s, jitter 0 -> half of it
    assert sleeper.delays == [1.0]


def test_retrying_model_exposes_the_wrapped_identity():
    model, _, _ = _retrying([])
    assert (model.provider, model.model) == ("fake", "fake-model")


@pytest.mark.parametrize(
    ("attempts", "base", "maximum"),
    [(0, 1.0, 2.0), (1, -1.0, 2.0), (1, 3.0, 2.0)],
)
def test_retry_policy_rejects_invalid_values(attempts: int, base: float, maximum: float):
    with pytest.raises(ValueError, match=r"max_attempts|Delays"):
        RetryPolicy(attempts, base, maximum)


# --------------------------------------------------------------------------- fallback


async def test_uses_the_primary_when_it_works():
    # Arrange
    primary = ScriptedChatModel([reply("primary")], provider="gemini")
    backup = ScriptedChatModel([reply("backup")], provider="ollama")

    # Act
    result = await FallbackChatModel(primary, [backup]).chat(MESSAGES, [])

    # Assert
    assert result.message.content == "primary"
    assert backup.calls == []


async def test_falls_back_when_the_primary_is_unavailable(spans: InMemorySpanExporter):
    # Arrange
    primary = ScriptedChatModel([LLMUnavailableError("down")], provider="gemini")
    backup = ScriptedChatModel([reply("backup", provider="ollama")], provider="ollama")
    model = FallbackChatModel(primary, [backup])
    tracer = trace.get_tracer("test")

    # Act
    with tracer.start_as_current_span("parent"):
        result = await model.chat(MESSAGES, [])

    # Assert
    assert (result.message.content, result.provider) == ("backup", "ollama")
    events = [e for s in spans.get_finished_spans() for e in s.events]
    assert [e.name for e in events] == ["llm.fallback"]
    assert events[0].attributes == {"from_provider": "gemini", "to_provider": "ollama"}


async def test_walks_the_whole_chain_and_raises_the_last_error():
    # Arrange
    first = ScriptedChatModel([LLMRateLimitError("limit")], provider="a")
    second = ScriptedChatModel([LLMUnavailableError("down")], provider="b")
    third = ScriptedChatModel([LLMUnavailableError("also down")], provider="c")

    # Act / Assert
    with pytest.raises(LLMUnavailableError, match="also down"):
        await FallbackChatModel(first, [second, third]).chat(MESSAGES, [])


async def test_does_not_fall_back_on_configuration_errors():
    # Arrange: hiding a bad API key behind a fallback would mask a real problem
    primary = ScriptedChatModel([LLMAuthError("bad key")], provider="gemini")
    backup = ScriptedChatModel([reply("backup")], provider="ollama")

    # Act / Assert
    with pytest.raises(LLMAuthError):
        await FallbackChatModel(primary, [backup]).chat(MESSAGES, [])
    assert backup.calls == []


def test_fallback_model_identifies_as_the_primary():
    primary = ScriptedChatModel([], provider="gemini", model="g1")
    model = FallbackChatModel(primary, [ScriptedChatModel([], provider="ollama")])
    assert (model.provider, model.model) == ("gemini", "g1")


class FakeTime:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _limited(limit: int, replies: int, fake: FakeTime) -> RateLimitedChatModel:
    inner = ScriptedChatModel([reply(f"r{i}") for i in range(replies)])
    return RateLimitedChatModel(inner, limit, clock=fake.clock, sleep=fake.sleep)


async def test_requests_under_the_limit_do_not_wait():
    fake = FakeTime()
    model = _limited(3, 3, fake)

    for _ in range(3):
        await model.chat(MESSAGES, [])

    assert fake.sleeps == []


async def test_the_request_over_the_limit_waits_until_the_oldest_leaves_the_window():
    # Arrange: 2 per minute; the first two go at t=0 and t=10.
    fake = FakeTime()
    model = _limited(2, 3, fake)
    await model.chat(MESSAGES, [])
    fake.now = 10.0
    await model.chat(MESSAGES, [])
    fake.now = 20.0

    # Act
    result = await model.chat(MESSAGES, [])

    # Assert: it waits until t=60, when the t=0 request is a minute old.
    assert fake.sleeps == [40.0]
    assert result.message.content == "r2"


async def test_old_requests_leave_the_window():
    fake = FakeTime()
    model = _limited(1, 2, fake)
    await model.chat(MESSAGES, [])
    fake.now = 61.0

    await model.chat(MESSAGES, [])

    assert fake.sleeps == []


async def test_rate_limiter_exposes_the_inner_identity_and_validates_the_limit():
    inner = ScriptedChatModel([])

    model = RateLimitedChatModel(inner, 5)

    assert (model.provider, model.model) == (inner.provider, inner.model)
    with pytest.raises(ValueError, match="at least 1"):
        RateLimitedChatModel(inner, 0)
