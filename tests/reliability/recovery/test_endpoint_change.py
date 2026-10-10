"""Recovery：在线/掉线期间设备 endpoint（host/port）迁移的真实链路验证。

与 component 层 ``test_endpoint_port_change_reconnects_to_new_peer`` 的区别：
这里的 Collector 是独立 subprocess，reload 经 Prepare/Activate 两段事务
RPC 下发（与生产路径一致），观测全部在系统边界——sink 文件中的点值来源
（A=1200.5 / B=1500.5）、ctl status 的 ``devices_connected``。

两个对端都是真实 Modbus 从站：A 与 B 寄存器值不同，sink 行里的值直接
证明「当前连接的是哪一台」。host 变化用 loopback 别名 127.0.0.2 构造
（Linux 整个 127/8 均为本地回环，无需额外网络配置）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer, _holding_registers
from tests.reliability.recovery.helpers import wait_status, write_modbus_file_config
from tests.support.control import apply_placement_and_start_instance, reload_config
from tests.support.functional_config import update_yaml
from tests.support.process import CollectorProcess, free_port
from tests.support.wait import read_jsonl, wait_file_rows

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

TASK_ID = "modbus-telemetry"
INSTANCE_ID = "modbus-telemetry:modbus-1"

VALUE_A = 1200.5
VALUE_B = 1500.5

#: 无监听者的 loopback 地址：connect 立即 ECONNREFUSED，不引入超时等待。
DEAD_HOST = "127.0.0.2"


def _set_endpoint(config_dir: Path, *, host: str, port: int) -> None:
    """改写 devices.yaml 中唯一设备的 endpoint（reload 的输入）。"""

    def _mutate(data: dict) -> None:
        endpoint = data["devices"][0]["endpoint"]
        endpoint["host"] = host
        endpoint["port"] = port

    update_yaml(config_dir, "devices.yaml", _mutate)


def _rows_with_value(sink_path: Path, value: float) -> list[dict]:
    return [
        r
        for r in read_jsonl(sink_path)
        if r["point_id"] == "rotor.speed" and r["value"] == pytest.approx(value)
    ]


async def _start_server_b(*, host: str = "127.0.0.1") -> ModbusMockServer:
    server = ModbusMockServer(
        port=free_port(), host=host, holding=_holding_registers(VALUE_B, VALUE_B)
    )
    await server.start()
    return server


class TestEndpointMigration:
    async def test_endpoint_port_change_switches_data_source(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        """DEV-03a：在线迁移 port——Sink 出现 B 值后不再读到 A，连接数保持 1。"""
        sink_path = tmp_path / "out" / "telemetry.jsonl"
        config_dir = write_modbus_file_config(tmp_path / "cfg", modbus_server.port, sink_path)
        proc: CollectorProcess = await collector_factory(config_dir)
        await apply_placement_and_start_instance(
            proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
        )
        await wait_file_rows(
            sink_path, min_rows=2, match=lambda r: r["value"] == pytest.approx(VALUE_A)
        )

        server_b = await _start_server_b()
        try:
            _set_endpoint(config_dir, host="127.0.0.1", port=server_b.port)
            activated = await reload_config(
                proc.grpc_target, config_dir=config_dir, revision_id="rev-port-b"
            )
            assert activated.success, f"activate failed: {list(activated.errors)}"

            # 新值出现 + 设备保持已连接（迁移不允许表现为长期掉线）。
            await wait_file_rows(
                sink_path, min_rows=2, match=lambda r: r["value"] == pytest.approx(VALUE_B)
            )
            await wait_status(
                proc, lambda p: p["devices_connected"] == 1, description="device on B"
            )

            # 旧连接已关闭：A 仍在运行，但稳定窗口内不再产生 A 值行。
            await asyncio.sleep(1.0)
            settled_a = len(_rows_with_value(sink_path, VALUE_A))
            await asyncio.sleep(1.0)
            assert len(_rows_with_value(sink_path, VALUE_A)) == settled_a
        finally:
            await server_b.stop()

    async def test_endpoint_host_change_switches_data_source(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        """DEV-03b：在线迁移 host（127.0.0.1 → 127.0.0.2）——真实修改 host 字段。"""
        sink_path = tmp_path / "out" / "telemetry.jsonl"
        config_dir = write_modbus_file_config(tmp_path / "cfg", modbus_server.port, sink_path)
        proc: CollectorProcess = await collector_factory(config_dir)
        await apply_placement_and_start_instance(
            proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
        )
        await wait_file_rows(
            sink_path, min_rows=2, match=lambda r: r["value"] == pytest.approx(VALUE_A)
        )

        server_b = await _start_server_b(host="127.0.0.2")
        try:
            _set_endpoint(config_dir, host="127.0.0.2", port=server_b.port)
            activated = await reload_config(
                proc.grpc_target, config_dir=config_dir, revision_id="rev-host-b"
            )
            assert activated.success, f"activate failed: {list(activated.errors)}"

            await wait_file_rows(
                sink_path, min_rows=2, match=lambda r: r["value"] == pytest.approx(VALUE_B)
            )
            await asyncio.sleep(1.0)
            settled_a = len(_rows_with_value(sink_path, VALUE_A))
            await asyncio.sleep(1.0)
            assert len(_rows_with_value(sink_path, VALUE_A)) == settled_a
        finally:
            await server_b.stop()

    async def test_endpoint_change_while_offline(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        """DEV-04：掉线期间迁移 endpoint——A 不恢复，reload 后直连 B 并出数。"""
        sink_path = tmp_path / "out" / "telemetry.jsonl"
        config_dir = write_modbus_file_config(tmp_path / "cfg", modbus_server.port, sink_path)
        proc: CollectorProcess = await collector_factory(config_dir)
        await apply_placement_and_start_instance(
            proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
        )
        await wait_file_rows(sink_path, min_rows=2)

        await modbus_server.stop()
        await wait_status(
            proc, lambda p: p["devices_connected"] == 0, description="A disconnected"
        )

        server_b = await _start_server_b()
        try:
            _set_endpoint(config_dir, host="127.0.0.1", port=server_b.port)
            activated = await reload_config(
                proc.grpc_target, config_dir=config_dir, revision_id="rev-offline-b"
            )
            assert activated.success, f"activate failed: {list(activated.errors)}"

            # 恢复路径是「连上 B」，不是「A 复活」——A 全程处于停止状态。
            await wait_status(
                proc, lambda p: p["devices_connected"] == 1, description="connected to B"
            )
            rows = await wait_file_rows(
                sink_path, min_rows=2, match=lambda r: r["value"] == pytest.approx(VALUE_B)
            )
            assert all(r["point_id"] for r in rows)
        finally:
            await server_b.stop()

    async def test_bad_endpoint_then_recover(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        """DEV-05：迁移到不可达 endpoint——不得残留假 healthy，修正后恢复。"""
        sink_path = tmp_path / "out" / "telemetry.jsonl"
        config_dir = write_modbus_file_config(tmp_path / "cfg", modbus_server.port, sink_path)
        proc: CollectorProcess = await collector_factory(config_dir)
        await apply_placement_and_start_instance(
            proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
        )
        await wait_file_rows(sink_path, min_rows=2)

        # ---- 切到无监听者的地址（connect 立即被拒绝） ----
        _set_endpoint(config_dir, host=DEAD_HOST, port=free_port())
        activated = await reload_config(
            proc.grpc_target, config_dir=config_dir, revision_id="rev-dead"
        )
        assert activated.success, f"activate failed: {list(activated.errors)}"

        # 配置已生效但设备必须如实报告离线——旧连接不得残留为假 healthy。
        await wait_status(
            proc,
            lambda p: p["devices_connected"] == 0,
            description="device offline after bad endpoint",
        )
        assert proc.is_running()

        # ---- 修正到 B，再次 reload 恢复 ----
        server_b = await _start_server_b()
        try:
            _set_endpoint(config_dir, host="127.0.0.1", port=server_b.port)
            activated = await reload_config(
                proc.grpc_target, config_dir=config_dir, revision_id="rev-fixed-b"
            )
            assert activated.success, f"activate failed: {list(activated.errors)}"
            await wait_status(
                proc, lambda p: p["devices_connected"] == 1, description="recovered on B"
            )
            await wait_file_rows(
                sink_path, min_rows=2, match=lambda r: r["value"] == pytest.approx(VALUE_B)
            )
        finally:
            await server_b.stop()


class TestReloadDuringReconnect:
    async def test_reload_endpoint_during_backoff_connects_promptly(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        """CFG-01：backoff 中途迁移 endpoint——旧退避状态不得阻塞新设备连接。

        A 掉线约 12s（退避已增长到 ≥8s 档位），此时 reload 到 B：若旧
        DeviceRuntimeState 泄漏到新协议实例，连接会被 next_retry_at 推迟到
        8s 以后；正确行为是重建状态、立即连接（<5s）。最后重启 A，验证旧
        协议实例不会复活（不再出现 A 值）。
        """
        sink_path = tmp_path / "out" / "telemetry.jsonl"
        config_dir = write_modbus_file_config(tmp_path / "cfg", modbus_server.port, sink_path)
        proc: CollectorProcess = await collector_factory(config_dir)
        await apply_placement_and_start_instance(
            proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
        )
        await wait_file_rows(sink_path, min_rows=2)

        await modbus_server.stop()
        await wait_status(
            proc, lambda p: p["devices_connected"] == 0, description="A disconnected"
        )
        # 等退避爬升：尝试时刻约为 0/1/3/7s，12s 后下一档 ≥8s。
        await asyncio.sleep(12.0)

        server_b = await _start_server_b()
        try:
            _set_endpoint(config_dir, host="127.0.0.1", port=server_b.port)
            switched_at = time.monotonic()
            await reload_config(
                proc.grpc_target, config_dir=config_dir, revision_id="rev-during-backoff"
            )
            await wait_status(
                proc,
                lambda p: p["devices_connected"] == 1,
                timeout=15.0,
                description="B connected despite old backoff",
            )
            elapsed = time.monotonic() - switched_at
            assert elapsed < 5.0, (
                f"connect to B took {elapsed:.1f}s — old backoff state leaked into "
                "the rebuilt device"
            )

            # 旧协议实例不得复活：A 恢复后，数据仍只能来自 B。
            await modbus_server.start()
            await asyncio.sleep(1.0)
            settled_a = len(_rows_with_value(sink_path, VALUE_A))
            await asyncio.sleep(1.0)
            assert len(_rows_with_value(sink_path, VALUE_A)) == settled_a
            await wait_file_rows(
                sink_path, min_rows=2, match=lambda r: r["value"] == pytest.approx(VALUE_B)
            )
        finally:
            await server_b.stop()
