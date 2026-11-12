from __future__ import annotations

from tests.interface.api.conftest import ClientFactory, StaticHealthCheck


def test_liveness_returns_alive(make_client: ClientFactory):
    # Act
    response = make_client().get("/health/live")

    # Assert
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_is_ready_with_no_dependencies(make_client: ClientFactory):
    response = make_client().get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "dependencies": []}


def test_readiness_is_200_when_every_dependency_is_healthy(make_client: ClientFactory):
    # Arrange
    client = make_client(checks=[StaticHealthCheck("ollama", True), StaticHealthCheck("mcp", True)])

    # Act
    response = client.get("/health/ready")

    # Assert
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_readiness_is_503_when_a_dependency_is_down(make_client: ClientFactory):
    # Arrange
    client = make_client(
        checks=[StaticHealthCheck("ollama", True), StaticHealthCheck("mcp", False, "unreachable")]
    )

    # Act
    response = client.get("/health/ready")

    # Assert
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert {"name": "mcp", "healthy": False, "detail": "unreachable"} in body["dependencies"]
