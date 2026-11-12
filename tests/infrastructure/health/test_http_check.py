from __future__ import annotations

import httpx

from src.infrastructure.health.http_check import HttpHealthCheck


def _check(handler: httpx.MockTransport) -> HttpHealthCheck:
    return HttpHealthCheck("ollama", "http://secret-host.internal/api/tags", 1.0, transport=handler)


async def test_healthy_on_2xx():
    # Arrange
    check = _check(httpx.MockTransport(lambda _: httpx.Response(200, json={})))

    # Act
    status = await check.check()

    # Assert
    assert (status.name, status.healthy, status.detail) == ("ollama", True, None)


async def test_unhealthy_on_error_status_without_leaking_the_url():
    # Arrange
    check = _check(httpx.MockTransport(lambda _: httpx.Response(503)))

    # Act
    status = await check.check()

    # Assert
    assert status.healthy is False
    assert status.detail == "unexpected status 503"
    assert "secret-host" not in (status.detail or "")


async def test_unhealthy_on_timeout():
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    status = await _check(httpx.MockTransport(slow)).check()

    assert (status.healthy, status.detail) == (False, "timeout")


async def test_unhealthy_when_unreachable():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    status = await _check(httpx.MockTransport(refuse)).check()

    assert (status.healthy, status.detail) == (False, "unreachable")


def test_check_exposes_its_name():
    assert _check(httpx.MockTransport(lambda _: httpx.Response(200))).name == "ollama"
