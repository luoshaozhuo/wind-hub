"""Component：File Sink 文件系统故障（EACCES / ENOSPC）的隔离与恢复。

文件系统故障不得波及其他 sink、不得阻断采集主循环：

- 启动期 EACCES（目录不可写）→ 该 sink 标记 open failed，其余 sink
  正常出数；修复权限并 reload 后，同一份配置重建成功、数据落盘。
- 运行期 ENOSPC（``/dev/full`` 真实注入）→ 连续写失败达到阈值后该
  sink 报告 unhealthy（错误消息可见 ENOSPC），好 sink 与采集不受影响。
"""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from typing import Any

import pytest

from core.domain.point_value import PointValue
from core.infrastructure.sink.file import FileSink
from tests.support.functional_config import update_yaml
from tests.support.wait import read_csv, wait_until

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

SINKS = "sinks.yaml"

#: FileSink 写入失败立即标记 unhealthy；等待窗口用于观察 Collector 错误传播。
_UNHEALTHY_TIMEOUT = 8.0


def _two_file_sinks(bad_root: Path, good_root: Path) -> dict[str, Any]:
    return {
        "bad_file": {
            "type": "file",
            "connection": {
                "path": str(bad_root),
                "retention_days": 3,
            },
        },
        "good_file": {
            "type": "file",
            "connection": {
                "path": str(good_root),
                "retention_days": 4,
            },
        },
    }


def _task_both_sinks() -> dict[str, Any]:
    return {
        "modbus-telemetry": {
            "device_group_id": "modbus-1",
            "point_group": "telemetry",
            "interval": 0.2,
            "outputs": [
                {"sink_id": "bad_file"},
                {"sink_id": "good_file"},
            ],
        }
    }


def _archive_rows(root: Path) -> list[dict[str, Any]]:
    """汇总 FileSink 归档目录下所有 Task/Device/年月分段的数据行。"""
    rows: list[dict[str, Any]] = []
    for segment in root.glob("*/*/[0-9][0-9][0-9][0-9]/[0-9][0-9]/*.csv"):
        rows.extend(read_csv(segment))
    return rows


async def _wait_archive_rows(root: Path, *, min_rows: int, timeout: float) -> list[dict[str, Any]]:
    async def _probe() -> list[dict[str, Any]] | None:
        rows = _archive_rows(root)
        return rows if len(rows) >= min_rows else None

    return await wait_until(_probe, timeout=timeout, description=f"{min_rows} rows under {root}")


class TestFileSinkFsFaults:
    async def test_open_eacces_isolated_and_recovers_after_fix(
        self, app_factory, tmp_path: Path
    ) -> None:
        """启动期 EACCES：坏 sink open failed，好 sink 正常；修复+reload 后恢复。"""
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        bad_root = readonly_dir / "out"
        good_root = tmp_path / "good"
        readonly_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)  # 0o500：不可写

        try:
            ctx = await app_factory(
                sinks=_two_file_sinks(bad_root, good_root),
                tasks=_task_both_sinks(),
            )
            await ctx.start_instances()
            app = ctx.app

            # ---- 故障被检测：坏 sink open failed ----
            async def _bad_unhealthy() -> bool | None:
                health = (await app.runtime.health())["bad_file"]
                return True if not health.healthy else None

            unhealthy: bool = await wait_until(
                _bad_unhealthy, timeout=5.0, description="bad sink marked open failed"
            )
            assert unhealthy
            assert "open failed" in ((await app.runtime.health())["bad_file"].message or "")

            # ---- 隔离：好 sink 持续出数，采集主循环不受影响 ----
            await _wait_archive_rows(good_root, min_rows=3, timeout=10.0)
            snap = app.metrics.snapshot()
            assert (await app.runtime.health())["good_file"].healthy is True

            # ---- 恢复：修复权限 + 触发 sink 重建（retention_days 变更） ----
            readonly_dir.chmod(stat.S_IRWXU)  # 0o700

            def mutate(data: dict[str, Any]) -> None:
                data["sinks"]["bad_file"]["connection"]["retention_days"] = 5

            update_yaml(ctx.config_dir, SINKS, mutate)
            result = await app.config.reload()
            assert result.success, result.errors

            async def _bad_healthy() -> bool | None:
                return True if (await app.runtime.health())["bad_file"].healthy else None

            reopened: bool = await wait_until(
                _bad_healthy, timeout=5.0, description="bad sink reopened after fix"
            )
            assert reopened
            await _wait_archive_rows(bad_root, min_rows=2, timeout=10.0)

            # 好 sink 全程无中断。
            assert (
                app.metrics.snapshot()["counters"]["acquisition_runs"]
                > snap["counters"]["acquisition_runs"]
            )
        finally:
            # 用例失败也要恢复权限，否则 tmp_path 清理报 EACCES。
            readonly_dir.chmod(stat.S_IRWXU)

    @pytest.mark.mock_service
    async def test_enospc_mid_run_marks_unhealthy_and_isolates(
        self, app_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """运行期注入 ENOSPC：坏 sink unhealthy，好 sink 不受影响。"""
        good_root = tmp_path / "good"
        bad_root = tmp_path / "bad"
        original_write = FileSink._write_task

        def fail_bad_file(self: FileSink, task_id: str, batch: list[PointValue]) -> None:
            if self._root == bad_root:
                raise OSError(28, "No space left on device")
            original_write(self, task_id, batch)

        monkeypatch.setattr(FileSink, "_write_task", fail_bad_file)
        ctx = await app_factory(
            sinks=_two_file_sinks(bad_root, good_root),
            tasks=_task_both_sinks(),
        )
        await ctx.start_instances()
        app = ctx.app

        # 故障前：好 sink 正常出数。
        await _wait_archive_rows(good_root, min_rows=2, timeout=10.0)

        # ---- 故障被检测：连续写失败越阈，unhealthy 且错误消息可见 ENOSPC ----
        async def _full_unhealthy() -> bool | None:
            return True if not (await app.runtime.health())["bad_file"].healthy else None

        full_unhealthy: bool = await wait_until(
            _full_unhealthy,
            timeout=_UNHEALTHY_TIMEOUT,
            description="ENOSPC sink marked unhealthy",
        )
        assert full_unhealthy
        message = (await app.runtime.health())["bad_file"].message or ""
        assert "No space" in message, f"错误消息应暴露 ENOSPC 根因: {message}"

        # ---- 隔离：坏 sink 持续失败期间，好 sink 与采集主循环不受影响 ----
        rows_before = len(_archive_rows(good_root))
        runs_before = app.metrics.snapshot()["counters"]["acquisition_runs"]
        await asyncio.sleep(2.0)
        await _wait_archive_rows(good_root, min_rows=rows_before + 2, timeout=5.0)
        runs_after = app.metrics.snapshot()["counters"]["acquisition_runs"]
        assert runs_after - runs_before >= 5
        assert (await app.runtime.health())["good_file"].healthy is True
        assert (await app.runtime.health())["modbus-1"].healthy is True
