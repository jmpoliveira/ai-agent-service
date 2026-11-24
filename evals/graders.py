"""Code-based graders: deterministic checks of one trajectory against one expectation.

Each grader returns a ``Check`` only when its expectation is set, so a case is scored only
on what it states. No LLM-as-judge here: code checks are cheap, fast and unambiguous, and
they are the right first layer (an LLM judge can be added later for open-ended answers).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from evals.dataset import ArgMatcher, Expectation
from evals.trajectory import RecordedCall, Trajectory


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    detail: str = ""


def grade(expect: Expectation, trajectory: Trajectory) -> list[Check]:
    """Run every applicable grader, in a fixed order so reports are stable."""
    graders: Sequence[Callable[[Expectation, Trajectory], Check | None]] = (
        _stop_reason,
        _no_tools,
        _tool_sequence,
        _arguments,
        _forbidden_tools,
        _tool_errors,
        _grounded_ids,
        _max_steps,
        _answer,
    )
    return [check for grader in graders if (check := grader(expect, trajectory)) is not None]


def _stop_reason(expect: Expectation, t: Trajectory) -> Check:
    passed = t.stop_reason == expect.stop_reason
    return Check("stop_reason", passed, "" if passed else f"got '{t.stop_reason}'")


def _no_tools(expect: Expectation, t: Trajectory) -> Check | None:
    if not expect.no_tools:
        return None
    passed = not t.calls
    return Check("no_tools", passed, "" if passed else f"called {list(t.tool_names)}")


def _tool_sequence(expect: Expectation, t: Trajectory) -> Check | None:
    if not expect.tools:
        return None
    # Ordered subsequence: the expected tools in this order, other calls allowed in between.
    remaining = iter(t.tool_names)
    passed = all(any(name == expected for name in remaining) for expected in expect.tools)
    detail = "" if passed else f"expected {list(expect.tools)} in order, got {list(t.tool_names)}"
    return Check("tool_sequence", passed, detail)


def _arguments(expect: Expectation, t: Trajectory) -> Check | None:
    if not expect.arguments:
        return None
    problems: list[str] = []
    for tool, matchers in expect.arguments.items():
        calls = [call for call in t.calls if call.name == tool]
        if not calls:
            problems.append(f"{tool} was not called")
        elif not any(_call_matches(call, matchers) for call in calls):
            seen = [call.arguments for call in calls]
            problems.append(f"no {tool} call matched {_describe(matchers)}; got {seen}")
    return Check("arguments", not problems, "; ".join(problems))


def _forbidden_tools(expect: Expectation, t: Trajectory) -> Check | None:
    if not expect.forbidden_tools:
        return None
    called = sorted(set(t.tool_names) & set(expect.forbidden_tools))
    return Check("forbidden_tools", not called, f"called {called}" if called else "")


def _tool_errors(expect: Expectation, t: Trajectory) -> Check | None:
    if expect.max_tool_errors is None:
        return None
    failed = [call for call in t.calls if call.is_error]
    passed = len(failed) <= expect.max_tool_errors
    detail = (
        ""
        if passed
        else (
            f"{len(failed)} failed calls (max {expect.max_tool_errors}): "
            + ", ".join(f"{call.name}({call.arguments})" for call in failed)
        )
    )
    return Check("tool_errors", passed, detail)


def _grounded_ids(expect: Expectation, t: Trajectory) -> Check | None:
    """Every identifier passed to a tool must already be in the conversation.

    Catches guesses that happen to be right (a failed guess is caught by tool_errors), and
    writes made with an invented id. Skipped for trajectories saved without tool results.
    """
    if not expect.id_pattern or t.prompt is None or any(c.result is None for c in t.calls):
        return None
    pattern = re.compile(expect.id_pattern)
    guessed: list[str] = []
    for call in t.calls:
        # Only results of earlier steps: calls in the same step cannot see each other's results.
        seen = [t.prompt, *(c.result or "" for c in t.calls if c.step < call.step)]
        for name, value in call.arguments.items():
            identifier = value.strip() if isinstance(value, str) else ""
            if pattern.fullmatch(identifier) and not any(_mentions(s, identifier) for s in seen):
                guessed.append(f"{call.name}({name}={value!r})")
    detail = f"ids not seen before use: {', '.join(guessed)}" if guessed else ""
    return Check("grounded_ids", not guessed, detail)


def _mentions(text: str, identifier: str) -> bool:
    # Whole-token match: "C-001" must not count as seen inside "C-0010".
    token = rf"(?<![\w-]){re.escape(identifier)}(?![\w-])"
    return re.search(token, text, re.IGNORECASE) is not None


def _max_steps(expect: Expectation, t: Trajectory) -> Check | None:
    if expect.max_steps is None:
        return None
    passed = t.steps <= expect.max_steps
    return Check("max_steps", passed, "" if passed else f"{t.steps} > {expect.max_steps}")


def _answer(expect: Expectation, t: Trajectory) -> Check | None:
    if not (expect.answer_contains or expect.answer_contains_any or expect.answer_not_contains):
        return None
    answer = _normalise(t.answer)
    problems: list[str] = []
    missing = [text for text in expect.answer_contains if _normalise(text) not in answer]
    if missing:
        problems.append(f"missing {missing}")
    if expect.answer_contains_any and not any(
        _normalise(text) in answer for text in expect.answer_contains_any
    ):
        problems.append(f"none of {list(expect.answer_contains_any)}")
    present = [text for text in expect.answer_not_contains if _normalise(text) in answer]
    if present:
        problems.append(f"must not contain {present}")
    return Check("answer", not problems, "; ".join(problems))


def _call_matches(call: RecordedCall, matchers: dict[str, ArgMatcher]) -> bool:
    return all(
        name in call.arguments and _value_matches(call.arguments[name], matcher)
        for name, matcher in matchers.items()
    )


def _value_matches(value: Any, matcher: ArgMatcher) -> bool:
    if matcher.contains is not None:
        return isinstance(value, str) and _normalise(matcher.contains) in _normalise(value)
    if matcher.one_of is not None:
        return any(_same(value, option) for option in matcher.one_of)
    return _same(value, matcher.equals)


def _same(value: Any, expected: Any) -> bool:
    if isinstance(value, str) and isinstance(expected, str):
        return _normalise(value) == _normalise(expected)
    return value == expected


# Typographic quotes some models write (GPT-6: "couldn\u2019t") and the plain ones in datasets.
_PLAIN_QUOTES = str.maketrans(
    {
        "\u2018": "'",  # left single quotation mark
        "\u2019": "'",  # right single quotation mark (the usual typographic apostrophe)
        "\u201b": "'",  # single high-reversed-9 quotation mark
        "\u2032": "'",  # prime
        "\u201c": '"',  # left double quotation mark
        "\u201d": '"',  # right double quotation mark
        "\u201e": '"',  # double low-9 quotation mark
        "\u2033": '"',  # double prime
    }
)


def _normalise(text: str) -> str:
    # casefold + collapsed whitespace + plain quotes: "In  Progress" and "in progress" are the
    # same answer, and so are "couldn\u2019t" and "couldn't".
    return " ".join(text.translate(_PLAIN_QUOTES).casefold().split())


def _describe(matchers: dict[str, ArgMatcher]) -> str:
    return str({name: m.model_dump(exclude_none=True) for name, m in matchers.items()})
