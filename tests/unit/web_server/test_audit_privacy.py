"""HTTP 审计只输出路由模板，不泄露 URL 动态参数。"""

import logging

from fastapi.testclient import TestClient
from pytest import LogCaptureFixture

from web_server.app import create_app


def test_path_parameters_not_written_to_logs(caplog: LogCaptureFixture) -> None:
    app = create_app()
    with TestClient(app) as client:
        with caplog.at_level(logging.INFO, logger="web_server.app"):
            response = client.get(
                "/api/v1/commander/devices/device-private-123/points/point-private-456"
            )
    assert response.status_code == 503
    assert "device-private-123" not in caplog.text
    assert "point-private-456" not in caplog.text
    assert "/api/v1/commander/devices/{device_id}/points/{point_id}" in caplog.text


def test_read_many_is_not_logged_as_control_action(caplog: LogCaptureFixture) -> None:
    app = create_app()
    with TestClient(app) as client:
        with caplog.at_level(logging.INFO, logger="web_server.app"):
            response = client.post(
                "/api/v1/commander/devices/device-private-123/read-many",
                json={"point_ids": []},
            )
    assert response.status_code == 422
    assert "control_audit" not in caplog.text
