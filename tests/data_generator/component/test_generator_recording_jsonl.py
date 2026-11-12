"""Data Generator JSONL 真值录制组件测试（R2 S2 专项）。

录制在交付前捕获源侧批次（不依赖 Gateway ACK），通过真实 Runtime + 进程内
FakeWriter 驱动；配置树仍由仓库真实 Core 模板装配，仅输出端口为测试替身。
覆盖：开关默认关闭、逐批单行 JSONL、时间戳/数据类型/bytes base64 编码、
磁盘写入失败使 Generation 进入 FAILED 且无静默吞错。
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import pytest

from data_generator.application.runtime import Runtime
from data_generator.domain.model import DataBatch, DataPoint, DeliveryResult, OutputTarget
from data_generator.infrastructure.config import load_settings
from data_generator.infrastructure.snapshots import recover_transaction

from ..conftest import ship_scenarios_document

pytestmark = [pytest.mark.component, pytest.mark.reliability]


class FakeWriter:
    def __init__(self) -> None:
        self.unsafe_gateways: frozenset[str] = frozenset()
        self.unsettled_gateways: frozenset[str] = frozenset()
        self.rpc_durations: deque[tuple[float, str, float]] = deque(maxlen=1024)
        self.native_started: dict[str, int] = {}
        self.owners: set[str] = set()
        self.sent = 0

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
        self.sent += 1
        return [
            DeliveryResult(OutputTarget("gw-test", point.point_id), "SUCCESS")
            for point in batch.points
        ]

    async def wait_settled(self, gateway_ids, timeout: float) -> bool:  # noqa: ANN001
        return True

    async def drain(self) -> None:
        return None

    async def close(self) -> bool:
        return True


def _runtime(configs_root, **service_kwargs) -> tuple[Runtime, Path]:  # noqa: ANN001
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-test": "127.0.0.1:59999"},
            interval=0.05,
            mappings=[("gw-test", "GW01.pitch_angle")],
        ),
        bind="127.0.0.1:0",
        **service_kwargs,
    )
    recover_transaction(service_path.absolute().parent.parent)
    return Runtime(load_settings(service_path), FakeWriter()), service_path


async def test_recording_disabled_by_default_writes_no_file(configs_root) -> None:  # noqa: ANN001
    runtime, service_path = _runtime(configs_root)
    generation = await runtime.start("demo-random", None, runtime.settings.revision)
    await asyncio.sleep(0.25)
    await runtime.stop(generation.generation_id, "")
    await runtime.shutdown()
    # 录制基准目录是 configs 根的父级（项目工作根）；默认关闭时不得创建任何制品。
    assert not (service_path.parent.parent.parent / "artifacts").exists()


async def test_enabled_recording_captures_every_batch_as_jsonl(configs_root) -> None:  # noqa: ANN001
    runtime, service_path = _runtime(
        configs_root,
        recording={"enabled": True, "directory": "artifacts/data-generator"},
    )
    generation = await runtime.start("demo-random", None, runtime.settings.revision)
    for _ in range(20):
        await asyncio.sleep(0.03)
        if runtime.writer.sent >= 3:
            break
    await runtime.stop(generation.generation_id, "")
    await runtime.shutdown()
    assert runtime.writer.sent >= 3

    jsonl_path = (
        service_path.parent.parent.parent
        / "artifacts"
        / "data-generator"
        / f"{generation.generation_id}.jsonl"
    )
    assert jsonl_path.is_file()
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == runtime.writer.sent
    sequences: list[int] = []
    for line in lines:
        payload = json.loads(line)  # 每行必须是独立可解析 JSON 对象
        assert payload["generation_id"] == generation.generation_id
        produced_at = datetime.fromisoformat(payload["produced_at"])
        assert produced_at.tzinfo is not None
        assert payload["points"], "every recorded batch carries at least one point"
        for point in payload["points"]:
            assert set(point) == {"device_id", "point_id", "source_time", "value"}
            source_time = datetime.fromisoformat(point["source_time"])
            assert source_time.tzinfo is not None
            # 随机 uniform 规则产出有限数值，allow_nan=False 拒绝 NaN/Infinity。
            assert isinstance(point["value"], int | float)
            assert point["value"] == point["value"]
            assert abs(point["value"]) != float("inf")
        sequences.append(payload["sequence"])
    assert sequences == sorted(sequences)
    assert sequences[0] == 1
    assert len(set(sequences)) == len(sequences)


async def test_bytes_point_is_recorded_as_base64_and_round_trips(configs_root) -> None:  # noqa: ANN001
    runtime, service_path = _runtime(
        configs_root,
        recording={"enabled": True, "directory": "artifacts/data-generator"},
    )
    generation = await runtime.start("demo-random", None, runtime.settings.revision)
    state = await runtime.get_generation(generation.generation_id)
    assert state is not None
    raw = b"\x00\x01\xfe\xff"
    batch = DataBatch(
        generation.generation_id,
        1,
        datetime.now(UTC),
        (DataPoint("GW01", "a02_dm_pitchangle1", raw, datetime.now(UTC)),),
    )
    await runtime._record(state, batch)
    await runtime.stop(generation.generation_id, "")
    await runtime.shutdown()

    jsonl_path = (
        service_path.parent.parent.parent
        / "artifacts"
        / "data-generator"
        / f"{generation.generation_id}.jsonl"
    )
    payload = json.loads(jsonl_path.read_text(encoding="utf-8").splitlines()[0])
    encoded = payload["points"][0]["value"]
    assert encoded == {"encoding": "base64", "value": base64.b64encode(raw).decode("ascii")}
    assert base64.b64decode(encoded["value"]) == raw


async def test_recording_disk_failure_marks_generation_failed(configs_root, tmp_path) -> None:  # noqa: ANN001
    # 录制基准目录是 configs 根的父级（pytest tmp_path）。先把 artifacts 预置为
    # 普通文件，JSONL 目录创建必然失败：异常不得被静默吞掉。
    (tmp_path / "artifacts").write_text("blocker", encoding="utf-8")
    runtime, _service_path = _runtime(
        configs_root,
        recording={"enabled": True, "directory": "artifacts/data-generator"},
    )
    generation = await runtime.start("demo-random", None, runtime.settings.revision)
    state = None
    for _ in range(40):
        await asyncio.sleep(0.03)
        state = await runtime.get_generation(generation.generation_id)
        if state is not None and state.actual_state == "FAILED":
            break
    assert state is not None and state.actual_state == "FAILED"
    assert state.last_error
    # 失败发生在目录创建阶段，不会留下半截 JSONL 制品。
    assert not (tmp_path / "artifacts" / "data-generator").exists()
    await runtime.shutdown()
