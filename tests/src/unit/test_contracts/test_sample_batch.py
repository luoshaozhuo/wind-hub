"""SampleBatch v2 严格线协议契约（Contracts 设计 v1_6 §5.2 矩阵）。

覆盖：schema_version 严格性、Realtime/Reliable 隔离、NATS Subject 与
Header 安全、显式 null、标量边界、时间戳语义、重复 JSON 字段、多余/缺失
字段、批次内重复身份、容量与字节限制、编解码回环、稳定身份与 Subject
不一致拒绝。纯单元测试，不接触 NATS。
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from contracts.sample_batch import (
    DEFAULT_MAX_BATCH_BYTES,
    DEFAULT_MAX_SAMPLES_PER_BATCH,
    SampleBatch,
    SampleItem,
    validate_subject_token,
)

pytestmark = [pytest.mark.unit, pytest.mark.contract, pytest.mark.invalid_input]

_AWARE = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _item(
    point_id: str = "turbine.power",
    sample_id: str = "s-0001",
    *,
    value: object = 1200,
    staleness: int | None = 500,
    sample_time: datetime = _AWARE,
) -> SampleItem:
    return SampleItem(
        point_id=point_id,
        sample_id=sample_id,
        value=value,
        quality="good",
        sample_time=sample_time,
        max_staleness_ms=staleness,
    )


def _batch(
    *,
    mode: str = "realtime",
    samples: tuple[SampleItem, ...] | None = None,
    batch_id: str = "batch-0001",
    publish_time: datetime = _AWARE,
) -> SampleBatch:
    return SampleBatch(
        batch_id=batch_id,
        mode=mode,  # type: ignore[arg-type]
        wind_farm_id="farm-a",
        collector_id="collector-1",
        task_id="wtg-01",
        device_id="wtg-01",
        publish_time=publish_time,
        samples=samples if samples is not None else (_item(),),
    )


# ---------- schema_version 严格性 ----------


def test_schema_version_is_literal_2_and_wire_requires_int_2() -> None:
    batch = _batch()
    assert batch.schema_version == 2
    payload = batch.encode()
    decoded = SampleBatch.decode(payload, "realtime")
    assert decoded.schema_version == 2


@pytest.mark.parametrize("version", [1, 3, "2", 2.0, None])
def test_decode_rejects_non_literal_schema_version(version: object) -> None:
    document = json.loads(_batch().encode())
    if version is None:
        document.pop("schema_version")
    else:
        document["schema_version"] = version
    with pytest.raises((ValueError, ValidationError)):
        SampleBatch.decode(json.dumps(document).encode(), "realtime")


# ---------- Realtime / Reliable 模式隔离 ----------


def test_realtime_requires_max_staleness_on_every_sample() -> None:
    with pytest.raises(ValidationError, match="max_staleness"):
        _batch(mode="realtime", samples=(_item(staleness=None),))


def test_reliable_forbids_max_staleness_on_every_sample() -> None:
    reliable_item = _item(staleness=None)
    batch = _batch(mode="reliable", samples=(reliable_item,))
    assert batch.subject().startswith("wh.hist.batch.")
    with pytest.raises(ValidationError, match="max_staleness"):
        _batch(mode="reliable", samples=(_item(staleness=500),))


def test_decode_rejects_reliable_payload_carrying_staleness() -> None:
    payload = _batch(mode="realtime").encode()
    with pytest.raises(ValueError, match="reliable"):
        SampleBatch.decode(payload, "reliable")


def test_decode_rejects_mode_channel_mismatch() -> None:
    reliable = _batch(mode="reliable", samples=(_item(staleness=None),))
    with pytest.raises(ValueError, match="mode differs"):
        SampleBatch.decode(reliable.encode(), "realtime")


# ---------- NATS Subject / Header 安全 ----------


@pytest.mark.parametrize(
    "token",
    ["farm a", "farm.a", "farm*a", "farm>a", "farm\ta", "farm\x00a", "farm\x7fa", ""],
)
def test_subject_identity_tokens_reject_unsafe_components(token: str) -> None:
    with pytest.raises((ValueError, ValidationError)):
        validate_subject_token(token)


@pytest.mark.parametrize("field", ["wind_farm_id", "collector_id", "task_id", "device_id"])
def test_batch_identity_fields_use_subject_validation(field: str) -> None:
    kwargs: dict[str, object] = {
        "batch_id": "b1",
        "mode": "realtime",
        "wind_farm_id": "farm-a",
        "collector_id": "collector-1",
        "task_id": "wtg-01",
        "device_id": "wtg-01",
        "publish_time": _AWARE,
        "samples": (_item(),),
    }
    kwargs[field] = "bad token"
    with pytest.raises((ValueError, ValidationError)):
        SampleBatch(**kwargs)


def test_point_id_may_contain_dots_but_not_control_characters() -> None:
    dotted = _item(point_id="root.branch.leaf")
    assert dotted.point_id == "root.branch.leaf"
    with pytest.raises(ValidationError):
        _item(point_id="root\x01leaf")
    with pytest.raises(ValidationError):
        _item(point_id="   ")


def test_subject_is_device_scoped_and_distinct_per_channel() -> None:
    realtime = _batch(mode="realtime")
    reliable = _batch(mode="reliable", samples=(_item(staleness=None),))
    assert realtime.subject() == "wh.rt.batch.farm-a.collector-1.wtg-01"
    assert reliable.subject() == "wh.hist.batch.farm-a.collector-1.wtg-01"


def test_decode_rejects_subject_envelope_identity_mismatch() -> None:
    payload = _batch().encode()
    with pytest.raises(ValueError, match="subject"):
        SampleBatch.decode(payload, "realtime", subject="wh.rt.batch.farm-a.other.wtg-01")
    decoded = SampleBatch.decode(
        payload, "realtime", subject="wh.rt.batch.farm-a.collector-1.wtg-01"
    )
    assert decoded.batch_id == "batch-0001"


# ---------- 显式 null 与标量值域 ----------


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 3.14, "运行中", b""])
def test_accepted_scalar_values_roundtrip(value: object) -> None:
    # bytes 不是 SampleItem 允许的线值类型（JSON 无 bytes），单独断言拒绝。
    if isinstance(value, bytes):
        with pytest.raises(ValidationError):
            _batch(samples=(_item(value=value),))
        return
    batch = _batch(samples=(_item(sample_id="s-x", value=value),))
    decoded = SampleBatch.decode(batch.encode(), "realtime")
    assert decoded.samples[0].value == value


def test_explicit_null_value_is_preserved_not_omitted() -> None:
    batch = _batch(samples=(_item(sample_id="s-null", value=None),))
    document = json.loads(batch.encode())
    assert "value" in document["samples"][0]
    assert document["samples"][0]["value"] is None
    decoded = SampleBatch.decode(batch.encode(), "realtime")
    assert decoded.samples[0].value is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_floats_rejected_at_model_boundary(value: float) -> None:
    with pytest.raises(ValidationError, match="nonfinite"):
        _item(value=value)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_decode_rejects_nonfinite_json_constants(literal: str) -> None:
    text = _batch().encode().decode()
    payload = text.replace('"value":1200', f'"value":{literal}').encode()
    with pytest.raises(ValueError, match="JSON constant"):
        SampleBatch.decode(payload, "realtime")


@pytest.mark.parametrize("value", [-(2**63) - 1, 2**64])
def test_integer_outside_protobuf_scalar_range_rejected(value: int) -> None:
    with pytest.raises(ValidationError, match="scalar range"):
        _item(value=value)


@pytest.mark.parametrize("value", [-(2**63), 2**64 - 1])
def test_integer_boundaries_roundtrip(value: int) -> None:
    batch = _batch(samples=(_item(sample_id="s-int", value=value),))
    decoded = SampleBatch.decode(batch.encode(), "realtime")
    assert decoded.samples[0].value == value


def test_bool_is_not_treated_as_integer_boundary_violation() -> None:
    batch = _batch(samples=(_item(sample_id="s-bool", value=True),))
    decoded = SampleBatch.decode(batch.encode(), "realtime")
    assert decoded.samples[0].value is True


# ---------- 时间戳语义 ----------


def test_naive_sample_time_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone|UTC offset"):
        _item(sample_time=datetime(2026, 11, 8, 12, 0))


def test_wire_naive_timestamp_string_rejected_at_decode() -> None:
    document = json.loads(_batch().encode())
    document["samples"][0]["sample_time"] = "2026-11-08T12:00:00"
    with pytest.raises(ValueError, match="UTC offset"):
        SampleBatch.decode(json.dumps(document).encode(), "realtime")


def test_malformed_timestamp_string_rejected_at_decode() -> None:
    document = json.loads(_batch().encode())
    document["publish_time"] = "not-a-timestamp"
    with pytest.raises(ValueError, match="ISO timestamp"):
        SampleBatch.decode(json.dumps(document).encode(), "realtime")


def test_zoned_timestamp_normalized_to_utc() -> None:
    cst = timezone(timedelta(hours=8))
    item = _item(sample_time=datetime(2026, 11, 8, 20, 0, tzinfo=cst))
    assert item.sample_time == datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


# ---------- 重复 JSON 字段 / 多余字段 / 缺失字段 ----------


def test_decode_rejects_duplicate_json_keys() -> None:
    text = _batch().encode().decode()
    duplicated = text.replace('"batch_id":"batch-0001"', '"batch_id":"a","batch_id":"b"', 1)
    with pytest.raises(ValueError, match="duplicate JSON key"):
        SampleBatch.decode(duplicated.encode(), "realtime")


def test_extra_fields_rejected_at_model_and_decode() -> None:
    with pytest.raises(ValidationError):
        SampleItem(
            point_id="p",
            sample_id="s",
            value=1,
            quality="good",
            sample_time=_AWARE,
            max_staleness_ms=500,
            unexpected="x",
        )
    document = json.loads(_batch().encode())
    document["unknown_top"] = 1
    with pytest.raises(ValidationError):
        SampleBatch.decode(json.dumps(document).encode(), "realtime")


def test_missing_required_field_rejected() -> None:
    document = json.loads(_batch().encode())
    document.pop("batch_id")
    with pytest.raises((ValueError, ValidationError)):
        SampleBatch.decode(json.dumps(document).encode(), "realtime")


def test_sample_without_explicit_value_key_rejected() -> None:
    document = json.loads(_batch().encode())
    del document["samples"][0]["value"]
    with pytest.raises(ValueError, match="explicitly contain value"):
        SampleBatch.decode(json.dumps(document).encode(), "realtime")


# ---------- 批次内重复身份与空批次 ----------


def test_duplicate_point_id_within_batch_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate point_id"):
        _batch(samples=(_item(point_id="p", sample_id="s1"), _item(point_id="p", sample_id="s2")))


def test_duplicate_sample_id_within_batch_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate sample_id"):
        _batch(samples=(_item(point_id="p1", sample_id="s"), _item(point_id="p2", sample_id="s")))


def test_empty_batch_rejected() -> None:
    with pytest.raises(ValidationError, match="must contain samples"):
        _batch(samples=())


# ---------- 容量与字节限制 ----------


def test_encode_rejects_more_than_max_samples() -> None:
    batch = _batch(samples=tuple(_item(point_id=f"p{i}", sample_id=f"s{i}") for i in range(10)))
    with pytest.raises(ValueError, match="max_samples"):
        batch.encode(max_samples=9, max_bytes=DEFAULT_MAX_BATCH_BYTES)


def test_decode_rejects_payload_above_max_bytes() -> None:
    payload = _batch().encode()
    with pytest.raises(ValueError, match="payload length"):
        SampleBatch.decode(payload, "realtime", max_bytes=len(payload) - 1)


def test_invalid_limits_rejected() -> None:
    batch = _batch()
    for kwargs in (
        {"max_samples": 0, "max_bytes": 1024},
        {"max_samples": 10, "max_bytes": 0},
        {"max_samples": True, "max_bytes": 1024},  # type: ignore[dict-item]
    ):
        with pytest.raises(ValueError, match="limits"):
            batch.encode(**kwargs)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="limits"):
            SampleBatch.decode(b"{}", "realtime", **kwargs)  # type: ignore[arg-type]


def test_oversized_default_batch_rejected_by_default_limits() -> None:
    batch = _batch(
        samples=tuple(
            _item(point_id=f"p{i}", sample_id=f"s{i}")
            for i in range(DEFAULT_MAX_SAMPLES_PER_BATCH + 1)
        )
    )
    with pytest.raises(ValueError, match="max_samples"):
        batch.encode()


# ---------- 编解码回环与稳定身份 ----------


def test_encode_decode_loophole_preserves_all_fields() -> None:
    item = _item(
        point_id="机组.功率",
        sample_id="s-中文",
        value="运行",
    )
    batch = _batch(samples=(item,))
    decoded = SampleBatch.decode(batch.encode(), "realtime")
    assert decoded == batch


def test_reliable_omits_optional_fields_in_wire_document() -> None:
    batch = _batch(mode="reliable", samples=(_item(staleness=None, value=None),))
    document = json.loads(batch.encode())
    sample = document["samples"][0]
    assert "max_staleness_ms" not in sample
    assert "receive_time" not in sample
    assert sample["value"] is None


def test_identity_is_stable_across_resends_and_publish_time_restamp() -> None:
    batch = _batch()
    identity = batch.identity
    resent = batch.model_copy(update={"publish_time": datetime.now(UTC)})
    assert resent.identity == identity
    assert identity == ("farm-a", "collector-1", "wtg-01", "wtg-01", "realtime", "batch-0001")


def test_model_is_frozen() -> None:
    batch = _batch()
    with pytest.raises(ValidationError):
        batch.batch_id = "other"  # type: ignore[misc]


# ---------- 大批量编码/解码与内存占用基线（不虚构生产阈值） ----------


def test_large_batch_roundtrip_and_reports_measured_footprint() -> None:
    size = DEFAULT_MAX_SAMPLES_PER_BATCH
    batch = _batch(
        samples=tuple(
            _item(point_id=f"turbine.point.{i}", sample_id=f"s-{i:08x}") for i in range(size)
        )
    )
    payload = batch.encode()
    assert len(payload) <= DEFAULT_MAX_BATCH_BYTES
    decoded = SampleBatch.decode(payload, "realtime")
    assert len(decoded.samples) == size
    measured = {
        "payload_bytes": len(payload),
        "sample_model_bytes": sys.getsizeof(decoded.samples[0]),
        "tuple_overhead_bytes": sys.getsizeof(decoded.samples),
    }
    # 记录实测占用而非断言虚构阈值；仅保证有界且可重复回环。
    assert measured["payload_bytes"] > 0
    again = SampleBatch.decode(payload, "realtime")
    assert again == decoded
