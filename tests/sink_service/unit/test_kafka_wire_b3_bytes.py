"""B3 批量 Kafka 编码器的独立字节等价门禁。

B3（``encode_kafka_points``）把 5 个共享身份字段编码为一次公共前缀，再与
逐点 8 字段做字节拼接。本文件的参考实现完全独立于 B3：按线格式规范用
**单个 13 字段 dict 一次 json.dumps** 重建权威字节（结构上与 B3 的
「前缀 + 切片拼接」不同），并同时以生产 Sink 私有 ``DeliveryPoint.encode()`` 作为
第二权威。判定一律比较 UTF-8 原始字节，不接受 JSON 语义相等。

覆盖 B3-01/B3-02 要求：指数浮点、Unicode/emoji/转义、时间精度与时区、
null、布尔、整型、字符串、500/1024 点拆批与切片边界、空子集、确定性、
字段顺序，以及可信边界对 NaN/Inf/非法报文的拒绝。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.application.delivery_point import DeliveryPoint
from sink_service.infrastructure.adapters.outbound.kafka_wire import (
    encode_kafka_point,
    encode_kafka_points,
)

pytestmark = [pytest.mark.unit, pytest.mark.contract]

FARM = "farm-b3"
COLLECTOR = "collector-b3"
TASK = "task-b3"
DEVICE = "WT042"
PUBLISH = datetime(2026, 11, 10, 9, 30, 1, 123456, tzinfo=UTC)

# 本文件需要覆盖 1024 点拆批/切片边界，显式提升可信边界容量（契约默认
# max_samples=500、max_bytes=1MiB；默认值与拒绝路径由 U02 容量专项测试钉死）。
LIMIT_SAMPLES = 1024
LIMIT_BYTES = 16 * 1024 * 1024

# 13 字段线格式的规范顺序（设计 v1_38 表 12-3）
WIRE_FIELD_ORDER = [
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

SCALAR_VALUES: list = [
    # 整型：零、负数、2^53 边界、超过 32 位的大整数
    0,
    1,
    -17,
    2**53,
    2**53 + 11,
    -1_000_000_000,
    # 浮点：普通小数、科学计数法正负指数、极值、次正规值、负零
    0.0,
    -0.0,
    1.5,
    -3.14e-9,
    1e308,
    1.5e-300,
    5e-324,
    # 布尔与 null
    True,
    False,
    None,
]

STRING_VALUES = [
    "中文，标点·测试",
    "emoji😀unicodeé",
    'quote"back\\slash',
    "tab\there\nnewline\r",
    "{}[]:,",
    "null true false 1.5e10",
    "",
    " " * 200,
]

PRECISE_TIMES = [
    datetime(2026, 11, 10, 8, 30, tzinfo=UTC),  # 零位小数
    datetime(2026, 11, 10, 8, 30, 1, tzinfo=UTC),  # 秒
    datetime(2026, 11, 10, 8, 30, 1, 100, tzinfo=UTC),  # 1 位
    datetime(2026, 11, 10, 8, 30, 1, 120000, tzinfo=UTC),  # 3 位
    datetime(2026, 11, 10, 8, 30, 1, 123456, tzinfo=UTC),  # 6 位
    datetime(2026, 11, 10, 16, 30, 1, tzinfo=timezone(timedelta(hours=8))),
]


def _json_ts(value: datetime | None) -> str | None:
    """独立实现的时间渲染（与生产 _json_timestamp 同规范，单独书写）。"""
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def reference_wire_bytes(batch: SampleBatch, item: SampleItem) -> bytes:
    """测试内独立权威：13 字段单 dict 一次性序列化，不走 B3 拼接逻辑。"""
    return json.dumps(
        {
            "schema_version": 1,
            "wind_farm_id": batch.wind_farm_id,
            "collector_id": batch.collector_id,
            "task_id": batch.task_id,
            "device_id": batch.device_id,
            "point_id": item.point_id,
            "sample_id": item.sample_id,
            "value": item.value,
            "quality": item.quality,
            "sample_time": _json_ts(item.sample_time),
            "publish_time": _json_ts(batch.publish_time),
            "receive_time": _json_ts(item.receive_time),
            "max_staleness_ms": item.max_staleness_ms,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _item(index: int, value: object, sample_time: datetime, receive_time, stale) -> SampleItem:
    return SampleItem(
        point_id=f"p{index:04d}",
        sample_id=f"s-b3-{index}",
        value=value,
        quality="good",
        sample_time=sample_time,
        receive_time=receive_time,
        max_staleness_ms=stale,
    )


def _batch(items: tuple[SampleItem, ...]) -> SampleBatch:
    return SampleBatch(
        batch_id="b-b3",
        mode="reliable",
        wind_farm_id=FARM,
        collector_id=COLLECTOR,
        task_id=TASK,
        device_id=DEVICE,
        publish_time=PUBLISH,
        samples=items,
    )


def _decoded(batch: SampleBatch) -> SampleBatch:
    # 复刻生产 _run 入口：线格式 → 严格 decoder
    return SampleBatch.decode(
        batch.encode(max_samples=LIMIT_SAMPLES, max_bytes=LIMIT_BYTES),
        "reliable",
        subject=batch.subject(),
        max_samples=LIMIT_SAMPLES,
        max_bytes=LIMIT_BYTES,
    )


def _matrix_pairs():
    pairs = []
    values = SCALAR_VALUES + STRING_VALUES
    for value in values:
        for sample_time in PRECISE_TIMES:
            for receive_time in (None, sample_time):
                pairs.append((value, sample_time, receive_time))
    return pairs


@pytest.mark.parametrize(("value", "sample_time", "receive_time"), _matrix_pairs())
def test_batch_encoder_byte_identical_to_two_independent_authorities(
    value, sample_time, receive_time
) -> None:
    batch = _decoded(_batch((_item(0, value, sample_time, receive_time, None),)))
    item = batch.samples[0]

    b3_bytes = encode_kafka_points(batch, (item,))[0]
    reference_bytes = reference_wire_bytes(batch, item)
    production_bytes = DeliveryPoint.from_batch(batch, item).encode()

    # 三重字节相等：B3 拼接 == 测试内单 dict 参考 == 生产 DeliveryPoint
    assert b3_bytes == reference_bytes
    assert b3_bytes == production_bytes
    # 单点包装入口必须复用同一批量规范，不得形成两套合同
    assert encode_kafka_point(batch, item) == b3_bytes
    # 语义层再确认可被生产 decoder 接受
    assert json.loads(b3_bytes) == json.loads(production_bytes)


@pytest.mark.parametrize("stale", [None, 1, 60_000, 86_400_000])
def test_staleness_rendering_byte_identical(stale: int | None) -> None:
    if stale is not None:
        # Reliable 契约：非 None staleness 在可信边界即拒绝，编码器不可达
        with pytest.raises(Exception):  # noqa: B017, BLE001
            _batch((_item(7, 42, PRECISE_TIMES[0], None, stale),))
        return
    batch = _decoded(_batch((_item(7, 42, PRECISE_TIMES[0], None, stale),)))
    item = batch.samples[0]
    rendered = encode_kafka_points(batch, (item,))[0]
    assert rendered == reference_wire_bytes(batch, item)
    # reliable 线格式固定渲染 null（13 字段不缺省）
    assert b'"max_staleness_ms":null' in rendered


def test_exponent_floats_preserve_json_rendering_across_splice() -> None:
    # 拼接点位于 device_id 与 point_id 之间，指数渲染在逐点段，
    # 显式钉死浮点文本与拼接 junction 都不丢字符。
    for value in (1e308, -3.14e-9, 1.5e-300, 5e-324, -0.0):
        batch = _decoded(_batch((_item(1, value, PRECISE_TIMES[3], None, None),)))
        item = batch.samples[0]
        rendered = encode_kafka_points(batch, (item,))[0]
        assert rendered == reference_wire_bytes(batch, item)
        expected_token = json.dumps(value, separators=(",", ":"))
        assert f'"value":{expected_token},'.encode() in rendered
        assert f'"device_id":"{DEVICE}","point_id":'.encode() in rendered


def test_unicode_and_escapes_are_raw_utf8_byte_identical() -> None:
    for value in STRING_VALUES:
        batch = _decoded(_batch((_item(2, value, PRECISE_TIMES[0], None, None),)))
        item = batch.samples[0]
        rendered = encode_kafka_points(batch, (item,))[0]
        assert rendered == reference_wire_bytes(batch, item)
        # ensure_ascii=False：中文/emoji 必须是原始 UTF-8 而非 \\u 转义
        if any(ord(ch) > 127 for ch in value):
            assert b"\\u" not in rendered


def _varied_batch(size: int) -> SampleBatch:
    pool = SCALAR_VALUES + STRING_VALUES
    items = tuple(
        _item(
            idx,
            pool[idx % len(pool)],
            PRECISE_TIMES[idx % len(PRECISE_TIMES)],
            PRECISE_TIMES[idx % len(PRECISE_TIMES)] if idx % 2 else None,
            None,  # reliable 模式必须省略 staleness
        )
        for idx in range(size)
    )
    return _decoded(_batch(items))


@pytest.mark.parametrize("size", [500, 1024])
def test_full_sized_chunks_every_record_byte_identical(size: int) -> None:
    batch = _varied_batch(size)
    encoded = encode_kafka_points(batch, batch.samples)
    assert len(encoded) == size
    production = tuple(DeliveryPoint.from_batch(batch, point).encode() for point in batch.samples)
    for idx in range(size):
        assert encoded[idx] == reference_wire_bytes(batch, batch.samples[idx])
        assert encoded[idx] == production[idx]


def test_runtime_chunk_slicing_500_500_24_keeps_indices_and_bytes() -> None:
    # 复刻 reliable_runtime 的 selected[start:start+500] 三段切片
    batch = _varied_batch(1024)
    slices = [batch.samples[0:500], batch.samples[500:1000], batch.samples[1000:1024]]
    assert [len(part) for part in slices] == [500, 500, 24]
    flattened: list[bytes] = []
    for part in slices:
        flattened.extend(encode_kafka_points(batch, part))
    production = tuple(DeliveryPoint.from_batch(batch, point).encode() for point in batch.samples)
    assert len(flattened) == 1024
    for idx in range(1024):
        assert flattened[idx] == production[idx]
    # 第二段首条对应第 500 点，索引不错位
    assert b'"point_id":"p0500"' in flattened[500]
    assert b'"point_id":"p1000"' in flattened[1000]


def test_shared_identity_prefix_is_identical_across_chunk() -> None:
    batch = _varied_batch(500)
    encoded = encode_kafka_points(batch, batch.samples)
    junction = f'"device_id":"{DEVICE}",'.encode()
    prefixes = {raw[: raw.index(junction) + len(junction)] for raw in encoded}
    assert len(prefixes) == 1  # 500 条共享同一公共前缀字节
    shared = next(iter(prefixes))
    for token in (
        f'"wind_farm_id":"{FARM}"',
        f'"collector_id":"{COLLECTOR}"',
        f'"task_id":"{TASK}"',
        f'"device_id":"{DEVICE}"',
    ):
        assert token.encode() in shared
    # publish_time 每子批只渲染一次，但在每条记录中的位置与字节不变
    publish_token = f'"publish_time":{json.dumps(_json_ts(PUBLISH))}'.encode()
    assert all(publish_token in raw for raw in encoded)


def test_empty_subset_returns_empty_tuple() -> None:
    batch = _varied_batch(3)
    assert encode_kafka_points(batch, ()) == ()


def test_encoding_is_deterministic_with_no_hidden_state() -> None:
    batch = _varied_batch(128)
    first = encode_kafka_points(batch, batch.samples)
    second = encode_kafka_points(batch, batch.samples)
    assert first == second
    # 不同源批之间不得共享/污染缓存：身份字段必须各自正确
    other = _decoded(
        _batch(
            (
                SampleItem(
                    point_id="p0000",
                    sample_id="s-other",
                    value=1,
                    quality="good",
                    sample_time=PRECISE_TIMES[0],
                ),
            )
        )
    )
    other_batch = SampleBatch(
        batch_id=other.batch_id,
        mode=other.mode,
        wind_farm_id="farm-other",
        collector_id=other.collector_id,
        task_id=other.task_id,
        device_id=other.device_id,
        publish_time=other.publish_time,
        samples=other.samples,
    )
    rendered = encode_kafka_points(other_batch, other_batch.samples)[0]
    assert b'"wind_farm_id":"farm-other"' in rendered
    assert FARM.encode() not in rendered


def test_field_order_preserved_under_splice() -> None:
    batch = _varied_batch(1)
    rendered = encode_kafka_points(batch, batch.samples)[0].decode()
    keys = [segment.split(":", 1)[0].strip('"') for segment in rendered.strip("{}").split(",")]
    assert keys == WIRE_FIELD_ORDER


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_values_rejected_at_trusted_boundary(non_finite: float) -> None:
    # B3 只接受已验证输入；非有限浮点在 SampleBatch 构造期即拒绝
    with pytest.raises(Exception):  # noqa: B017, BLE001
        _batch((_item(0, non_finite, PRECISE_TIMES[0], None, None),))
    # 参考实现与 json 规范同样拒绝 NaN/Infinity（allow_nan=False）
    with pytest.raises(ValueError):
        json.dumps({"value": non_finite}, allow_nan=False)


@pytest.mark.parametrize(
    "bad_payload",
    [
        b'{"schema_version":2,"batch_id":"b","mode":"reliable","wind_farm_id":"f",'
        b'"collector_id":"c","task_id":"t","device_id":"d","publish_time":'
        b'"2026-11-10T08:30:00Z","samples":[{"point_id":"p","sample_id":"s",'
        b'"value":NaN,"quality":"good","sample_time":"2026-11-10T08:30:00Z"}]}',
        # v2 中 point_id 不再是 subject 分量（"." 合法）；控制字符仍被拒绝。
        b'{"schema_version":2,"batch_id":"b","mode":"reliable","wind_farm_id":"f",'
        b'"collector_id":"c","task_id":"t","device_id":"d","publish_time":'
        b'"2026-11-10T08:30:00Z","samples":[{"point_id":"bad\\npoint","sample_id":"s",'
        b'"value":1,"quality":"good","sample_time":"2026-11-10T08:30:00Z"}]}',
        b"not-json",
    ],
)
def test_malformed_payloads_never_reach_encoder(bad_payload: bytes) -> None:
    with pytest.raises(Exception):  # noqa: B017, BLE001
        SampleBatch.decode(bad_payload, mode="reliable")
