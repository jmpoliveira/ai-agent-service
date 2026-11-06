from __future__ import annotations

import pytest

from src.domain.errors import (
    DomainError,
    LLMAuthError,
    LLMError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMResponseError,
    LLMUnavailableError,
    ToolCallingNotSupportedError,
    ToolGatewayError,
)
from src.domain.messages import ToolCall
from tests.fakes import reply, unreachable_provider
from tests.interface.api.conftest import ClientFactory

RUNS = "/v1/agent/runs"


def test_successful_run_returns_the_answer_and_metadata(make_client: ClientFactory):
    # Arrange
    client = make_client([reply("Ada has 2 open tickets.")])

    # Act
    response = client.post(RUNS, json={"prompt": "Tickets for Ada?"})

    # Assert
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Ada has 2 open tickets."
    assert body["stop_reason"] == "finished"
    assert body["steps"] == 1
    assert (body["provider"], body["model"]) == ("fake", "fake-model")
    assert body["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert len(body["trace_id"]) == 32


def test_run_with_a_tool_call_reports_two_steps(make_client: ClientFactory):
    # Arrange
    call = ToolCall("c1", "search_customers", {"query": "ada"})
    client = make_client([reply(tool_calls=[call]), reply("done")])

    # Act
    response = client.post(RUNS, json={"prompt": "go"})

    # Assert
    assert response.status_code == 200
    assert response.json()["steps"] == 2


def test_requested_provider_is_validated(make_client: ClientFactory):
    # Arrange
    client = make_client([])

    # Act
    response = client.post(RUNS, json={"prompt": "go", "provider": "gemini"})

    # Assert
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_provider"


@pytest.mark.parametrize("payload", [{}, {"prompt": ""}, {"prompt": 5}])
def test_malformed_requests_are_rejected_by_validation(
    make_client: ClientFactory, payload: dict[str, object]
):
    response = make_client([]).post(RUNS, json=payload)
    assert response.status_code == 422


def test_whitespace_only_prompt_is_a_client_error_not_a_server_error(make_client: ClientFactory):
    # Arrange: passes the schema (min_length=1) but is empty once sanitised
    client = make_client([])

    # Act
    response = client.post(RUNS, json={"prompt": "   "})

    # Assert
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_prompt"


def test_overlong_prompt_is_rejected(make_client: ClientFactory):
    response = make_client([]).post(RUNS, json={"prompt": "x" * 201})

    assert response.status_code == 422
    assert "maximum length" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (LLMRateLimitError("x"), 429, "rate_limited"),
        (LLMUnavailableError("x"), 503, "llm_unavailable"),
        (LLMAuthError("x"), 502, "llm_misconfigured"),
        (LLMInvalidRequestError("x"), 502, "llm_rejected_request"),
        (ToolCallingNotSupportedError("x"), 422, "tool_calling_not_supported"),
        (LLMResponseError("x"), 502, "llm_bad_response"),
        (LLMError("x"), 502, "llm_error"),
        (ToolGatewayError("x"), 502, "tools_error"),
        (DomainError("x"), 500, "internal_error"),
    ],
)
def test_domain_errors_map_to_http_statuses(
    make_client: ClientFactory, error: DomainError, status: int, code: str
):
    # Arrange
    client = make_client([error])

    # Act
    response = client.post(RUNS, json={"prompt": "go"})

    # Assert
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_unreachable_tool_server_is_a_503(make_client: ClientFactory):
    client = make_client([], provider=unreachable_provider())

    response = client.post(RUNS, json={"prompt": "go"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "tools_unavailable"


def test_error_responses_never_echo_upstream_details(make_client: ClientFactory):
    # Arrange
    client = make_client([LLMUnavailableError("http://internal-host:11434 refused (key=abc123)")])

    # Act
    response = client.post(RUNS, json={"prompt": "go"})

    # Assert
    assert "internal-host" not in response.text
    assert "abc123" not in response.text


def test_rate_limit_responses_carry_retry_after(make_client: ClientFactory):
    # Arrange
    client = make_client([LLMRateLimitError("x", retry_after_seconds=12.4)])

    # Act
    response = client.post(RUNS, json={"prompt": "go"})

    # Assert
    assert response.headers["Retry-After"] == "12"


# ---------------------------------------------------------------------------- security


def test_requests_without_the_api_key_are_rejected(make_client: ClientFactory):
    response = make_client([reply("x")], api_key="s3cret").post(RUNS, json={"prompt": "go"})
    assert response.status_code == 401


def test_requests_with_a_wrong_api_key_are_rejected(make_client: ClientFactory):
    client = make_client([reply("x")], api_key="s3cret")

    response = client.post(RUNS, json={"prompt": "go"}, headers={"X-API-Key": "nope"})

    assert response.status_code == 401


def test_requests_with_the_right_api_key_are_accepted(make_client: ClientFactory):
    client = make_client([reply("x")], api_key="s3cret")

    response = client.post(RUNS, json={"prompt": "go"}, headers={"X-API-Key": "s3cret"})

    assert response.status_code == 200


def test_health_probes_do_not_require_the_api_key(make_client: ClientFactory):
    assert make_client([], api_key="s3cret").get("/health/live").status_code == 200


def test_docs_can_be_switched_off(make_client: ClientFactory):
    assert make_client([], expose_docs=True).get("/docs").status_code == 200
    assert make_client([], expose_docs=False).get("/docs").status_code == 404
    assert make_client([], expose_docs=False).get("/openapi.json").status_code == 404
