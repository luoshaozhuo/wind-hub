"""HTTP 追踪与写操作日志脱敏测试。"""

import logging

from fastapi.testclient import TestClient
from pytest import LogCaptureFixture

from web_server.app import create_app


def test_request_id_is_logged(caplog: LogCaptureFixture) -> None:
    with TestClient(create_app()) as client:
        with caplog.at_level(logging.INFO, logger="web_server.app"):
            response = client.get("/health/live")
    assert response.status_code == 200
    assert len(response.headers["X-Request-ID"]) == 32
    assert response.headers["X-Request-ID"] in caplog.text
    assert "duration_ms=" in caplog.text


def test_control_log_does_not_expose_secrets(caplog: LogCaptureFixture) -> None:
    with TestClient(create_app(write_token="server-secret")) as client:
        with caplog.at_level(logging.INFO, logger="web_server.app"):
            response = client.post(
                "/api/v1/commander/commands",
                headers={"X-API-Key": "client-secret"},
                json={"command_id": "private-command", "device_id": "private-device",
                      "point_id": "private-point", "value": 1},
            )
    assert response.status_code == 403
    assert "control_audit" in caplog.text
    for value in ("server-secret", "client-secret", "private-command", "private-device", "private-point"):
        assert value not in caplog.text
