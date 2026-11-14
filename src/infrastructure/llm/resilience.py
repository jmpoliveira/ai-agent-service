"""Decorators that add resilience to any ``ChatModel``.

Retry, fallback and rate limiting live here, not inside the provider adapters, so each policy
is written and tested once. Only errors that can heal themselves are retried: an
authentication or validation error would fail again, so it is raised immediately.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from opentelemetry import trace

from src.application.ports import ChatModel
from src.domain.errors import LLMRateLimitError, LLMUnavailableError
from src.domain.llm import ChatResult
from src.domain.messages import Message
from src.domain.tools import ToolSpec

logger = logging.getLogger(__name__)

_RETRYABLE = (LLMUnavailableError, LLMRateLimitError)
_SYSTEM_RANDOM = random.SystemRandom()


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("Delays must satisfy 0 <= base <= max")


class RetryingChatModel:
    """Retries transient failures with exponential backoff and jitter."""

    def __init__(
        self,
        inner: ChatModel,
        policy: RetryPolicy,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = _SYSTEM_RANDOM.random,
    ) -> None:
        self._inner = inner
        self._policy = policy
        self._sleep = sleep
        self._jitter = jitter

    @property
    def provider(self) -> str:
        return self._inner.provider

    @property
    def model(self) -> str:
        return self._inner.model

    async def chat(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ChatResult:
        attempt = 1
        while True:
            try:
                return await self._inner.chat(messages, tools)
            except _RETRYABLE as err:
                delay = self._next_delay(attempt, err)
                if delay is None:
                    raise
                logger.warning(
                    "Retrying LLM call",
                    extra={
                        "provider": self.provider,
                        "attempt": attempt,
                        "max_attempts": self._policy.max_attempts,
                        "delay_seconds": round(delay, 3),
                        "error_type": type(err).__name__,
                    },
                )
                await self._sleep(delay)
                attempt += 1

    def _next_delay(
        self, attempt: int, err: LLMUnavailableError | LLMRateLimitError
    ) -> float | None:
        """Seconds to wait before the next attempt, or None when we should give up."""
        policy = self._policy
        if attempt >= policy.max_attempts:
            return None

        retry_after = err.retry_after_seconds if isinstance(err, LLMRateLimitError) else None
        if retry_after is not None and retry_after > policy.max_delay_seconds:
            # The provider asks for a longer pause than we are willing to wait: give up so
            # that a fallback (if any) can take over instead of stalling the request.
            return None

        ceiling = min(policy.max_delay_seconds, policy.base_delay_seconds * 2 ** (attempt - 1))
        # "Equal jitter": always wait at least half the backoff, plus a random share of the other
        # half, so many clients that failed together do not all retry at the same instant.
        delay = ceiling / 2 + self._jitter() * ceiling / 2
        # Never retry sooner than the provider asked us to.
        return max(delay, retry_after or 0.0)


class FallbackChatModel:
    """Tries the primary, then each fallback in order, when a provider is down or throttled."""

    def __init__(self, primary: ChatModel, fallbacks: Sequence[ChatModel]) -> None:
        self._primary = primary
        self._candidates: tuple[ChatModel, ...] = (primary, *fallbacks)

    @property
    def provider(self) -> str:
        return self._primary.provider

    @property
    def model(self) -> str:
        return self._primary.model

    async def chat(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ChatResult:
        for index, candidate in enumerate(self._candidates[:-1]):
            try:
                return await candidate.chat(messages, tools)
            except _RETRYABLE as err:
                following = self._candidates[index + 1]
                logger.warning(
                    "Falling back to another LLM provider",
                    extra={
                        "from_provider": candidate.provider,
                        "to_provider": following.provider,
                        "error_type": type(err).__name__,
                    },
                )
                trace.get_current_span().add_event(
                    "llm.fallback",
                    {"from_provider": candidate.provider, "to_provider": following.provider},
                )
        return await self._candidates[-1].chat(messages, tools)


class RateLimitedChatModel:
    """Keeps requests under a provider quota by waiting, instead of failing with 429.

    A sliding window of the last minute: when it already holds ``max_requests_per_minute``
    requests, the next one waits until the oldest leaves the window. Waiting is better than
    retrying here: a rate-limit error would usually come with a Retry-After longer than the
    retry policy accepts, so the request would fail anyway.

    The window is per instance, so it only protects requests that share this object. That is
    enough for one process (the evals, or one service replica); several replicas share a
    provider quota and would need a shared limiter.
    """

    _WINDOW_SECONDS = 60.0

    def __init__(
        self,
        inner: ChatModel,
        max_requests_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_requests_per_minute < 1:
            raise ValueError("max_requests_per_minute must be at least 1")
        self._inner = inner
        self._limit = max_requests_per_minute
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque()
        # Serialises the bookkeeping, so concurrent callers cannot both take the last slot.
        self._lock = asyncio.Lock()

    @property
    def provider(self) -> str:
        return self._inner.provider

    @property
    def model(self) -> str:
        return self._inner.model

    async def chat(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ChatResult:
        async with self._lock:
            await self._wait_for_slot()
            self._sent.append(self._clock())
        return await self._inner.chat(messages, tools)

    async def _wait_for_slot(self) -> None:
        while True:
            now = self._clock()
            while self._sent and now - self._sent[0] >= self._WINDOW_SECONDS:
                self._sent.popleft()
            if len(self._sent) < self._limit:
                return
            wait = self._WINDOW_SECONDS - (now - self._sent[0])
            logger.info(
                "Waiting for the request rate limit",
                extra={"provider": self.provider, "wait_seconds": round(wait, 1)},
            )
            await self._sleep(wait)
