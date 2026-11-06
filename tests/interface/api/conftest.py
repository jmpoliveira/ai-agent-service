from __future__ import annotations

from collections.abc import Callable, Sequence

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from src.application.catalog import ModelCatalog
from src.application.ports import DependencyStatus
from src.application.run_agent import AgentConfig, RunAgent
from src.domain.agent import AgentBudget
from src.domain.llm import ChatResult
from src.interface.api.app import create_api
from tests.fakes import (
    FakeToolGateway,
    FakeToolGatewayProvider,
    ScriptedChatModel,
    tool_spec,
)

ClientFactory = Callable[..., TestClient]


class StaticHealthCheck:
    def __init__(self, name: str, healthy: bool, detail: str | None = None) -> None:
        self._status = DependencyStatus(name, healthy, detail)

    @property
    def name(self) -> str:
        return self._status.name

    async def check(self) -> DependencyStatus:
        return self._status


@pytest.fixture
def make_client() -> ClientFactory:
    def factory(
        script: Sequence[ChatResult | BaseException] = (),
        *,
        api_key: str | None = None,
        checks: Sequence[StaticHealthCheck] = (),
        expose_docs: bool = True,
        provider: FakeToolGatewayProvider | None = None,
    ) -> TestClient:
        model = ScriptedChatModel(script, provider="fake", model="fake-model")
        config = AgentConfig("api-agent", "SYSTEM", 200, 500, AgentBudget(4, 10_000, 5.0, 2))
        run_agent = RunAgent(
            ModelCatalog({"fake": model}, default="fake"),
            provider or FakeToolGatewayProvider(FakeToolGateway([tool_spec()])),
            config,
        )
        app = create_api(
            title="test",
            run_agent=run_agent,
            health_checks=list(checks),
            api_key=SecretStr(api_key) if api_key else None,
            on_shutdown=lambda: None,
            expose_docs=expose_docs,
        )
        return TestClient(app, raise_server_exceptions=False)

    return factory
