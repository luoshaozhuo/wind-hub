"""UC-01/02 组件级：Runtime 生命周期、单活动所有权、失败与回收。

OutputPort 使用进程内 fake（组件层允许）；真实 Gateway 链路在 integration/system
层验证。Settings 仍由真实 Core configs 树经 load_settings 解析。
"""

from __future__ import annotations

import asyncio
from collections import deque

import pytest

from data_generator.application.runtime import Runtime
from data_generator.domain.model import DataBatch, DeliveryResult, OutputTarget
from data_generator.infrastructure.config.service_settings import load_settings
from data_generator.infrastructure.persistence.snapshots import recover_transaction

from ..conftest import ship_scenarios_document

pytestmark = [pytest.mark.component, pytest.mark.reliability, pytest.mark.lifecycle]


class FakeWriter:
    def __init__(self, *, fail: bool = False, block: float = 0.0) -> None:
        self.unsafe_gateways: frozenset[str] = frozenset()
        self.unsettled_gateways: frozenset[str] = frozenset()
        self.rpc_durations: deque[tuple[float, str, float]] = deque(maxlen=1024)
        self.native_started: dict[str, int] = {}
        self.owners: set[str] = set()
        self.sent = 0
        self._fail = fail
        self._block = block

    def has_claims(self, owner_id: str) -> bool:
        return owner_id in self.owners

    async def claim(self, scenario, owner_id: str) -> None:  # noqa: ANN001
        self.owners.add(owner_id)

    async def release(self, scenario, owner_id: str) -> bool:  # noqa: ANN001
        self.owners.discard(owner_id)
        return True

    async def validate(self, scenario) -> None:  # noqa: ANN001
        return None

    async def send(self, scenario, batch: DataBatch):  # noqa: ANN001
        if self._block:
            await asyncio.sleep(self._block)
        self.sent += 1
        if self._fail:
            raise RuntimeError("forced output failure")
        return [
            DeliveryResult(OutputTarget("gw-test", "GW01.pitch_angle"), "SUCCESS")
            for _ in batch.points
        ]

    async def wait_settled(self, gateway_ids, timeout: float) -> bool:  # noqa: ANN001
        return True

    async def drain(self) -> None:
        return None

    async def close(self) -> bool:
        return True


def _settings(service_path):  # noqa: ANN001, ANN202
    recover_transaction(service_path.absolute().parent.parent)
    return load_settings(service_path)


async def test_start_unknown_scenario_and_version_mismatch(configs_root) -> None:  # noqa: ANN001
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-test": "127.0.0.1:59999"}, mappings=[("gw-test", "GW01.pitch_angle")]
        ),
        bind="127.0.0.1:0",
    )
    runtime = Runtime(_settings(service_path), FakeWriter())
    with pytest.raises(Exception, match="scenario not found"):
        await runtime.start("missing", None, runtime.settings.revision)
    with pytest.raises(Exception, match="version unavailable"):
        await runtime.start("demo-random", "9.9.9", runtime.settings.revision)


async def test_revision_mismatch_rejects_start(configs_root) -> None:  # noqa: ANN001
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-test": "127.0.0.1:59999"}, mappings=[("gw-test", "GW01.pitch_angle")]
        ),
        bind="127.0.0.1:0",
    )
    runtime = Runtime(_settings(service_path), FakeWriter())
    with pytest.raises(Exception, match="expected_revision"):
        await runtime.start("demo-random", None, "deadbeef")


async def test_single_active_generation_and_completion(configs_root) -> None:  # noqa: ANN001
    # 3 条记录的真实 CSV 重放：源自然耗尽后进入 COMPLETED，随后可再次启动。
    service_path = configs_root(
        scenarios={
            "schema_version": 1,
            "file_version": "1.0.0",
            "scenarios": [
                {
                    "scenario_id": "replay-finish",
                    "version": "1.0.0",
                    "source": {
                        "kind": "replay",
                        "history_reader": "csv",
                        "file": "history/short.csv",
                        "start_time": "2026-01-01T00:00:00Z",
                        "end_time": "2026-01-01T01:00:00Z",
                        "speed": 1000.0,
                        "loop": False,
                    },
                    "devices": [
                        {"device_id": "GW02", "points": [{"point_id": "a02_dm_pitchangle1"}]}
                    ],
                    "gateways": [
                        {
                            "gateway_id": "gw-test",
                            "endpoint": "127.0.0.1:59999",
                            "device_id": "GW02",
                        }
                    ],
                }
            ],
        },
        history={
            "short.csv": [
                "GW02,a02_dm_pitchangle1,2026-01-01T00:00:00Z,1.0,GOOD,r1",
                "GW02,a02_dm_pitchangle1,2026-01-01T00:00:01Z,2.0,GOOD,r2",
                "GW02,a02_dm_pitchangle1,2026-01-01T00:00:02Z,3.0,GOOD,r3",
            ]
        },
        bind="127.0.0.1:0",
        output_timeout=1.0,
    )
    writer = FakeWriter()
    runtime = Runtime(_settings(service_path), writer)
    first = await runtime.start("replay-finish", None, runtime.settings.revision)
    await asyncio.sleep(0)
    state = await runtime.get_generation(first.generation_id)
    assert state is not None
    assert state.actual_state in ("RUNNING", "COMPLETED")
    # 第二实例并发占用必须被拒绝。
    with pytest.raises(Exception, match="ownership retained"):
        await runtime.start("replay-finish", None, runtime.settings.revision)
    for _ in range(100):
        await asyncio.sleep(0.02)
        settled = await runtime.get_generation(first.generation_id)
        if settled is not None and settled.actual_state == "COMPLETED":
            break
    assert settled is not None
    assert settled.actual_state == "COMPLETED"
    assert writer.sent == 3
    # 源回收后允许新实例。
    second = await runtime.start("replay-finish", None, runtime.settings.revision)
    assert second.generation_id != first.generation_id
    await runtime.stop(second.generation_id, "")
    await runtime.shutdown()


async def test_failed_output_marks_generation_failed(configs_root) -> None:  # noqa: ANN001
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-test": "127.0.0.1:59999"},
            interval=0.05,
            mappings=[("gw-test", "GW01.pitch_angle")],
        ),
        bind="127.0.0.1:0",
    )
    runtime = Runtime(_settings(service_path), FakeWriter(fail=True))
    generation = await runtime.start("demo-random", None, runtime.settings.revision)
    for _ in range(50):
        await asyncio.sleep(0.02)
        state = await runtime.get_generation(generation.generation_id)
        if state is not None and state.actual_state == "FAILED":
            break
    assert state is not None
    assert state.actual_state == "FAILED"
    assert "forced output failure" in state.last_error


async def test_overrun_skips_periods_without_reentrant_emits(configs_root) -> None:  # noqa: ANN001
    # interval=0.05，但每批输出阻塞 0.18s：应跳期且不重入，实际批次数远少于墙钟步数。
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-test": "127.0.0.1:59999"},
            interval=0.05,
            mappings=[("gw-test", "GW01.pitch_angle")],
        ),
        bind="127.0.0.1:0",
    )
    writer = FakeWriter(block=0.18)
    runtime = Runtime(_settings(service_path), writer)
    generation = await runtime.start("demo-random", None, runtime.settings.revision)
    await asyncio.sleep(0.55)
    await runtime.stop(generation.generation_id, "")
    assert runtime.counters["skipped_steps"] >= 1
    assert writer.sent <= 4
