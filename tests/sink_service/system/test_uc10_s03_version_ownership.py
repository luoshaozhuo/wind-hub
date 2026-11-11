"""§14.4 UC-10-S03 版本冲突 / 旧资源所有权系统测试（真实三进程，无 Mock）。

- 不支持的 ``schema_version`` 必须 fail-closed：进程非零退出并留下明确
  配置错误日志，绝不带错运行；
- deployment 身份变化后新实例创建新 Durable，旧 Durable 作为 JetStream
  服务端资源保留且不被新实例接管或删除：两个 ``WH_SINK_`` Durable 并存，
  新实例只向自己的 Durable 投递，旧 Durable 身份在整个切换中不变。
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path
from typing import Any

import nats
import pytest
import yaml

from tests.sink_service._stage1_fixtures import SERVICE_FILE
from tests.sink_service.system.conftest import RELIABLE_DELIVERY, SinkSystemHarness
from tests.support.process import start_sink_service

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


async def _list_sink_durables(harness: SinkSystemHarness) -> dict[str, int | None]:
    """JetStream 服务端真值：WH_SINK_ Durable 名 -> num_pending。"""
    client = await nats.connect(servers=harness.stack.nats, connect_timeout=10)
    try:
        js = client.jetstream()
        infos = await js.consumers_info(harness.history_stream)
        return {info.name: info.num_pending for info in infos if info.name.startswith("WH_SINK_")}
    finally:
        await client.drain()


def _expected_durable(deployment_id: str) -> str:
    """与 reliable_runtime._durable_id 同一公式，双 Durable 场景下不能靠列举顺序。"""
    seed = f"{deployment_id}:{RELIABLE_DELIVERY}".encode()
    return "WH_SINK_" + hashlib.sha256(seed).hexdigest()[:24]


async def _consumer_info(harness: SinkSystemHarness, durable: str) -> Any:
    client = await nats.connect(servers=harness.stack.nats, connect_timeout=10)
    try:
        return await client.jetstream().consumer_info(harness.history_stream, durable)
    finally:
        await client.drain()


class TestUC10S03:
    async def test_unsupported_schema_version_fails_closed(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """schema_version=2 是不支持的配置世代，必须拒绝启动。"""
        sink_harness.write_dual_channel_root()
        await sink_harness.provision_streams()
        service_path = sink_harness.root / SERVICE_FILE
        doc = yaml.safe_load(service_path.read_text(encoding="utf-8"))
        doc["schema_version"] = 2
        service_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

        proc = start_sink_service(
            sink_harness.root, grpc_port=sink_harness.grpc_port, log_dir=sink_harness.root
        )
        try:
            code = await _wait_process_exit(proc)
            assert code != 0, "unsupported schema_version must be rejected"
            assert "unsupported schema_version" in proc.read_log()
        finally:
            proc.kill_tree()
            proc.close_log()

    async def test_old_durable_preserved_after_deployment_change(
        self, sink_harness: SinkSystemHarness, tmp_path: Path
    ) -> None:
        """deployment 切换：旧 Durable 保留不被接管，新实例只用新 Durable 交付。"""
        sink_harness.write_dual_channel_root(ack_wait_seconds=10)
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            for seq in range(2):
                await sink_harness.publish_reliable(
                    sink_harness.make_batch("reliable", seq=seq, points=20)
                )
            records = await sink_harness.wait_kafka_messages(min_messages=40, timeout=45)
            assert len(records) >= 40
            deployment_a = f"qa-{sink_harness.token}"
            durable_a = _expected_durable(deployment_a)
            # 公式必须与服务端真值一致，否则后续身份断言失去意义。
            assert durable_a in await _list_sink_durables(sink_harness)
            assert await sink_harness.stop() == 0
        finally:
            if sink_harness.process is not None:
                await sink_harness.stop()

        # 切换 deployment 身份：复制配置根，只改 deployment_id。
        root_b = tmp_path / "sink-root-b"
        shutil.copytree(sink_harness.root, root_b)
        service_b = root_b / SERVICE_FILE
        doc = yaml.safe_load(service_b.read_text(encoding="utf-8"))
        doc["deployment_id"] = f"qa-{sink_harness.token}-v2"
        # main.py 校验 Service 文档内 config_root 必须与运行根一致。
        doc["config_root"] = str(root_b)
        service_b.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        # 清除复制来的生成存储，强制 B 从编辑后的原始文档重新提交一代；
        # 否则 CURRENT 指向的旧快照仍带 A 的 config_root/deployment。
        shutil.rmtree(root_b / ".generations")
        (root_b / "CURRENT").unlink()

        # 第二套 harness 视图复用同一栈与流，但配置根/进程独立。
        harness_b = SinkSystemHarness(
            root=root_b,
            stack=sink_harness.stack,
            grpc_port=sink_harness.grpc_port,
            namespace=sink_harness.namespace,
            kafka_topic=sink_harness.kafka_topic,
            history_stream=sink_harness.history_stream,
            dlq_stream=sink_harness.dlq_stream,
            dlq_subject=sink_harness.dlq_subject,
            token=f"{sink_harness.token}b",
            bundle=sink_harness.bundle,
        )
        try:
            await harness_b.start()
            try:
                durable_b = _expected_durable(f"qa-{sink_harness.token}-v2")
                assert durable_b != durable_a, "deployment 身份不同必须派生不同 Durable"

                # 服务端真值：两个 Durable 并存，旧 Durable 未被删除。
                durables = await _list_sink_durables(harness_b)
                assert durable_a in durables, f"old durable missing: {durables}"
                assert durable_b in durables, f"new durable missing: {durables}"

                # 新实例只经自己的 Durable 继续交付，Kafka 侧持续可达。
                for seq in range(10, 12):
                    await harness_b.publish_reliable(
                        harness_b.make_batch("reliable", seq=seq, points=20)
                    )
                more = await harness_b.wait_kafka_messages(min_messages=80, timeout=45)
                assert len(more) >= 80
                # 切换后无重投：新 Durable 对新消息只投递一次。
                info_b = await _consumer_info(harness_b, durable_b)
                assert info_b.num_redelivered == 0
                # 旧 Durable 仍在，且身份没有被新实例改写。
                assert durable_a in await _list_sink_durables(harness_b)
            finally:
                await harness_b.stop()
        finally:
            if harness_b.process is not None:
                await harness_b.stop()
