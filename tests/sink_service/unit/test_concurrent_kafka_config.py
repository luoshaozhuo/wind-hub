"""v1_23 设备级并发 Kafka 的配置校验契约（单元层）。

覆盖设计 §6.3.3 的配置面规则：

- 并发模板 1.1.0 要求每 Reliable Delivery 恰含一个合法 Subject Token 设备；
- 空设备 / 多设备 / 非法 Token 一律 fail-closed；
- 同一物理 Kafka 目标上同设备不得被两个 Delivery 重复占用；
- 不同设备可经同一个 Sink ID 共享物理目标；
- 不同 Sink ID 指向同一物理目标不得绕过共享额度（含新旧模板混用）；
- 旧 1.0.0 模板保持串行路径，多设备选择器仍然合法。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from sink_service.application.config import validate_snapshot
from sink_service.domain.config import ConfigurationError, SnapshotFile
from tests.sink_service._stage1_fixtures import (
    DELIVERIES_INSTANCE,
    SERVICE_FILE,
    SINKS_INSTANCE,
    TEMPLATE_ROOT,
    service_document,
    snapshot_files,
    template_doc,
    template_path,
)

pytestmark = [pytest.mark.unit, pytest.mark.contract]

REPO_ROOT = Path(__file__).resolve().parents[3]
SHIPPED_110 = REPO_ROOT / "configs/templates/sinks/kafka_history_1.1.0.yaml"

KAFKA_PATH_110 = f"{TEMPLATE_ROOT}/sinks/kafka_history_1.1.0.yaml"
KAFKA_PATH_100 = f"{TEMPLATE_ROOT}/sinks/kafka_history_1.0.0.yaml"
DELIVERY_TEMPLATE_PATH = template_path("reliable_history")


def _kafka_template(*, version: str, concurrent: bool, inflight: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "file_version": version,
        "template_id": "kafka_history",
        "kind": "kafka",
        "capabilities": {
            "realtime": False,
            "reliable": True,
            "idempotent": False,
            "supports_concurrent_write": concurrent,
            "max_safe_inflight": inflight,
        },
        "connection": {"bootstrap_servers": ["192.168.1.20:9092"]},
        "output": {
            "topic": "windhub.samples",
            "key_field": "device_id",
            "business_duplicate_policy": "allow_at_least_once",
            "dedup_identity_fields": ["delivery_id", "sample_id"],
            "enable_idempotence": True,
            "acks": "all",
            "write_timeout_ms": 2000,
            "linger_ms": 0,
        },
    }


def _sink_instance(
    sink_id: str, *, version: str, servers: list[str] | None = None
) -> dict[str, Any]:
    return {
        "template_id": "kafka_history",
        "template_version": version,
        "connection": {
            "bootstrap_servers": servers if servers is not None else ["192.168.1.20:9092"]
        },
        "overrides": {},
    }


def _delivery_instance(
    delivery_id: str,
    *,
    sink: str,
    devices: list[str],
    points: list[str] | None = None,
    collector: str = "collector-1",
) -> dict[str, Any]:
    return {
        "template_id": "reliable_history",
        "template_version": "1.0.0",
        "sink_id": sink,
        "desired_state": "START",
        "source": {
            "wind_farm_id": "farm-a",
            "collector_id": collector,
            "devices": devices,
            "points": points if points is not None else ["active_power"],
        },
        "overrides": {},
    }


def _docs(
    *,
    sinks: dict[str, dict[str, Any]],
    deliveries: dict[str, dict[str, Any]],
    templates: tuple[str, ...] = ("1.1.0",),
    topic_per_sink: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    topic_per_sink = topic_per_sink or {}
    docs: dict[str, dict[str, Any]] = {SERVICE_FILE: service_document()}
    for version in templates:
        concurrent = version == "1.1.0"
        path = KAFKA_PATH_110 if concurrent else KAFKA_PATH_100
        docs[path] = _kafka_template(
            version=version, concurrent=concurrent, inflight=4 if concurrent else 1
        )
        if version in topic_per_sink:
            docs[path]["output"]["topic"] = topic_per_sink[version]
    docs[DELIVERY_TEMPLATE_PATH] = template_doc("reliable_history")
    docs[SINKS_INSTANCE] = {
        "schema_version": 1,
        "file_version": "1.0.0",
        "revision": "sinks-r7",
        "sinks": sinks,
    }
    docs[DELIVERIES_INSTANCE] = {
        "schema_version": 1,
        "file_version": "1.0.0",
        "revision": "deliveries-r7",
        "deliveries": deliveries,
    }
    return docs


def _validate(docs: dict[str, dict[str, Any]]):
    return validate_snapshot(snapshot_files(docs))


def _two_device_docs() -> dict[str, dict[str, Any]]:
    return _docs(
        sinks={"history_kafka": _sink_instance("history_kafka", version="1.1.0")},
        deliveries={
            "d-wt001": _delivery_instance("d-wt001", sink="history_kafka", devices=["WT001"]),
            "d-wt002": _delivery_instance("d-wt002", sink="history_kafka", devices=["WT002"]),
        },
    )


# ---- 入库模板与能力声明 ---------------------------------------------------


def test_shipped_kafka_110_template_declares_device_concurrency() -> None:
    document = yaml.safe_load(SHIPPED_110.read_bytes())
    assert document["template_id"] == "kafka_history"
    assert document["file_version"] == "1.1.0"
    caps = document["capabilities"]
    assert caps["supports_concurrent_write"] is True
    assert caps["max_safe_inflight"] == 4
    assert caps["reliable"] is True and caps["realtime"] is False
    assert document["output"]["acks"] == "all"
    assert document["output"]["enable_idempotence"] is True
    # 入库模板本身必须能通过完整快照校验。
    docs = _two_device_docs()
    docs[KAFKA_PATH_110] = document
    _validate(docs)


def test_legacy_100_template_remains_serial_capability() -> None:
    document = yaml.safe_load(
        (REPO_ROOT / "configs/templates/sinks/kafka_history_1.0.0.yaml").read_bytes()
    )
    assert document["capabilities"]["supports_concurrent_write"] is False
    assert document["capabilities"]["max_safe_inflight"] == 1


# ---- 接受路径 -------------------------------------------------------------


def test_distinct_devices_share_one_concurrent_sink() -> None:
    snapshot = _validate(_two_device_docs())
    assert set(snapshot.deliveries) == {"d-wt001", "d-wt002"}
    assert {d["sink_id"] for d in snapshot.deliveries.values()} == {"history_kafka"}


def test_distinct_devices_same_sink_accept_overlapping_points() -> None:
    # 设备互斥即满足要求；测点选择器相同不阻断并发配置。
    docs = _two_device_docs()
    _validate(docs)


def test_concurrent_delivery_accepts_single_legal_device_token() -> None:
    docs = _docs(
        sinks={"history_kafka": _sink_instance("history_kafka", version="1.1.0")},
        deliveries={"d1": _delivery_instance("d1", sink="history_kafka", devices=["WT-001_A"])},
    )
    _validate(docs)


# ---- 拒绝路径：设备选择器 -------------------------------------------------


@pytest.mark.parametrize(
    "bad_devices",
    [
        [],  # 空设备选择器是通配，不允许进入并发路径
        ["WT001", "WT002"],  # 多设备走旧串行路径，并发模式拒绝
    ],
)
def test_concurrent_delivery_requires_exactly_one_device(bad_devices: list[str]) -> None:
    docs = _docs(
        sinks={"history_kafka": _sink_instance("history_kafka", version="1.1.0")},
        deliveries={"d1": _delivery_instance("d1", sink="history_kafka", devices=bad_devices)},
    )
    with pytest.raises(ConfigurationError, match="concurrent Kafka requires one explicit device"):
        _validate(docs)


@pytest.mark.parametrize("bad_token", ["WT.001", "WT*1", "WT>1", "WT 001", "WT\t1"])
def test_concurrent_delivery_rejects_illegal_subject_token(bad_token: str) -> None:
    docs = _docs(
        sinks={"history_kafka": _sink_instance("history_kafka", version="1.1.0")},
        deliveries={"d1": _delivery_instance("d1", sink="history_kafka", devices=[bad_token])},
    )
    # v1_47 起设备 token 校验对全部 Delivery 生效（不再限于并发 Kafka），
    # 拒绝行为不变，错误位置与消息更新为通用 source device token。
    with pytest.raises(ConfigurationError, match="invalid source device token"):
        _validate(docs)


# ---- 拒绝路径：同目标占用与额度绕过 ---------------------------------------


def test_same_device_cannot_occupy_same_kafka_target_twice() -> None:
    docs = _docs(
        sinks={"history_kafka": _sink_instance("history_kafka", version="1.1.0")},
        deliveries={
            "d1": _delivery_instance("d1", sink="history_kafka", devices=["WT001"]),
            "d2": _delivery_instance(
                "d2", sink="history_kafka", devices=["WT001"], points=["voltage"]
            ),
        },
    )
    with pytest.raises(ConfigurationError, match="concurrent Kafka device overlaps"):
        _validate(docs)


def test_distinct_sink_ids_same_physical_target_cannot_bypass_quota() -> None:
    docs = _docs(
        sinks={
            "kafka_a": _sink_instance("kafka_a", version="1.1.0"),
            "kafka_b": _sink_instance("kafka_b", version="1.1.0"),
        },
        deliveries={
            "d1": _delivery_instance("d1", sink="kafka_a", devices=["WT001"]),
            "d2": _delivery_instance("d2", sink="kafka_b", devices=["WT002"]),
        },
    )
    with pytest.raises(ConfigurationError, match="concurrent Kafka target must reuse Sink"):
        _validate(docs)


def test_mixed_concurrent_and_legacy_sink_ids_same_target_rejected() -> None:
    docs = _docs(
        templates=("1.0.0", "1.1.0"),
        sinks={
            "kafka_new": _sink_instance("kafka_new", version="1.1.0"),
            "kafka_old": _sink_instance("kafka_old", version="1.0.0"),
        },
        deliveries={
            "d1": _delivery_instance("d1", sink="kafka_new", devices=["WT001"]),
            "d2": _delivery_instance("d2", sink="kafka_old", devices=["WT002"]),
        },
    )
    with pytest.raises(ConfigurationError, match="concurrent Kafka target must reuse Sink"):
        _validate(docs)


def test_same_device_on_different_topics_is_independent() -> None:
    # 物理目标不同（topic 不同），不构成同一目标的重复占用。
    docs = _docs(
        sinks={
            "kafka_a": _sink_instance("kafka_a", version="1.1.0"),
            "kafka_b": _sink_instance("kafka_b", version="1.0.0"),
        },
        deliveries={
            "d1": _delivery_instance("d1", sink="kafka_a", devices=["WT001"]),
            "d2": _delivery_instance("d2", sink="kafka_b", devices=["WT001"]),
        },
    )
    # topic 属于模板契约：1.0.0 路径放置同能力但不同 topic 的独立模板。
    extra = copy.deepcopy(docs[KAFKA_PATH_110])
    extra["file_version"] = "1.0.0"
    extra["output"]["topic"] = "windhub.other"
    docs[KAFKA_PATH_100] = extra
    _validate(docs)


# ---- 旧模板串行路径不退化 -------------------------------------------------


def test_legacy_template_still_allows_multi_device_delivery() -> None:
    docs = _docs(
        templates=("1.0.0",),
        sinks={"history_kafka": _sink_instance("history_kafka", version="1.0.0")},
        deliveries={
            "d1": _delivery_instance(
                "d1", sink="history_kafka", devices=["WT001", "WT002", "WT003"]
            )
        },
    )
    _validate(docs)


def test_legacy_template_allows_empty_device_wildcard() -> None:
    docs = _docs(
        templates=("1.0.0",),
        sinks={"history_kafka": _sink_instance("history_kafka", version="1.0.0")},
        deliveries={"d1": _delivery_instance("d1", sink="history_kafka", devices=[])},
    )
    _validate(docs)


def test_non_concurrent_capability_requires_inflight_one_shipped() -> None:
    # 能力声明自身不一致时 fail-closed：并发关闭却声明 >1 在途。
    docs = _two_device_docs()
    docs[KAFKA_PATH_110]["capabilities"]["supports_concurrent_write"] = False
    with pytest.raises(ConfigurationError, match="unsafe parallel write declaration"):
        _validate(docs)


def test_snapshot_files_remain_content_addressed() -> None:
    first = _validate(_two_device_docs())
    reordered = tuple(reversed(snapshot_files(_two_device_docs())))
    second = validate_snapshot(reordered)
    assert first.revision == second.revision
    assert all(isinstance(item, SnapshotFile) for item in second.files)
