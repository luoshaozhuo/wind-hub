"""Recovery：reload 失败韧性——错误配置不炸进程、旧配置继续服役、修复后重载成功。

全周期：正常采集 → 写入非法配置并 reload（失败）→ 进程存活且旧配置
继续采集 → 写回合法配置并 reload（成功）→ 数据面不中断。reload 经
PrepareConfig/ActivateConfig 两段事务下发（与 Server → Collector 生产
控制路径一致）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.reliability.recovery.helpers import (
    read_telemetry_rows,
    wait_telemetry_rows,
    write_modbus_file_config,
)
from tests.support.control import (
    prepare_config,
    reload_config,
    start_task_instance,
)
from tests.support.functional_config import update_yaml
from tests.support.process import CollectorProcess, run_cli_async

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

TASK_ID = "modbus-telemetry"
INSTANCE_ID = "modbus-telemetry:modbus-1"


class TestReloadFailureResilience:
    async def test_failed_reload_keeps_running_and_recovers(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        sink_root = tmp_path / "archive"
        config_dir = write_modbus_file_config(tmp_path / "cfg", modbus_server.port, sink_root)
        proc: CollectorProcess = await collector_factory(config_dir)

        # ---- 正常：采集数据到达 sink ----
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)
        await wait_telemetry_rows(sink_root, min_rows=4)

        # ---- 故障：tasks.yaml 指向不存在的 sink，prepare 必须拒绝 ----
        update_yaml(
            config_dir,
            "tasks.yaml",
            lambda data: data["tasks"]["modbus-telemetry"].update(
                {"outputs": [{"sink_id": "ghost_sink"}]}
            ),
        )
        prepared = await prepare_config(
            proc.grpc_target, config_dir=config_dir, revision_id="rev-broken"
        )
        assert prepared.success is False
        assert prepared.errors

        # ---- 存活：进程在、旧配置继续服役（行数持续增长） ----
        assert proc.is_running()
        rows_before = len(read_telemetry_rows(sink_root))
        await wait_telemetry_rows(sink_root, min_rows=rows_before + 2)

        # ---- 修复：写回合法配置（顺带改节拍证明 diff 生效），reload 成功 ----
        def _restore(data: dict[str, Any]) -> None:
            data["tasks"]["modbus-telemetry"]["outputs"] = [{"sink_id": "file_sink"}]
            data["tasks"]["modbus-telemetry"]["interval"] = 0.5

        update_yaml(config_dir, "tasks.yaml", _restore)
        activated = await reload_config(
            proc.grpc_target, config_dir=config_dir, revision_id="rev-restored"
        )
        assert activated.success is True, list(activated.errors)

        task = await run_cli_async("task", TASK_ID, target=proc.grpc_target)
        assert json.loads(task.stdout)["interval"] == 0.5

        # ---- 数据面贯穿全程不中断 ----
        await wait_telemetry_rows(sink_root, min_rows=rows_before + 4)
