"""Stage-one configuration contract validation (v1_20 sections 11.2.9, 11.3).

The v1_20 snapshot is a single-Main-Service closure: exactly one
``configs/services/sink_service.yaml`` plus the Instance files and exact-version
templates it references. Legacy dual-process fields (``process_role``,
``config_writer``, ``config_sync.writer_endpoint``) are rejected by production.
"""

from __future__ import annotations

import copy

import pytest

from sink_service.application.config import snapshot_from_wire, validate_snapshot
from sink_service.domain.config import ConfigurationError, SnapshotFile
from tests.sink_service._stage1_fixtures import (
    DELIVERIES_INSTANCE,
    SERVICE_FILE,
    SINKS_INSTANCE,
    TEMPLATE_ROOT,
    snapshot_docs,
    snapshot_files,
    wire_files,
)

pytestmark = [pytest.mark.unit, pytest.mark.contract]


def _valid_files() -> tuple[SnapshotFile, ...]:
    return snapshot_files(snapshot_docs())


def test_valid_reference_snapshot_passes() -> None:
    snapshot = validate_snapshot(_valid_files())
    assert set(snapshot.sinks) == {"ems_redis", "history_kafka"}
    assert set(snapshot.deliveries) == {"ems_power", "power_history"}
    assert len(snapshot.revision) == 64


def test_digest_revision_is_content_addressed_and_order_independent() -> None:
    first = validate_snapshot(_valid_files())
    reordered = tuple(reversed(_valid_files()))
    second = validate_snapshot(reordered)
    assert first.revision == second.revision


@pytest.mark.parametrize(
    "relative_path",
    [
        "",
        "/etc/passwd",
        "../escape.yaml",
        "configs/services/../evil.yaml",
        "configs/services/sub/../../evil.yaml",
        "configs/services/evil.txt",
        "evil/x.yaml",
        "configs\\services\\evil.yaml",
        "configs//services/evil.yaml",
        "configs/./services/evil.yaml",
    ],
)
def test_snapshot_file_rejects_unsafe_paths(relative_path: str) -> None:
    with pytest.raises(ConfigurationError):
        SnapshotFile(relative_path, b"schema_version: 1\n")


def test_empty_and_oversized_snapshots_rejected() -> None:
    with pytest.raises(ConfigurationError):
        validate_snapshot(())
    files = list(_valid_files())
    big = SnapshotFile(f"{TEMPLATE_ROOT}/sinks/big_1.0.0.yaml", b"x: " + b"a" * (2 * 1024 * 1024))
    with pytest.raises(ConfigurationError):
        validate_snapshot(tuple(files + [big]))


def test_duplicate_paths_rejected() -> None:
    files = list(_valid_files())
    files.append(files[0])
    with pytest.raises(ConfigurationError):
        validate_snapshot(tuple(files))


def _docs_without(path: str) -> dict:
    docs = snapshot_docs()
    del docs[path]
    return docs


def test_snapshot_requires_exactly_one_service_file() -> None:
    # Removing the sole Service file breaks the closure.
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(_docs_without(SERVICE_FILE)))
    # A second Service file is rejected: Main owns the single Service document.
    docs = snapshot_docs()
    docs["configs/services/sink_other.yaml"] = copy.deepcopy(docs[SERVICE_FILE])
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_legacy_process_role_rejected() -> None:
    docs = snapshot_docs()
    docs[SERVICE_FILE]["process_role"] = "realtime"
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_legacy_config_writer_rejected() -> None:
    docs = snapshot_docs()
    docs[SERVICE_FILE]["config_writer"] = True
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_legacy_config_sync_forwarding_fields_rejected() -> None:
    for legacy_field in ("writer_endpoint", "poll_interval_ms"):
        docs = snapshot_docs()
        docs[SERVICE_FILE]["config_sync"][legacy_field] = "127.0.0.1:9999"
        with pytest.raises(ConfigurationError):
            validate_snapshot(snapshot_files(docs))


def test_service_config_root_must_be_nonempty() -> None:
    docs = snapshot_docs()
    docs[SERVICE_FILE]["config_root"] = ""
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


@pytest.mark.parametrize(
    ("section", "field", "bad"),
    [
        ("grpc", "listen_port", 70000),
        ("grpc", "max_active_rpcs", 65),
        ("grpc", "max_request_bytes", 2 * 1024**2 + 1),
        ("grpc", "default_deadline_ms", 5001),
        ("runtime", "max_inflight_per_sink", 129),
        ("metrics", "metrics_compute_interval_ms", 99),
        ("metrics", "metrics_window_ms", 999),
    ],
)
def test_service_value_ranges_enforced(section: str, field: str, bad: int) -> None:
    docs = snapshot_docs()
    docs[SERVICE_FILE][section][field] = bad
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_missing_template_reference_rejected() -> None:
    docs = _docs_without(f"{TEMPLATE_ROOT}/sinks/redis_realtime_1.0.0.yaml")
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_template_identity_must_match_reference() -> None:
    docs = snapshot_docs()
    docs[f"{TEMPLATE_ROOT}/sinks/redis_realtime_1.0.0.yaml"]["template_id"] = "other"
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_unknown_sink_reference_rejected() -> None:
    docs = snapshot_docs()
    docs[DELIVERIES_INSTANCE]["deliveries"]["ems_power"]["sink_id"] = "ghost"
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_incompatible_sink_capability_rejected() -> None:
    docs = snapshot_docs()
    # Redis template declares reliable=false; a reliable Delivery cannot use it.
    docs[DELIVERIES_INSTANCE]["deliveries"]["power_history"]["sink_id"] = "ems_redis"
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_kafka_reliable_requires_duplicate_policy() -> None:
    docs = snapshot_docs()
    del docs[f"{TEMPLATE_ROOT}/sinks/kafka_history_1.0.0.yaml"]["output"][
        "business_duplicate_policy"
    ]
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_non_concurrent_adapter_requires_inflight_one() -> None:
    docs = snapshot_docs()
    caps = docs[f"{TEMPLATE_ROOT}/sinks/kafka_history_1.0.0.yaml"]["capabilities"]
    caps["supports_concurrent_write"] = False
    caps["max_safe_inflight"] = 2
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_wind_farm_identity_must_match_service() -> None:
    docs = snapshot_docs()
    docs[DELIVERIES_INSTANCE]["deliveries"]["ems_power"]["source"]["wind_farm_id"] = "farm-b"
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_invalid_desired_state_rejected() -> None:
    docs = snapshot_docs()
    docs[DELIVERIES_INSTANCE]["deliveries"]["ems_power"]["desired_state"] = "RUN"
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_wire_metadata_size_and_hash_validated() -> None:
    files = wire_files(_valid_files())
    path, content, size, digest = files[0]
    with pytest.raises(ConfigurationError):
        snapshot_from_wire([(path, content + b" ", size, digest), *files[1:]])
    with pytest.raises(ConfigurationError):
        snapshot_from_wire([(path, content, size + 1, digest), *files[1:]])


def test_mutating_document_changes_revision() -> None:
    before = validate_snapshot(_valid_files()).revision
    docs = snapshot_docs()
    docs[DELIVERIES_INSTANCE]["deliveries"]["ems_power"]["desired_state"] = "STOP"
    after = validate_snapshot(snapshot_files(docs)).revision
    assert before != after


def test_snapshot_returns_independent_file_tuples() -> None:
    snapshot = validate_snapshot(_valid_files())
    assert [f.path for f in snapshot.files] == sorted(f.path for f in snapshot.files)
    # dataclass is frozen
    with pytest.raises((AttributeError, Exception)):
        snapshot.revision = "x"  # type: ignore[misc]


def test_missing_required_instance_file_rejected() -> None:
    docs = snapshot_docs()
    docs.pop(SINKS_INSTANCE)
    # A stray sink template without the instance file must not pass.
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_copy_helper_is_deep() -> None:
    docs = snapshot_docs()
    docs[SERVICE_FILE]["grpc"]["listen_port"] = 55555
    fresh = snapshot_docs()
    assert fresh[SERVICE_FILE]["grpc"]["listen_port"] == 50061
    _ = copy.copy(fresh)
