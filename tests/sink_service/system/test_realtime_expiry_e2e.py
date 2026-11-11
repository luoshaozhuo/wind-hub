"""§14.5 实时过期与重启不补发系统测试（S3-10，真实 NATS/Redis/三进程）。

设计 14.5 节专项 ``test_realtime_expiry_e2e``：实时数据按样本
``max_staleness_ms`` 设置 Redis TTL，过期后键必须消失；Main 进程重启
不得从任何通道补发旧实时点（实时链路本就无持久化缓存，重启后键集合
只能保持或因 TTL 缩小，不能增长）。

为控制用例时长，模板 ``max_age_ms`` 与样本 ``max_staleness_ms`` 统一
收紧到 5 s；被测的仍是生产 TTL/过期代码路径，不改变任何生产逻辑。
"""

from __future__ import annotations

import asyncio

import pytest

from tests.sink_service.system.conftest import SinkSystemHarness

pytestmark = [
    pytest.mark.system,
    pytest.mark.reliability,
    pytest.mark.docker,
    pytest.mark.real_service,
    pytest.mark.slow,
    pytest.mark.redis,
    pytest.mark.nats,
    pytest.mark.exception,
    pytest.mark.recovery,
]

MARKER_DEVICE = "WT099"
SHORT_STALENESS_MS = 5_000


class TestRealtimeExpiryAndRestart:
    async def test_s3_10_ttl_expiry_and_no_replay_after_main_restart(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        sink_harness.staleness_ms = SHORT_STALENESS_MS
        sink_harness.write_dual_channel_root(reliable=False)
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            batches = [
                sink_harness.make_batch("realtime", seq, 10, device_id=MARKER_DEVICE)
                for seq in range(3)
            ]
            await sink_harness.publish_realtime(batches, interval=0.1)

            pattern = f"{sink_harness.namespace}:*:{MARKER_DEVICE}:*"

            async def _keys_present() -> list[str]:
                deadline = asyncio.get_running_loop().time() + 20
                while asyncio.get_running_loop().time() < deadline:
                    keys = await sink_harness.redis_keys(pattern)
                    if keys:
                        return keys
                    await asyncio.sleep(0.3)
                raise AssertionError("marker device never appeared in Redis")

            keys_after_publish = sorted(await _keys_present())
            ttl = await sink_harness.redis_ttl(keys_after_publish[0])
            assert 0 < ttl <= SHORT_STALENESS_MS + 1_000

            # 同配置根重启 Main：无新流量，实时键集合不得增长（无补发）。
            await sink_harness.stop()
            await sink_harness.start()
            try:
                await asyncio.sleep(2.0)
                keys_after_restart = sorted(await sink_harness.redis_keys(pattern))
                assert keys_after_restart <= keys_after_publish, (
                    "Main restart replayed expired/realtime points: "
                    f"{keys_after_restart} vs {keys_after_publish}"
                )

                # TTL 到期后全部 marker 键消失。
                expire_deadline = asyncio.get_running_loop().time() + 12.0
                while asyncio.get_running_loop().time() < expire_deadline:
                    if not await sink_harness.redis_keys(pattern):
                        return
                    await asyncio.sleep(0.5)
                raise AssertionError("realtime keys survived past their staleness TTL")
            finally:
                await sink_harness.stop()
        finally:
            if sink_harness.process is not None:
                await sink_harness.stop()
