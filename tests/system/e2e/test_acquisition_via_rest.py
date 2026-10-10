"""System E2E：REST → Server → Collector 全链路采集验收。

经 REST 启动/停止 Task（Server 内部完成 placement 下发与受栅栏保护的
Start——与生产控制路径一致），数据面验证发生在系统边界：File sink 的
输出行数。这条链路同时覆盖 Server 出站适配器的 placement 契约。
"""

from __future__ import annotations

import asyncio

import pytest

from tests.support.wait import read_csv, wait_file_rows
from tests.system.conftest import SYSTEM_TASK_ID, FullStack

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]


class TestAcquisitionViaRest:
    async def test_start_task_streams_points_to_sink(self, full_stack: FullStack) -> None:
        response = await full_stack.http.post(f"/api/v1/tasks/{SYSTEM_TASK_ID}/start")
        assert response.status_code == 200, response.text
        task = response.json()
        assert task["runtime_state"] == "running"
        assert task["running_instances"] == 1

        rows = await wait_file_rows(
            full_stack.sink_path,
            min_rows=4,
            match=lambda r: r["point_id"] == "rotor.speed",
        )
        assert all(r["device_id"] == "modbus-1" for r in rows)
        assert all(r["value"] == pytest.approx(1200.5) for r in rows)

    async def test_stop_task_halts_delivery(self, full_stack: FullStack) -> None:
        started = await full_stack.http.post(f"/api/v1/tasks/{SYSTEM_TASK_ID}/start")
        assert started.status_code == 200, started.text
        await wait_file_rows(full_stack.sink_path, min_rows=4)

        stopped = await full_stack.http.post(f"/api/v1/tasks/{SYSTEM_TASK_ID}/stop")
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["running_instances"] == 0

        # 静默验证：先等残余缓冲落盘，再确认行数不再增长。
        await asyncio.sleep(1.0)
        settled = len(read_csv(full_stack.sink_path))
        await asyncio.sleep(0.8)
        assert len(read_csv(full_stack.sink_path)) == settled

    async def test_start_unknown_task_returns_404(self, full_stack: FullStack) -> None:
        response = await full_stack.http.post("/api/v1/tasks/ghost-task/start")
        assert response.status_code == 404

    async def test_task_state_visible_via_rest(self, full_stack: FullStack) -> None:
        await full_stack.http.post(f"/api/v1/tasks/{SYSTEM_TASK_ID}/start")
        await wait_file_rows(full_stack.sink_path, min_rows=2)

        instances = await full_stack.http.get(
            f"/api/v1/tasks/{SYSTEM_TASK_ID}/instances"
        )
        assert instances.status_code == 200
        assert instances.json()[0]["state"] == "running"

        overview = await full_stack.http.get("/api/v1/overview")
        assert overview.json()["task_instances_running"] == 1
