"""Web API composition tests."""

from fastapi.testclient import TestClient

from web_server.app import create_app


def test_liveness_does_not_claim_upstream_readiness() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
