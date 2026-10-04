"""Component：File Sink 文件系统故障（EACCES / ENOSPC）的隔离与恢复。

文件系统故障不得波及其他 sink、不得阻断采集主循环：

- 启动期 EACCES（目录不可写）→ 该 sink 标记 open failed，其余 sink
  正常出数；修复权限并 reload 后，同一份配置重建成功、数据落盘。
- 运行期 ENOSPC（``/dev/full`` 真实注入）→ 连续写失败达到阈值后该
  sink 报告 unhealthy（错误消息可见 ENOSPC），好 sink 与采集不受影响。
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from tests.component.collector.conftest import FunctionalContext, update_yaml
from tests.support.wait import read_jsonl, wait_file_rows, wait_until

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

SINKS = "sinks.yaml"

#: FileSink 连续写失败达到 5 次才标记 unhealthy（决策 8）；interval 0.2s
#: 下 2s 窗口足够越阈。
_UNHEALTHY_TIMEOUT = 8.0


def _two_file_sinks(bad_path: Path, good_path: Path) -> list[dict[str, Any]]:
    return [
        {
            "name": "bad_file",
            "type": "file",
            "connection": {
                "path": str(bad_path),
                "buffer_size": 1,
                "flush_interval": 0.2,
            },
        },
        {
            "name": "good_file",
            "type": "file",
            "connection": {
                "path": str(good_path),
                "buffer_size": 4,
                "flush_interval": 0.5,
            },
        },
    ]


def _task_both_sinks() -> list[dict[str, Any]]:
    return [
        {
            "task_id": "modbus-telemetry",
            "device": "modbus-1",
            "point_group": "telemetry",
            "interval": 0.2,
            "targets": [{"sink": "bad_file"}, {"sink": "good_file"}],
        }
    ]


async def _start_instances(ctx: FunctionalContext) -> None:
    for instance in await ctx.rt.tasks.list_instances():
        await ctx.rt.tasks.start_instance(instance.instance_id)


class TestFileSinkFsFaults:
    async def test_open_eacces_isolated_and_recovers_after_fix(
        self, runtime_factory, tmp_path: Path
    ) -> None:
        """启动期 EACCES：坏 sink open failed，好 sink 正常；修复+reload 后恢复。"""
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        bad_path = readonly_dir / "out" / "telemetry.jsonl"
        good_path = tmp_path / "good" / "telemetry.jsonl"
        readonly_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)  # 0o500：不可写

        try:
            async with runtime_factory(
                sinks=_two_file_sinks(bad_path, good_path),
                tasks=_task_both_sinks(),
            ) as ctx:
                await _start_instances(ctx)

                # ---- 故障被检测：坏 sink open failed ----
                async def _bad_unhealthy() -> bool | None:
                    health = ctx.rt.runtime.health()["bad_file"]
                    return True if not health.healthy else None

                await wait_until(
                    _bad_unhealthy, timeout=5.0, description="bad sink marked open failed"
                )
                assert "open failed" in (
                    ctx.rt.runtime.health()["bad_file"].message or ""
                )

                # ---- 隔离：好 sink 持续出数，采集主循环不受影响 ----
                await wait_file_rows(good_path, min_rows=3, timeout=10.0)
                snap = ctx.rt.metrics_state.snapshot()
                assert ctx.rt.runtime.health()["good_file"].healthy is True

                # ---- 恢复：修复权限 + 触发 sink 重建（buffer_size 变更） ----
                readonly_dir.chmod(stat.S_IRWXU)  # 0o700

                def mutate(data: dict[str, Any]) -> None:
                    for sink in data["sinks"]:
                        if sink["name"] == "bad_file":
                            sink["connection"]["buffer_size"] = 4

                update_yaml(ctx.config_dir, SINKS, mutate)
                result = await ctx.rt.config.reload()
                assert result.success, result.errors

                async def _bad_healthy() -> bool | None:
                    return True if ctx.rt.runtime.health()["bad_file"].healthy else None

                await wait_until(
                    _bad_healthy, timeout=5.0, description="bad sink reopened after fix"
                )
                await wait_file_rows(bad_path, min_rows=2, timeout=10.0)

                # 好 sink 全程无中断。
                assert (
                    ctx.rt.metrics_state.snapshot()["counters"]["acquisition_runs"]
                    > snap["counters"]["acquisition_runs"]
                )
        finally:
            # 用例失败也要恢复权限，否则 tmp_path 清理报 EACCES。
            readonly_dir.chmod(stat.S_IRWXU)

    async def test_enospc_mid_run_marks_unhealthy_and_isolates(
        self, runtime_factory, tmp_path: Path
    ) -> None:
        """运行期 ENOSPC（/dev/full）：坏 sink 越阈 unhealthy，好 sink 不受影响。"""
        if not os.path.exists("/dev/full"):
            pytest.skip("/dev/full 不存在（非标准 Linux 环境）——NOT_RUN")

        good_path = tmp_path / "good" / "telemetry.jsonl"
        async with runtime_factory(
            sinks=_two_file_sinks(Path("/dev/full"), good_path),
            tasks=_task_both_sinks(),
        ) as ctx:
            await _start_instances(ctx)

            # 故障前：好 sink 正常出数。
            await wait_file_rows(good_path, min_rows=2, timeout=10.0)

            # ---- 故障被检测：连续写失败越阈，unhealthy 且错误消息可见 ENOSPC ----
            async def _full_unhealthy() -> bool | None:
                return (
                    True if not ctx.rt.runtime.health()["bad_file"].healthy else None
                )

            await wait_until(
                _full_unhealthy,
                timeout=_UNHEALTHY_TIMEOUT,
                description="ENOSPC sink marked unhealthy",
            )
            message = ctx.rt.runtime.health()["bad_file"].message or ""
            assert "No space" in message, f"错误消息应暴露 ENOSPC 根因: {message}"

            # ---- 隔离：坏 sink 持续失败期间，好 sink 与采集主循环不受影响 ----
            rows_before = len(read_jsonl(good_path))
            runs_before = ctx.rt.metrics_state.snapshot()["counters"]["acquisition_runs"]
            await asyncio.sleep(2.0)
            await wait_file_rows(good_path, min_rows=rows_before + 2, timeout=5.0)
            runs_after = ctx.rt.metrics_state.snapshot()["counters"]["acquisition_runs"]
            assert runs_after - runs_before >= 5
            assert ctx.rt.runtime.health()["good_file"].healthy is True
            assert ctx.rt.runtime.health()["modbus-1"].healthy is True
