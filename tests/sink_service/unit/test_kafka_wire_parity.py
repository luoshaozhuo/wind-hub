"""B1 快路径 Kafka 线格式与生产 SampleMessage.encode 的等价门禁。

正式收纳 R8 实验（仓库外 /tmp 矩阵）的 228 组字节/语义等价用例与非法
输入拒绝用例，防止 ``kafka_wire.encode_kafka_point`` 与权威编码发生漂移：

- 字段集合、字段顺序、JSON 类型与 UTF-8 字节逐字节相等；
- float/int/bool/str/null、中文、emoji、引号/反斜杠/制表符等转义；
- 时间精度（零/1/3/6 位微秒）、UTC 的 Z 渲染、非 UTC 偏移保留、
  receive_time 可选缺省；
- 非法数据仍由可信边界 ``SampleBatch.decode`` 拒绝，快路径不削减校验。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.application.config import validate_snapshot
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.delivery_point import DeliveryPoint
from sink_service.infrastructure.adapters.outbound.kafka_wire import encode_kafka_point
from sink_service.infrastructure.reliable_runtime import ReliableDeliveryPort
from tests.sink_service._stage1_fixtures import snapshot_docs, snapshot_files

pytestmark = [pytest.mark.unit, pytest.mark.contract]

FARM = "farm-a"
COLLECTOR = "collector-1"
TASK = "task-r9"
DEVICE = "WT001"

SPECIAL_STRINGS = [
    "中文，标点·测试",
    'quote"back\\slash',
    "tab\there\nnewline\r",
    "emoji😀unicodeé",
    "{}[]:,",
    "null true false 1.5e10",
    "",
    " " * 200,
]

VALUES: list = [
    0,
    1,
    -17,
    2**53,
    0.0,
    1.5,
    -3.14e-9,
    1e308,
    True,
    False,
    None,
    *SPECIAL_STRINGS,
]

TIMES = [
    datetime(2026, 11, 9, 8, 30, tzinfo=UTC),
    datetime(2026, 11, 9, 8, 30, 1, tzinfo=UTC),
    datetime(2026, 11, 9, 8, 30, 1, 120000, tzinfo=UTC),
    datetime(2026, 11, 9, 8, 30, 1, 123456, tzinfo=UTC),
    datetime(2026, 11, 9, 8, 30, 1, 100, tzinfo=UTC),
    datetime(2026, 11, 9, 16, 30, 1, tzinfo=timezone(timedelta(hours=8))),
]


def _batch(value, sample_time, *, receive_time=None, stale=None) -> SampleBatch:
    now = datetime(2026, 11, 9, 8, 30, 1, 123456, tzinfo=UTC)
    item = SampleItem(
        point_id="p000",
        sample_id="s-parity-0",
        value=value,
        quality="good",
        sample_time=sample_time,
        receive_time=receive_time,
        max_staleness_ms=stale,
    )
    return SampleBatch(
        batch_id="b-parity",
        mode="reliable",
        wind_farm_id=FARM,
        collector_id=COLLECTOR,
        task_id=TASK,
        device_id=DEVICE,
        publish_time=now,
        samples=(item,),
    )


def _all_pairs():
    pairs = []
    for value in VALUES:
        for sample_time in TIMES:
            for receive_time in (None, sample_time):
                pairs.append((value, sample_time, receive_time))
    return pairs


@pytest.mark.parametrize(("value", "sample_time", "receive_time"), _all_pairs())
def test_b1_bytes_identical_to_production_and_semantically_equal(
    value, sample_time, receive_time
) -> None:
    batch = _batch(value, sample_time, receive_time=receive_time)
    # 从线格式重新 decode，复刻生产 _run 入口
    decoded = SampleBatch.decode(batch.encode(), "reliable", subject=batch.subject())
    item = decoded.samples[0]

    production_bytes = DeliveryPoint.from_batch(decoded, item).encode()
    b1_bytes = encode_kafka_point(decoded, item)

    # 第一层：UTF-8 字节逐字节相等（字段顺序与 JSON 渲染都被钉死）
    assert b1_bytes == production_bytes

    # 第二层：B1 产物与生产 13 字段记录语义相等（逐字段 JSON 对象对比）
    assert json.loads(b1_bytes) == json.loads(production_bytes)


def test_reliable_mode_renders_null_optional_fields() -> None:
    batch = _batch(1.0, TIMES[0])
    decoded = SampleBatch.decode(batch.encode(), mode="reliable")
    rendered = encode_kafka_point(decoded, decoded.samples[0]).decode()
    assert '"max_staleness_ms":null' in rendered
    assert '"receive_time":null' in rendered


def test_field_order_matches_sample_message_schema() -> None:
    batch = _batch(1.5, TIMES[2], receive_time=TIMES[2])
    decoded = SampleBatch.decode(batch.encode(), mode="reliable")
    rendered = encode_kafka_point(decoded, decoded.samples[0]).decode()
    keys = [segment.split(":", 1)[0].strip('"') for segment in rendered.strip("{}").split(",")]
    assert keys == [
        "schema_version",
        "wind_farm_id",
        "collector_id",
        "task_id",
        "device_id",
        "point_id",
        "sample_id",
        "value",
        "quality",
        "sample_time",
        "publish_time",
        "receive_time",
        "max_staleness_ms",
    ]


@pytest.mark.parametrize(
    "bad_payload",
    [
        # NaN 不是合法 JSON token
        b'{"schema_version":2,"batch_id":"b","mode":"reliable","wind_farm_id":"f",'
        b'"collector_id":"c","task_id":"t","device_id":"d","publish_time":'
        b'"2026-11-09T08:30:00Z","samples":[{"point_id":"p","sample_id":"s",'
        b'"value":NaN,"quality":"good","sample_time":"2026-11-09T08:30:00Z"}]}',
        # v2 中 point_id 不再是 NATS subject 分量，"." 合法；但控制字符仍被
        # _is_unsafe_identifier 拒绝（JSON 转义 \\n 解析为换行符 ord=10）。
        b'{"schema_version":2,"batch_id":"b","mode":"reliable","wind_farm_id":"f",'
        b'"collector_id":"c","task_id":"t","device_id":"d","publish_time":'
        b'"2026-11-09T08:30:00Z","samples":[{"point_id":"bad\\npoint","sample_id":"s",'
        b'"value":1,"quality":"good","sample_time":"2026-11-09T08:30:00Z"}]}',
        b"not-json",
    ],
)
def test_invalid_payloads_still_rejected_at_trusted_boundary(bad_payload: bytes) -> None:
    with pytest.raises(Exception):  # noqa: B017, BLE001 - 可信边界拒绝方式不限定具体异常类
        SampleBatch.decode(bad_payload, mode="reliable")


def test_kafka_target_flag_only_set_for_kafka_templates() -> None:
    """非 Kafka Reliable 模板不得误开预编码分支（_validate_target 守门）。"""
    snapshot = validate_snapshot(snapshot_files(snapshot_docs()))
    definition = DeliveryDefinition("power_history", "history_kafka", "reliable", "START")
    port = ReliableDeliveryPort(definition, snapshot, pool=object())
    # start() 之外手动触发模板校验，覆盖 _kafka_target 赋值
    port._validate_target()
    assert port._kafka_target is True
