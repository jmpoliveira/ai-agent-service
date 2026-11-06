"""HTTP readiness probe for a dependency."""

from __future__ import annotations

import logging

import httpx

from src.application.ports import DependencyStatus

logger = logging.getLogger(__name__)


class HttpHealthCheck:
    """Healthy when ``GET url`` answers with a 2xx status within the timeout.

    The URL is never included in the result, so a readiness response cannot leak
    internal hostnames.
    """

    def __init__(
        self,
        name: str,
        url: str,
        timeout_seconds: float,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._name = name
        self._url = url
        self._timeout = timeout_seconds
        self._transport = transport

    @property
    def name(self) -> str:
        return self._name

    async def check(self) -> DependencyStatus:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.get(self._url)
                response.raise_for_status()
        except httpx.TimeoutException:
            logger.warning("Readiness probe timed out", extra={"dependency": self._name})
            return DependencyStatus(self._name, healthy=False, detail="timeout")
        except httpx.HTTPStatusError as err:
            logger.warning(
                "Readiness probe got an unexpected status",
                extra={"dependency": self._name, "status": err.response.status_code},
            )
            return DependencyStatus(
                self._name, healthy=False, detail=f"unexpected status {err.response.status_code}"
            )
        except httpx.HTTPError:
            logger.warning("Readiness probe could not connect", extra={"dependency": self._name})
            return DependencyStatus(self._name, healthy=False, detail="unreachable")
        return DependencyStatus(self._name, healthy=True)
