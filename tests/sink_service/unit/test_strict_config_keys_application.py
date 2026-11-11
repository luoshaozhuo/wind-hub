"""R13（v1_47）application 快照层严格键集拒绝矩阵。

domain 层的同名校验由 test_strict_config_keys_domain.py 钉死；本文件覆盖
``application/config.py`` 中 Push/Apply 前的实例与模板白名单：

- sink/delivery 实例、connection/output/capabilities/reliable 各节未知键拒绝；
- delivery 模板出现非空 source_filter 必须拒绝（不支持的规则不得静默忽略）；
- 模板顶层未知键、实例容器未知键拒绝；
- 基准快照正向通过，防止严格校验误拒合法配置。
"""

from __future__ import annotations

import copy

import pytest

from sink_service.application.config import validate_snapshot
from sink_service.domain.config import ConfigurationError
from tests.sink_service._stage1_fixtures import (
    DELIVERIES_INSTANCE,
    SERVICE_FILE,
    SINKS_INSTANCE,
    snapshot_docs,
    snapshot_files,
    template_path,
)

pytestmark = [pytest.mark.unit, pytest.mark.contract]


def _validate(docs: dict) -> object:
    return validate_snapshot(snapshot_files(docs))


def test_reference_snapshot_passes_application_validation() -> None:
    _validate(snapshot_docs())


def _mutate(mutator) -> dict:
    docs = snapshot_docs()
    mutator(docs)
    return docs


def test_unknown_sink_instance_key_rejected() -> None:
    def mutator(docs: dict) -> None:
        docs[SINKS_INSTANCE]["sinks"]["history_kafka"]["surprise_field"] = 1

    with pytest.raises(ConfigurationError, match="unknown"):
        _validate(_mutate(mutator))


def test_unknown_sink_instance_container_key_rejected() -> None:
    def mutator(docs: dict) -> None:
        docs[SINKS_INSTANCE]["surprise_container"] = {}

    with pytest.raises(ConfigurationError, match="unknown"):
        _validate(_mutate(mutator))


def test_unknown_kafka_connection_key_rejected() -> None:
    def mutator(docs: dict) -> None:
        docs[SINKS_INSTANCE]["sinks"]["history_kafka"]["connection"]["sasl_mech"] = "plain"

    with pytest.raises(ConfigurationError, match="unknown"):
        _validate(_mutate(mutator))


def test_unknown_kafka_output_key_rejected() -> None:
    kafka_template = template_path("kafka_history")

    def mutator(docs: dict) -> None:
        docs[kafka_template]["output"]["compression_type"] = "gzip"

    with pytest.raises(ConfigurationError, match="unknown"):
        _validate(_mutate(mutator))


def test_unknown_capability_key_rejected() -> None:
    kafka_template = template_path("kafka_history")

    def mutator(docs: dict) -> None:
        docs[kafka_template]["capabilities"]["quota_hint"] = 42

    with pytest.raises(ConfigurationError, match="unknown"):
        _validate(_mutate(mutator))


def test_unknown_delivery_instance_key_rejected() -> None:
    def mutator(docs: dict) -> None:
        docs[DELIVERIES_INSTANCE]["deliveries"]["power_history"]["priority"] = 9

    with pytest.raises(ConfigurationError, match="unknown"):
        _validate(_mutate(mutator))


def test_unknown_delivery_source_key_rejected() -> None:
    def mutator(docs: dict) -> None:
        docs[DELIVERIES_INSTANCE]["deliveries"]["power_history"]["source"]["region"] = "north"

    with pytest.raises(ConfigurationError, match="unknown"):
        _validate(_mutate(mutator))


def test_unknown_reliable_template_key_rejected() -> None:
    reliable_template = template_path("reliable_history")

    def mutator(docs: dict) -> None:
        docs[reliable_template]["reliable"]["jitter_ms"] = 50

    with pytest.raises(ConfigurationError, match="unknown"):
        _validate(_mutate(mutator))


def test_unknown_template_top_level_key_rejected() -> None:
    reliable_template = template_path("reliable_history")

    def mutator(docs: dict) -> None:
        docs[reliable_template]["metadata"] = {"owner": "team-x"}

    with pytest.raises(ConfigurationError, match="unknown"):
        _validate(_mutate(mutator))


def test_non_empty_source_filter_rejected() -> None:
    reliable_template = template_path("reliable_history")

    def mutator(docs: dict) -> None:
        docs[reliable_template]["source_filter"] = {"quality": ["good"]}

    with pytest.raises(ConfigurationError, match="unsupported source_filter"):
        _validate(_mutate(mutator))


def test_empty_source_filter_mapping_remains_allowed() -> None:
    # {} 是本版本唯一受支持形态：不过滤质量，good/bad/uncertain 全部派发。
    docs = snapshot_docs()
    reliable_template = template_path("reliable_history")
    assert docs[reliable_template]["source_filter"] == {}
    _validate(copy.deepcopy(docs))


def test_unknown_service_caller_permission_rejected_end_to_end() -> None:
    def mutator(docs: dict) -> None:
        callers = docs[SERVICE_FILE]["security"]["callers"]
        callers[0]["permissions"] = ["query", "destroy"]

    with pytest.raises(ConfigurationError, match="permission"):
        _validate(_mutate(mutator))
