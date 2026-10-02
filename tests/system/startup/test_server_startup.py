"""System：Server 启动/停机验收——Worker 登记可见、清单完整、优雅退出。

完整现场（从站 + Collector + Commander + Server 全部真实进程）下验证
系统边界行为：REST overview/workers/devices/tasks 反映真实拓扑，SIGTERM
后 Server 干净退出。
"""

from __future__ import annotations

import pytest

from tests.system.conftest import SYSTEM_COLLECTOR_ID, SYSTEM_TASK_ID, FullStack

pytestmark = pytest.mark.modbus


class TestServerStartup:
    async def test_overview_reflects_real_topology(self, full_stack: FullStack) -> None:
        response = await full_stack.http.get("/api/v1/overview")
        assert response.status_code == 200
        overview = response.json()

        assert overview["runtime_running"] is True
        assert overview["device_count"] == 1
        assert overview["task_count"] == 1
        assert overview["workers_unavailable"] == []

    async def test_workers_registered_and_probed(self, full_stack: FullStack) -> None:
        response = await full_stack.http.get("/api/v1/workers")
        assert response.status_code == 200
        workers = {w["worker_id"]: w for w in response.json()}

        assert set(workers) == {SYSTEM_COLLECTOR_ID, "commander"}
        collector = workers[SYSTEM_COLLECTOR_ID]
        assert collector["reported_id"] == SYSTEM_COLLECTOR_ID
        assert collector["runtime_running"] is True

    async def test_devices_and_tasks_listed(self, full_stack: FullStack) -> None:
        devices = await full_stack.http.get("/api/v1/devices")
        assert devices.status_code == 200
        assert [d["device_id"] for d in devices.json()["items"]] == ["modbus-1"]

        tasks = await full_stack.http.get("/api/v1/tasks")
        assert tasks.status_code == 200
        rows = tasks.json()["items"]
        assert [t["task_id"] for t in rows] == [SYSTEM_TASK_ID]
        assert rows[0]["assigned_worker_id"] == SYSTEM_COLLECTOR_ID

        instances = await full_stack.http.get(
            f"/api/v1/tasks/{SYSTEM_TASK_ID}/instances"
        )
        assert instances.status_code == 200
        assert len(instances.json()) == 1

    async def test_unknown_resource_returns_404(self, full_stack: FullStack) -> None:
        response = await full_stack.http.get("/api/v1/tasks/ghost-task")
        assert response.status_code == 404

    async def test_sigterm_exits_cleanly(self, full_stack: FullStack) -> None:
        exit_code = full_stack.server.terminate(timeout=30.0)
        assert exit_code == 0
