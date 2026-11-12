from __future__ import annotations

import pytest

from src.domain.agent import AgentBudget


def _budget(**overrides: float) -> AgentBudget:
    values: dict[str, float] = {
        "max_steps": 5,
        "max_total_tokens": 1000,
        "timeout_seconds": 10,
        "max_identical_tool_calls": 2,
    }
    values.update(overrides)
    return AgentBudget(
        max_steps=int(values["max_steps"]),
        max_total_tokens=int(values["max_total_tokens"]),
        timeout_seconds=values["timeout_seconds"],
        max_identical_tool_calls=int(values["max_identical_tool_calls"]),
    )


def test_valid_budget_is_accepted():
    assert _budget().max_steps == 5


@pytest.mark.parametrize(
    "override",
    [
        {"max_steps": 0},
        {"max_total_tokens": 0},
        {"timeout_seconds": 0},
        {"max_identical_tool_calls": 0},
    ],
)
def test_budget_rejects_non_positive_limits(override: dict[str, float]):
    with pytest.raises(ValueError, match="must be"):
        _budget(**override)
