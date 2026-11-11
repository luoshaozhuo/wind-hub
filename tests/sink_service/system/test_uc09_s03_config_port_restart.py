"""§14.4 UC-09-S03 非法配置 / 端口冲突 / 重启保护系统测试（真实三进程，无 Mock）。

三个故障面全部在进程与协议边界观察：

- 非法 Service 文档必须 fail-closed：进程非零退出、gRPC 端口不监听、
  绝不带错运行；
- gRPC 端口被占用时第二个实例必须 bind 失败退出，存活实例不受影响，
  禁止双实例静默并存；
- 同配置根冷重启保持单一 JetStream Durable 所有权，Kafka 侧按
  sample_id 去重的记录数等于发布数，重启不产生重复交付或重投。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from contracts import sink_service_pb2, sink_service_pb2_grpc
from tests.sink_service._stage1_fixtures import SERVICE_FILE
from tests.sink_service.system.conftest import (
    RELIABLE_DELIVERY,
    SinkSystemHarness,
)
from tests.support.process import start_sink_service
from tests.support.wait import wait_tcp_port

pytestmark = [
    pytest.mark.system,
    pytest.mark.reliability,
    pytest.mark.docker,
    pytest.mark.real_service,
    pytest.mark.slow,
    pytest.mark.nats,
    pytest.mark.jetstream,
    pytest.mark.kafka,
    pytest.mark.exception,
    pytest.mark.timeout,
    pytest.mark.recovery,
]


async def _wait_process_exit(proc, timeout: float = 30.0) -> int:  # noqa: ANN001
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        code = proc.proc.poll()
        if code is not None:
            return int(code)
        await asyncio.sleep(0.5)
    proc.kill_tree()
    raise AssertionError(f"process did not exit within {timeout}s; log={proc.read_log()[-2000:]}")


async def _delivery_errors(harness: SinkSystemHarness) -> dict[str, str]:
    async with harness.channel() as channel:
        response = await sink_service_pb2_grpc.SinkServiceStub(channel).ListDeliveries(
            sink_service_pb2.ListDeliveriesRequest()
        )
    return {item.delivery_id: item.last_error for item in response.deliveries}


async def _kafka_sample_ids(harness: SinkSystemHarness, min_messages: int) -> set[str]:
    """独立 group、earliest 全量读取，返回 sample_id 集合（跨重启去重观测）。"""
    import json as json_lib
    import uuid

    from aiokafka import AIOKafkaConsumer

    consumer = AIOKafkaConsumer(
        harness.kafka_topic,
        bootstrap_servers=harness.stack.kafka,
        auto_offset_reset="earliest",
        group_id=f"qa-uc09-{uuid.uuid4().hex[:10]}",
        enable_auto_commit=False,
    )
    await consumer.start()
    try:
        records: list[Any] = []
        deadline = asyncio.get_running_loop().time() + 45
        while asyncio.get_running_loop().time() < deadline and len(records) < min_messages:
            result = await consumer.getmany(timeout_ms=2000, max_records=500)
            for messages in result.values():
                records.extend(messages)
        sample_ids = set()
        for record in records:
            payload = json_lib.loads(record.value.decode("utf-8"))
            sample_ids.add(payload["sample_id"])
        return sample_ids
    finally:
        await consumer.stop()


class TestUC09S03:
    async def test_invalid_service_config_fails_closed(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """非法 ack_wait（非正数）必须被生产校验器拒绝，进程非零退出、端口不监听。"""
        sink_harness.write_dual_channel_root(ack_wait_seconds=10)
        await sink_harness.provision_streams()
        service_path = sink_harness.root / SERVICE_FILE
        doc = yaml.safe_load(service_path.read_text(encoding="utf-8"))
        doc["reliable"]["ack_wait_seconds"] = 0
        service_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

        proc = start_sink_service(
            sink_harness.root, grpc_port=sink_harness.grpc_port, log_dir=sink_harness.root
        )
        try:
            code = await _wait_process_exit(proc)
            assert code != 0, "invalid config must not be accepted"
            log = proc.read_log()
            assert (
                "ConfigurationError" in log
                or "invalid positive integer" in log
                or "validation" in log.lower()
            ), log[-2000:]
            # 控制面绝不允许在非法配置上监听。
            with pytest.raises(TimeoutError):
                await wait_tcp_port("127.0.0.1", sink_harness.grpc_port, timeout=5.0)
        finally:
            proc.kill_tree()
            proc.close_log()

    async def test_grpc_port_conflict_second_instance_rejected(
        self, sink_harness: SinkSystemHarness, tmp_path: Path
    ) -> None:
        """同端口第二实例 bind 失败退出；第一实例持续 RUNNING，禁止双所有权。

        生产侧 ``grpc.so_reuseport=0``（grpc_server.py）：C-core 对重复 bind
        直接抛 RuntimeError（日志含 Failed to bind / address already in use），
        或 add_secure_port 返回 0 由守卫转 ConfigurationError，两条路径均
        fail-closed。最小双 server 复现实证见仓库外验收脚本
        reuseport_fix_repro.py（second_rejected=true）。
        """
        sink_harness.write_dual_channel_root()
        await sink_harness.provision_streams()
        proc_a = await sink_harness.start()
        try:
            assert proc_a.is_running()
            # 第二实例：独立配置根 + 不同 deployment（避免 durable 同名），
            # 但故意复用 A 的 gRPC 端口。
            root_b = tmp_path / "sink-root-b"
            shutil.copytree(sink_harness.root, root_b)
            service_b = root_b / SERVICE_FILE
            doc = yaml.safe_load(service_b.read_text(encoding="utf-8"))
            doc["deployment_id"] = f"qa-{sink_harness.token}-conflict"
            # main.py 校验 Service 文档内 config_root 必须与运行根一致。
            doc["config_root"] = str(root_b)
            service_b.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
            # 清除复制来的生成存储，强制 B 从编辑后的原始文档重新提交一代；
            # 否则 CURRENT 指向的旧快照仍带 A 的 config_root/deployment。
            shutil.rmtree(root_b / ".generations")
            (root_b / "CURRENT").unlink()

            proc_b = start_sink_service(root_b, grpc_port=sink_harness.grpc_port, log_dir=root_b)
            try:
                code = await _wait_process_exit(proc_b)
                assert code != 0, "second instance on a busy port must fail"
                log_b = proc_b.read_log().lower()
                assert (
                    "address already in use" in log_b
                    or "failed to bind" in log_b
                    or "cannot bind secure grpc endpoint" in log_b
                ), log_b[-2000:]
            finally:
                proc_b.kill_tree()
                proc_b.close_log()

            # A 实例不受影响：控制面可达且 Delivery 仍 RUNNING。
            assert proc_a.is_running()
            async with sink_harness.channel() as channel:
                response = await sink_service_pb2_grpc.SinkServiceStub(channel).ListDeliveries(
                    sink_service_pb2.ListDeliveriesRequest()
                )
            assert all(item.actual_state == "RUNNING" for item in response.deliveries)
        finally:
            await sink_harness.stop()

    async def test_restart_keeps_single_durable_and_no_duplicate_writes(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """同根冷重启：durable 同名、零重投，Kafka sample_id 全局无重复。"""
        sink_harness.write_dual_channel_root(ack_wait_seconds=10)
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            for seq in range(2):
                await sink_harness.publish_reliable(
                    sink_harness.make_batch("reliable", seq=seq, points=20)
                )
            first_ids = await _kafka_sample_ids(sink_harness, min_messages=40)
            assert len(first_ids) == 40

            durable_before = (await sink_harness.consumer_state())["durable"]
            assert durable_before is not None

            assert await sink_harness.stop() == 0
            await sink_harness.start()

            for seq in range(2, 4):
                await sink_harness.publish_reliable(
                    sink_harness.make_batch("reliable", seq=seq, points=20)
                )
            all_ids = await _kafka_sample_ids(sink_harness, min_messages=80)
            assert len(all_ids) == 80, f"duplicate/missing samples after restart: {len(all_ids)}"
            assert first_ids.issubset(all_ids)

            state = await sink_harness.consumer_state()
            assert state["durable"] == durable_before, "durable identity changed across restart"
            assert state["num_redelivered"] == 0, f"redelivery observed: {state}"
            errors = await _delivery_errors(sink_harness)
            assert errors.get(RELIABLE_DELIVERY, "") == "", errors
        finally:
            await sink_harness.stop()
