"""Recovery：reload 失败韧性——错误配置不炸进程、旧配置继续服役、修复后重载成功。

全周期：正常采集 → 写入非法配置并 reload（失败）→ 进程存活且旧配置
继续采集 → 写回合法配置并 reload（成功）→ 数据面不中断。reload 经
PrepareConfig/ActivateConfig 两段事务下发（与 Server → Collector 生产
控制路径一致）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.component.collector.conftest import update_yaml
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.reliability.recovery.helpers import write_modbus_file_config
from tests.support.control import (
    apply_placement_and_start_instance,
    prepare_config,
    reload_config,
)
from tests.support.process import CollectorProcess, run_ctl_async
from tests.support.wait import read_jsonl, wait_file_rows

pytestmark = pytest.mark.modbus

TASK_ID = "modbus-telemetry"
INSTANCE_ID = "modbus-telemetry:modbus-1"


class TestReloadFailureResilience:
    async def test_failed_reload_keeps_running_and_recovers(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        sink_path = tmp_path / "out" / "telemetry.jsonl"
        config_dir = write_modbus_file_config(tmp_path / "cfg", modbus_server.port, sink_path)
        proc: CollectorProcess = await collector_factory(config_dir)

        # ---- 正常：采集数据到达 sink ----
        await apply_placement_and_start_instance(
            proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
        )
        await wait_file_rows(sink_path, min_rows=4)

        # ---- 故障：tasks.yaml 指向不存在的 sink，prepare 必须拒绝 ----
        update_yaml(
            config_dir,
            "tasks.yaml",
            lambda data: data["tasks"][0].update({"targets": [{"sink": "ghost_sink"}]}),
        )
        prepared = await prepare_config(
            proc.grpc_target, config_dir=config_dir, revision_id="rev-broken"
        )
        assert prepared.success is False
        assert prepared.errors

        # ---- 存活：进程在、旧配置继续服役（行数持续增长） ----
        assert proc.is_running()
        rows_before = len(read_jsonl(sink_path))
        await wait_file_rows(sink_path, min_rows=rows_before + 2)

        # ---- 修复：写回合法配置（顺带改节拍证明 diff 生效），reload 成功 ----
        def _restore(data: dict) -> None:
            data["tasks"][0]["targets"] = [{"sink": "file_sink"}]
            data["tasks"][0]["interval"] = 0.5

        update_yaml(config_dir, "tasks.yaml", _restore)
        activated = await reload_config(
            proc.grpc_target, config_dir=config_dir, revision_id="rev-restored"
        )
        assert activated.success is True, list(activated.errors)

        task = await run_ctl_async("task", TASK_ID, target=proc.grpc_target)
        assert json.loads(task.stdout)["interval"] == 0.5

        # ---- 数据面贯穿全程不中断 ----
        await wait_file_rows(sink_path, min_rows=rows_before + 4)
