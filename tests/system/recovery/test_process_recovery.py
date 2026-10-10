"""System：Server / Commander / Collector 进程故障与恢复的全链路验证。

进程模型约定（本文件逐条在系统边界验证）：

- Server 只是控制面/监控/REST 入口，不是数据采集单点——SIGKILL 后
  Collector 采集与 Sink 写入必须继续；重启后重新识别 Worker、恢复 API。
- Commander 只承载即时读写——SIGKILL 后采集继续、控制 API 明确失败
  （统一错误信封，不伪报成功）；重启后控制恢复。
- Collector SIGKILL 后 Server Worker 状态必须反映离线；重启后重新
  probe 上线，任务可重新下发、数据恢复。
- 配置事务：一个 Worker 不可达时全局 apply 必须失败，且不得对可达
  Worker 产生部分 activate。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.support.process import (
    CollectorProcess,
    start_collector,
    start_commander,
    start_server,
)
from tests.support.wait import (
    read_csv,
    wait_commander_ready,
    wait_file_rows,
    wait_grpc_ready,
    wait_http_ready,
    wait_until,
)
from tests.system.conftest import SYSTEM_COLLECTOR_ID, SYSTEM_TASK_ID, FullStack

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]


def _port_of(proc: CollectorProcess) -> int:
    return int(proc.grpc_target.rsplit(":", 1)[1])


async def _start_task(stack: FullStack) -> None:
    response = await stack.http.post(f"/api/v1/tasks/{SYSTEM_TASK_ID}/start")
    assert response.status_code == 200, response.text


async def _rows_grow(path: Path, *, window: float = 3.0, min_growth: int = 2) -> None:
    """在测量窗口内 sink 行数必须增长（采集自治性判据）。"""
    before = len(read_csv(path))
    await wait_file_rows(path, min_rows=before + min_growth, timeout=window + 7.0)


async def _worker_state(http, worker_id: str) -> str | None:
    workers = (await http.get("/api/v1/workers")).json()
    for worker in workers:
        if worker["worker_id"] == worker_id:
            return worker["state"]
    return None


class TestServerRestart:
    async def test_server_kill_does_not_stop_acquisition(
        self, full_stack: FullStack, tmp_path: Path
    ) -> None:
        """PROC-01：Server SIGKILL 后采集继续；重启后控制面完整恢复。"""
        stack = full_stack
        await _start_task(stack)
        await wait_file_rows(stack.sink_path, min_rows=2)

        # ---- 故障：SIGKILL Server ----
        http_port = _port_of(stack.server)
        stack.server.kill_tree()
        assert not stack.server.is_running()

        # ---- 数据面自治：Server 死亡期间行数持续增长 ----
        await _rows_grow(stack.sink_path)

        # ---- 恢复：同端口重启 Server ----
        restarted = start_server(
            stack.config_dir,
            http_port=http_port,
            collectors=[f"{SYSTEM_COLLECTOR_ID}={stack.collector.grpc_target}"],
            commander=stack.commander.grpc_target,
            log_dir=tmp_path,
        )
        try:
            await wait_http_ready(restarted.grpc_target)

            # Worker 重新识别（probe 周期 1s）。
            async def _online() -> str | None:
                state = await _worker_state(stack.http, SYSTEM_COLLECTOR_ID)
                return state if state == "online" else None

            await wait_until(_online, timeout=20.0, description="collector re-probed online")

            # API 状态恢复：task 可见、命令链路可用。
            task = await stack.http.get(f"/api/v1/tasks/{SYSTEM_TASK_ID}")
            assert task.status_code == 200, task.text
            command = await stack.http.post(
                "/api/v1/devices/modbus-1/commands",
                json={
                    "point_id": "setpoint.power",
                    "value": 33.3,
                    "timeout": 5.0,
                    "command_id": "cmd-after-server-restart",
                },
            )
            assert command.status_code == 200, command.text
            assert command.json()["success"] is True

            # 数据面在恢复后继续出数。
            await _rows_grow(stack.sink_path)
        finally:
            if restarted.is_running():
                restarted.terminate()
            restarted.close_log()


class TestCommanderRestart:
    async def test_commander_kill_keeps_acquisition_and_recovers(
        self, full_stack: FullStack, tmp_path: Path
    ) -> None:
        """PROC-02：Commander SIGKILL 后采集继续、控制明确失败；重启后控制恢复。"""
        stack = full_stack
        await _start_task(stack)
        await wait_file_rows(stack.sink_path, min_rows=2)

        # ---- 故障：SIGKILL Commander ----
        commander_port = _port_of(stack.commander)
        stack.commander.kill_tree()

        # ---- 采集继续 ----
        await _rows_grow(stack.sink_path)

        # ---- 即时控制明确失败（统一错误信封，不伪报成功） ----
        failed = await stack.http.post(
            "/api/v1/devices/modbus-1/commands",
            json={"point_id": "setpoint.power", "value": 1.0, "timeout": 5.0},
        )
        assert failed.status_code == 500, failed.text
        assert failed.json()["error"]["code"] == "INTERNAL_ERROR"

        # ---- 恢复：同端口重启 Commander，控制链路恢复 ----
        restarted = start_commander(
            stack.config_dir, grpc_port=commander_port, log_dir=tmp_path
        )
        try:
            await wait_commander_ready(restarted.grpc_target)

            async def _command_ok() -> dict | None:
                response = await stack.http.post(
                    "/api/v1/devices/modbus-1/commands",
                    json={
                        "point_id": "setpoint.power",
                        "value": 44.4,
                        "timeout": 5.0,
                        "command_id": "cmd-after-commander-restart",
                    },
                )
                if response.status_code != 200:
                    return None
                body = response.json()
                return body if body["success"] else None

            result = await wait_until(
                _command_ok, timeout=20.0, description="command after commander restart"
            )
            assert result["success"] is True
        finally:
            if restarted.is_running():
                restarted.terminate()
            restarted.close_log()


class TestCollectorRestart:
    async def test_server_marks_offline_and_recovers_after_restart(
        self, full_stack: FullStack, tmp_path: Path
    ) -> None:
        """PROC-03：Collector SIGKILL 后 Server 反映离线；重启后上线且任务可恢复。"""
        stack = full_stack
        await _start_task(stack)
        await wait_file_rows(stack.sink_path, min_rows=2)

        # ---- 故障：SIGKILL Collector，Server 必须观测到离线 ----
        collector_port = _port_of(stack.collector)
        stack.collector.kill_tree()

        async def _offline() -> str | None:
            state = await _worker_state(stack.http, SYSTEM_COLLECTOR_ID)
            return state if state == "offline" else None

        await wait_until(
            _offline, timeout=20.0, description="collector marked offline by server"
        )

        # ---- 恢复：同端口同 ID 重启，Server 重新 probe 上线 ----
        restarted = start_collector(
            stack.config_dir,
            grpc_port=collector_port,
            collector_id=SYSTEM_COLLECTOR_ID,
            log_dir=tmp_path,
        )
        try:
            await wait_grpc_ready(restarted.grpc_target)

            async def _online() -> str | None:
                state = await _worker_state(stack.http, SYSTEM_COLLECTOR_ID)
                return state if state == "online" else None

            await wait_until(_online, timeout=20.0, description="collector back online")

            # 任务控制恢复：重启的 Collector 是空运行时，Server 对账循环
            # （reconcile_interval=5s）重新下发 placement 后 start 才合法——
            # 之前的 PLACEMENT_UNSAFE 是 fencing 的正确表现，不是失败。
            async def _task_started() -> bool | None:
                response = await stack.http.post(
                    f"/api/v1/tasks/{SYSTEM_TASK_ID}/start"
                )
                return True if response.status_code == 200 else None

            await wait_until(
                _task_started,
                timeout=30.0,
                description="placement re-applied and task started",
            )
            await _rows_grow(stack.sink_path)
        finally:
            if restarted.is_running():
                restarted.terminate()
            restarted.close_log()


class TestConfigTransactionPartialFailure:
    async def test_apply_with_unreachable_worker_fails_globally(
        self, full_stack: FullStack
    ) -> None:
        """CFG-04：一个 Worker 不可达时全局 apply 失败，可达 Worker 不出现部分 activate。"""
        stack = full_stack
        await _start_task(stack)
        await wait_file_rows(stack.sink_path, min_rows=2)

        revisions_before = {
            w["worker_id"]: w["active_revision"]
            for w in (await stack.http.get("/api/v1/workers")).json()
        }

        # ---- Commander 不可达时发起 apply ----
        stack.commander.kill_tree()
        current = await stack.http.get("/api/v1/config/files/tasks.yaml")
        data = yaml.safe_load(current.json()["content"])
        data["tasks"][0]["interval"] = 0.9
        applied = await stack.http.post(
            "/api/v1/config/apply",
            json={"name": "tasks.yaml", "content": yaml.safe_dump(data)},
        )
        if applied.status_code == 200:
            assert applied.json()["success"] is False, applied.json()
        else:
            assert applied.status_code >= 400

        # ---- 全局失败：Collector 不得部分 activate（revision 不变、节拍不变） ----
        workers = (await stack.http.get("/api/v1/workers")).json()
        collector_row = next(
            w for w in workers if w["worker_id"] == SYSTEM_COLLECTOR_ID
        )
        assert collector_row["active_revision"] == revisions_before[SYSTEM_COLLECTOR_ID]
        task = await stack.http.get(f"/api/v1/tasks/{SYSTEM_TASK_ID}")
        assert task.json()["interval"] == pytest.approx(0.2)

        # 数据面不受失败事务影响。
        await _rows_grow(stack.sink_path)
