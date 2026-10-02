"""System：REST 诊断读写全链路——REST → Server → Commander → 真实从站。

即时设备读写由 Commander 独占（server-owned control）；本文件在系统
边界验证 REST 诊断端点驱动真实 Modbus 从站的读、写与回读一致性。
"""

from __future__ import annotations

import pytest

from tests.system.conftest import FullStack

pytestmark = pytest.mark.modbus


class TestProtocolDiagnostics:
    async def test_protocol_check_reports_connected(self, full_stack: FullStack) -> None:
        response = await full_stack.http.post(
            "/api/v1/diagnostics/protocol/check",
            json={"device_id": "modbus-1"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["connected"] is True

    async def test_read_returns_real_value(self, full_stack: FullStack) -> None:
        response = await full_stack.http.post(
            "/api/v1/diagnostics/protocol/read",
            json={"device_id": "modbus-1", "point_id": "rotor.speed"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["device_id"] == "modbus-1"
        assert payload["value"] == pytest.approx(1200.5)
        assert payload["quality"] == "good"

    async def test_write_reaches_slave_and_reads_back(self, full_stack: FullStack) -> None:
        before = full_stack.modbus.write_count
        response = await full_stack.http.post(
            "/api/v1/diagnostics/protocol/write",
            json={
                "device_id": "modbus-1",
                "point_id": "setpoint.power",
                "value": 1500.0,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["success"] is True
        assert full_stack.modbus.write_count == before + 1

        readback = await full_stack.http.post(
            "/api/v1/diagnostics/protocol/read",
            json={"device_id": "modbus-1", "point_id": "setpoint.power"},
        )
        assert readback.status_code == 200
        assert readback.json()["value"] == pytest.approx(1500.0)

    async def test_read_unknown_device_fails_cleanly(self, full_stack: FullStack) -> None:
        response = await full_stack.http.post(
            "/api/v1/diagnostics/protocol/read",
            json={"device_id": "ghost", "point_id": "rotor.speed"},
        )
        # 未知设备必须是非 2xx 且错误以 JSON 呈现，不允许 traceback 泄漏。
        assert response.status_code >= 400
        assert response.headers["content-type"].startswith("application/json")
