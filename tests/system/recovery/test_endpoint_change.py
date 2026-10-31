"""Recovery：在线/掉线期间设备 endpoint（host/port）迁移的真实链路验证。

与 component 层 endpoint 变更测试的区别：这里 Collector 是独立 subprocess，
endpoint 迁移经 v2 Config RPC（devices 文档整文档替换 + 运行时收敛）下发，
与生产控制路径一致；观测全部在系统边界——Redis 最新值的点值来源
（A=1200.5 / B=1500.5）、gRPC Runtime status 的 ``device_connected``。

两个对端都是真实 Modbus 从站：A 与 B 寄存器值不同，Redis 点值直接
证明「当前连接的是哪一台」。host 变化用 loopback 别名 127.0.0.2 构造
（Linux 整个 127/8 均为本地回环，无需额外网络配置）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from contracts import collector_v2_pb2 as pb
from contracts import collector_v2_pb2_grpc as pb_grpc
from tests.fixtures.servers.modbus_server import ModbusMockServer, _holding_registers
from tests.support.control import (
    _channel,
    _metadata,
    start_task_instance,
)
from tests.support.process import CollectorProcess, free_port
from tests.support.recovery_helpers import (
    DEVICE_ID,
    recovery_collector_tree,
    wait_status,
    wait_telemetry_value,
    write_modbus_redis_config,
)

pytestmark = [pytest.mark.modbus, pytest.mark.redis, pytest.mark.real_service]

# v2：task_id == device_id == instance_id。
TASK_ID = DEVICE_ID
INSTANCE_ID = DEVICE_ID

VALUE_A = 1200.5
VALUE_B = 1500.5

#: 无监听者的 loopback 地址：connect 立即 ECONNREFUSED，不引入超时等待。
DEAD_HOST = "127.0.0.2"


async def _switch_endpoint(
    proc: CollectorProcess,
    v2_tree: Path,
    *,
    host: str,
    port: int,
) -> pb.ConfigMutationResponse:
    """改写 v2 devices 文档中唯一设备的 endpoint 并经 ReplaceConfig 收敛。"""
    devices_path = v2_tree / "instances" / "devices.yaml"
    devices_doc: dict[str, Any] = yaml.safe_load(devices_path.read_text(encoding="utf-8"))
    endpoint = devices_doc["devices"][DEVICE_ID]["endpoint"]
    endpoint["host"] = host
    endpoint["port"] = port
    devices_path.write_text(
        yaml.safe_dump(devices_doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    # 读取 devices 文档当前 revision 作为乐观版本。
    async with _channel(proc.grpc_target) as channel:
        stub = pb_grpc.CollectorConfigServiceStub(channel)
        current = await stub.GetConfig(
            pb.GetConfigRequest(selector=pb.ConfigSelector(scope=pb.INSTANCE, name="devices")),
            metadata=_metadata(None),
            timeout=5.0,
        )
        return await stub.ReplaceConfig(
            pb.ReplaceConfigRequest(
                selector=pb.ConfigSelector(scope=pb.INSTANCE, name="devices"),
                expected_revision=current.revision,
                request_id=f"endpoint-{host}-{port}-{time.monotonic_ns()}",
                yaml_content=yaml.safe_dump(devices_doc, allow_unicode=True, sort_keys=False),
            ),
            metadata=_metadata(None),
            timeout=15.0,
        )


async def _wait_value(redis_address: str, expected: float) -> dict[str, Any]:
    return await wait_telemetry_value(
        redis_address,
        match=lambda payload: payload["value"] == pytest.approx(expected),
    )


async def _assert_stays_on_b(redis_address: str) -> None:
    observed = await _wait_value(redis_address, VALUE_B)
    latest = await wait_telemetry_value(redis_address, after=str(observed["timestamp"]))
    assert latest["value"] == pytest.approx(VALUE_B)


async def _start_server_b(*, host: str = "127.0.0.1") -> ModbusMockServer:
    server = ModbusMockServer(
        port=free_port(), host=host, holding=_holding_registers(VALUE_B, VALUE_B)
    )
    await server.start()
    return server


def _trees(tmp_path: Path, modbus_port: int, redis_service: str, nats_service: str):
    flat_dir = write_modbus_redis_config(
        tmp_path / "cfg", modbus_port, redis_service, nats_url=nats_service
    )
    v2_tree = recovery_collector_tree(flat_dir, tmp_path / "v2", nats_url=nats_service)
    return flat_dir, v2_tree


class TestEndpointMigration:
    async def test_endpoint_port_change_switches_data_source(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        """DEV-03a：在线迁移 port——Sink 出现 B 值后不再读到 A，连接数保持 1。"""
        flat_dir, v2_tree = _trees(tmp_path, modbus_server.port, redis_service, nats_service)
        await sink_service_factory(flat_dir)
        proc: CollectorProcess = await collector_factory(v2_tree)
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)
        await _wait_value(redis_service, VALUE_A)

        server_b = await _start_server_b()
        try:
            activated = await _switch_endpoint(proc, v2_tree, host="127.0.0.1", port=server_b.port)
            assert activated.success, f"activate failed: {list(activated.errors)}"

            # 新值出现 + 设备保持已连接（迁移不允许表现为长期掉线）。
            await _wait_value(redis_service, VALUE_B)
            await wait_status(proc, lambda p: p["device_connected"] == 1, description="device on B")

            # 新连接连续提供 B 值，不应恢复旧 A 值。
            await _assert_stays_on_b(redis_service)
        finally:
            await server_b.stop()

    async def test_endpoint_host_change_switches_data_source(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        """DEV-03b：在线迁移 host（127.0.0.1 → 127.0.0.2）——真实修改 host 字段。"""
        flat_dir, v2_tree = _trees(tmp_path, modbus_server.port, redis_service, nats_service)
        await sink_service_factory(flat_dir)
        proc: CollectorProcess = await collector_factory(v2_tree)
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)
        await _wait_value(redis_service, VALUE_A)

        server_b = await _start_server_b(host="127.0.0.2")
        try:
            activated = await _switch_endpoint(proc, v2_tree, host="127.0.0.2", port=server_b.port)
            assert activated.success, f"activate failed: {list(activated.errors)}"

            await _wait_value(redis_service, VALUE_B)
            await _assert_stays_on_b(redis_service)
        finally:
            await server_b.stop()

    async def test_endpoint_change_while_offline(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        """DEV-04：掉线期间迁移 endpoint——A 不恢复，替换后直连 B 并出数。"""
        flat_dir, v2_tree = _trees(tmp_path, modbus_server.port, redis_service, nats_service)
        await sink_service_factory(flat_dir)
        proc: CollectorProcess = await collector_factory(v2_tree)
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)
        await wait_telemetry_value(redis_service)

        await modbus_server.stop()
        await wait_status(proc, lambda p: p["device_connected"] == 0, description="A disconnected")

        server_b = await _start_server_b()
        try:
            activated = await _switch_endpoint(proc, v2_tree, host="127.0.0.1", port=server_b.port)
            assert activated.success, f"activate failed: {list(activated.errors)}"

            # 恢复路径是「连上 B」，不是「A 复活」——A 全程处于停止状态。
            await wait_status(
                proc, lambda p: p["device_connected"] == 1, description="connected to B"
            )
            value = await _wait_value(redis_service, VALUE_B)
            assert value["quality"] == "good"
        finally:
            await server_b.stop()

    async def test_bad_endpoint_then_recover(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        """DEV-05：迁移到不可达 endpoint——不得残留假 healthy，修正后恢复。"""
        flat_dir, v2_tree = _trees(tmp_path, modbus_server.port, redis_service, nats_service)
        await sink_service_factory(flat_dir)
        proc: CollectorProcess = await collector_factory(v2_tree)
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)
        await wait_telemetry_value(redis_service)

        # ---- 切到无监听者的地址（connect 立即被拒绝） ----
        activated = await _switch_endpoint(proc, v2_tree, host=DEAD_HOST, port=free_port())
        assert activated.success, f"activate failed: {list(activated.errors)}"

        # 配置已生效但设备必须如实报告离线——旧连接不得残留为假 healthy。
        await wait_status(
            proc,
            lambda p: p["device_connected"] == 0,
            description="device offline after bad endpoint",
        )
        assert proc.is_running()

        # ---- 修正到 B，再次替换配置恢复 ----
        server_b = await _start_server_b()
        try:
            activated = await _switch_endpoint(proc, v2_tree, host="127.0.0.1", port=server_b.port)
            assert activated.success, f"activate failed: {list(activated.errors)}"
            await wait_status(
                proc, lambda p: p["device_connected"] == 1, description="recovered on B"
            )
            await _wait_value(redis_service, VALUE_B)
        finally:
            await server_b.stop()


class TestReloadDuringReconnect:
    async def test_reload_endpoint_during_backoff_connects_promptly(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        """CFG-01：Watchdog 恢复期间迁移 endpoint——旧会话不得阻塞新设备连接。

        A 掉线约 12s 后替换到 B：新会话应立即尝试连接（<5s），
        不受旧 Watchdog 及旧状态影响。最后重启 A，验证旧
        协议实例不会复活（不再出现 A 值）。
        """
        flat_dir, v2_tree = _trees(tmp_path, modbus_server.port, redis_service, nats_service)
        await sink_service_factory(flat_dir)
        proc: CollectorProcess = await collector_factory(v2_tree)
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)
        await wait_telemetry_value(redis_service)

        await modbus_server.stop()
        await wait_status(proc, lambda p: p["device_connected"] == 0, description="A disconnected")
        # 等退避爬升：尝试时刻约为 0/1/3/7s，12s 后下一档 ≥8s。
        await asyncio.sleep(12.0)

        server_b = await _start_server_b()
        try:
            switched_at = time.monotonic()
            activated = await _switch_endpoint(proc, v2_tree, host="127.0.0.1", port=server_b.port)
            assert activated.success, f"activate failed: {list(activated.errors)}"
            await wait_status(
                proc,
                lambda p: p["device_connected"] == 1,
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
            await _assert_stays_on_b(redis_service)
            await _wait_value(redis_service, VALUE_B)
        finally:
            await server_b.stop()
