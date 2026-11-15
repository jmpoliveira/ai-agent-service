from __future__ import annotations

from typing import Any

import pytest

from evals.graders import Check, grade
from tests.evals.helpers import expect, trajectory


def _by_name(checks: list[Check]) -> dict[str, Check]:
    return {check.name: check for check in checks}


def test_only_stated_expectations_are_checked():
    checks = grade(expect(), trajectory(answer="anything"))

    assert [c.name for c in checks] == ["stop_reason"]


def test_a_good_run_passes_every_check():
    # Arrange
    expectation = expect(
        tools=["search_customers", "create_ticket"],
        arguments={"create_ticket": {"customer_id": "C-002", "priority": {"one_of": ["high"]}}},
        forbidden_tools=["delete_everything"],
        max_steps=3,
        answer_contains=["T-1004"],
    )
    run = trajectory(
        [
            ("search_customers", {"query": "Bruno"}),
            ("create_ticket", {"customer_id": "c-002", "priority": "high"}),
        ],
        answer="Created ticket T-1004.",
        steps=3,
    )

    # Act
    checks = grade(expectation, run)

    # Assert
    assert all(c.passed for c in checks), checks
    assert [c.name for c in checks] == [
        "stop_reason",
        "tool_sequence",
        "arguments",
        "forbidden_tools",
        "max_steps",
        "answer",
    ]


@pytest.mark.parametrize(
    ("called", "passed"),
    [
        (["search_customers", "create_ticket"], True),
        (["search_customers", "get_ticket_info", "create_ticket"], True),  # extra call between
        (["create_ticket", "search_customers"], False),  # wrong order
        (["search_customers"], False),  # missing step
    ],
)
def test_tool_sequence_is_an_ordered_subsequence(called: list[str], passed: bool):
    run = trajectory([(name, {}) for name in called])

    check = _by_name(grade(expect(tools=["search_customers", "create_ticket"]), run))

    assert check["tool_sequence"].passed is passed


def test_arguments_pass_when_any_call_matches_and_explain_failures():
    expectation = expect(arguments={"search_customers": {"query": {"contains": "ALICE"}}})

    good = grade(
        expectation,
        trajectory(
            [("search_customers", {"query": "x"}), ("search_customers", {"query": "alice johnson"})]
        ),
    )
    wrong = grade(expectation, trajectory([("search_customers", {"query": "bob"})]))
    missing = grade(expectation, trajectory())

    assert _by_name(good)["arguments"].passed
    assert "no search_customers call matched" in _by_name(wrong)["arguments"].detail
    assert "search_customers was not called" in _by_name(missing)["arguments"].detail


def test_argument_matchers_do_not_crash_on_unexpected_types():
    expectation = expect(arguments={"t": {"n": {"contains": "5"}, "m": 3}})

    checks = grade(expectation, trajectory([("t", {"n": 5, "m": 3})]))

    assert not _by_name(checks)["arguments"].passed  # contains needs text; 5 is a number


def test_no_tools_and_forbidden_tools_report_what_was_called():
    run = trajectory([("create_ticket", {})])

    checks = _by_name(grade(expect(no_tools=True, forbidden_tools=["create_ticket"]), run))

    assert checks["no_tools"].detail == "called ['create_ticket']"
    assert checks["forbidden_tools"].detail == "called ['create_ticket']"


def test_answer_checks_ignore_case_and_extra_spaces():
    expectation = expect(
        answer_contains=["In Progress"],
        answer_contains_any=["not found", "high"],
        answer_not_contains=["T-1002"],
    )

    good = grade(expectation, trajectory(answer="Status: in   progress, priority HIGH"))
    bad = grade(expectation, trajectory(answer="Ticket T-1002 is open"))

    assert _by_name(good)["answer"].passed
    detail = _by_name(bad)["answer"].detail
    assert "missing ['In Progress']" in detail
    assert "none of" in detail
    assert "must not contain ['T-1002']" in detail


def test_wrong_stop_reason_and_too_many_steps_fail():
    run = trajectory(stop_reason="max_steps", steps=6)

    checks = _by_name(grade(expect(max_steps=5), run))

    assert checks["stop_reason"].detail == "got 'max_steps'"
    assert checks["max_steps"].detail == "6 > 5"


def test_tool_errors_flag_a_run_that_guessed_its_way_to_the_answer():
    # The real Gemini run: a customer id used as a ticket id, a guessed id, then a lucky guess.
    run = trajectory(
        [
            ("search_customers", {"query": "Bruno Costa"}),
            ("get_ticket_info", {"ticket_id": "C-002"}),
            ("get_ticket_info", {"ticket_id": "T-002"}),
            ("get_ticket_info", {"ticket_id": "T-1002"}),
        ],
        errors=[False, True, True, False],
        answer="Bruno has ticket T-1002.",
    )

    checks = _by_name(grade(expect(max_tool_errors=0, answer_contains=["T-1002"]), run))

    assert checks["answer"].passed  # the answer alone would have passed
    assert not checks["tool_errors"].passed
    assert checks["tool_errors"].detail.startswith("2 failed calls (max 0): get_ticket_info(")


def test_tool_errors_allow_an_expected_error():
    run = trajectory([("get_ticket_info", {"ticket_id": "T-9999"})], errors=[True])

    assert _by_name(grade(expect(max_tool_errors=1), run))["tool_errors"].passed


ID = r"^[A-Za-z]-\d+$"
CUSTOMER = '{"customers": [{"id": "C-001", "name": "Alice Johnson"}]}'
TICKET = '{"id": "T-1001", "customer_id": "C-001", "status": "in_progress"}'


def _grounded(run: Any) -> Check:
    return _by_name(grade(expect(id_pattern=ID), run))["grounded_ids"]


def test_a_right_guess_is_still_a_guess():
    # The model saw C-001 and guessed T-1001 from the pattern; the server said "ok".
    run = trajectory(
        [("search_customers", {"query": "Alice"}), ("get_ticket_info", {"ticket_id": "T-1001"})],
        prompt="Which tickets does Alice have?",
        results=[CUSTOMER, TICKET],
    )

    check = _grounded(run)

    assert not check.passed
    assert check.detail == "ids not seen before use: get_ticket_info(ticket_id='T-1001')"


def test_ids_from_the_prompt_or_an_earlier_result_are_grounded():
    run = trajectory(
        [
            ("get_ticket_info", {"ticket_id": "T-1001"}),  # from the prompt, lower case there
            ("search_customers", {"query": "C-001"}),  # from the first result
        ],
        prompt="Status of t-1001?",
        results=[TICKET, CUSTOMER],
    )

    assert _grounded(run).passed


def test_results_of_the_same_step_do_not_ground_each_other():
    run = trajectory(
        [("search_customers", {"query": "Alice"}), ("create_ticket", {"customer_id": "C-001"})],
        prompt="Open a ticket for Alice",
        results=[CUSTOMER, "{}"],
        call_steps=[1, 1],  # asked for together, before any result was back
    )

    assert "create_ticket(customer_id='C-001')" in _grounded(run).detail


def test_ids_match_whole_tokens_only():
    run = trajectory(
        [("get_ticket_info", {"ticket_id": "T-100"})],
        prompt="Is T-1001 open?",  # T-100 is only a prefix of what the user wrote
        results=[TICKET],
    )

    assert not _grounded(run).passed


def test_grounding_is_skipped_without_recorded_results_or_when_disabled():
    old_report = trajectory([("get_ticket_info", {"ticket_id": "T-1"})], results=None)
    recorded = trajectory([("get_ticket_info", {"ticket_id": "T-1"})], prompt="?", results=["x"])

    assert "grounded_ids" not in _by_name(grade(expect(id_pattern=ID), old_report))
    assert "grounded_ids" not in _by_name(grade(expect(id_pattern=""), recorded))
    assert "grounded_ids" not in _by_name(grade(expect(), recorded))
