from __future__ import annotations

from collections.abc import Callable, Mapping

import pytest

from evals.runner import Agent, EvalAbortedError, EvalRunner, RunRecord
from src.domain.errors import LLMUnavailableError, ToolGatewayUnavailableError
from src.domain.messages import ToolCall
from tests.evals.helpers import case, dataset, make_agent
from tests.fakes import reply

SEARCH = ToolCall("c1", "search_customers", {"query": "alice"})


def _ticking_clock() -> Callable[[], float]:
    ticks = iter(range(1000))
    return lambda: float(next(ticks))


async def test_each_case_runs_repeats_times_with_its_own_stubs():
    # Arrange
    data = dataset(
        case("a", expect={"tools": ["search_customers"], "answer_contains": ["C-001"]}),
        case("b", stub_tools={"search_customers": "x"}, expect={"no_tools": True}),
        stub_tools={"create_ticket": "{}"},
    )
    stubs_seen: list[dict[str, str]] = []
    scripts = {
        "a": [reply(tool_calls=[SEARCH]), reply("C-001"), reply(tool_calls=[SEARCH]), reply("?")],
        "b": [reply("hi"), reply("hi")],
    }
    order = iter(["a", "b"])

    def agent_for(stubs: Mapping[str, str]) -> Agent:
        stubs_seen.append(dict(stubs))
        return make_agent(scripts[next(order)])

    seen: list[RunRecord] = []
    runner = EvalRunner(
        data, agent_for, provider=None, repeats=2, on_record=seen.append, clock=_ticking_clock()
    )

    # Act
    records = await runner.run(data.cases)

    # Assert
    assert [(r.case_id, r.repeat, r.passed) for r in records] == [
        ("a", 1, True),
        ("a", 2, False),  # second answer misses C-001
        ("b", 1, True),
        ("b", 2, True),
    ]
    assert stubs_seen == [{"create_ticket": "{}"}, {"create_ticket": "{}", "search_customers": "x"}]
    assert seen == records
    assert all(r.latency_seconds == 1.0 for r in records)


async def test_a_transient_provider_failure_is_recorded_not_raised():
    data = dataset(case("a"))
    runner = EvalRunner(
        data,
        lambda _: make_agent([LLMUnavailableError("down"), reply("ok")]),
        provider=None,
        repeats=2,
    )

    records = await runner.run(data.cases)

    assert [r.error for r in records] == ["LLMUnavailableError", None]
    assert [r.passed for r in records] == [False, True]
    assert records[0].trajectory is None


async def test_a_broken_environment_aborts_the_whole_eval():
    data = dataset(case("a"), case("b"))
    runner = EvalRunner(
        data,
        lambda _: make_agent([ToolGatewayUnavailableError("mcp down")]),
        provider=None,
        repeats=3,
    )

    with pytest.raises(EvalAbortedError, match="ToolGatewayUnavailableError on case 'a'"):
        await runner.run(data.cases)


def test_repeats_must_be_positive():
    with pytest.raises(ValueError, match="at least 1"):
        EvalRunner(dataset(case()), lambda _: make_agent([]), provider=None, repeats=0)
