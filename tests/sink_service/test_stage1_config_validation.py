"""Stage-one configuration contract validation (design sections 11.2.9, 11.3)."""

from __future__ import annotations

import copy

import pytest

from sink_service.application.config import snapshot_from_wire, validate_snapshot
from sink_service.domain.config import ConfigurationError, SnapshotFile
from tests.sink_service._stage1_fixtures import snapshot_docs, snapshot_files, wire_files

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
        "services/../evil.yaml",
        "services/sub/../../evil.yaml",
        "services/evil.txt",
        "evil/x.yaml",
        "services\\evil.yaml",
        "services//evil.yaml",
        "services/./evil.yaml",
    ],
)
def test_snapshot_file_rejects_unsafe_paths(relative_path: str) -> None:
    with pytest.raises(ConfigurationError):
        SnapshotFile(relative_path, b"schema_version: 1\n")


def test_empty_and_oversized_snapshots_rejected() -> None:
    with pytest.raises(ConfigurationError):
        validate_snapshot(())
    files = list(_valid_files())
    big = SnapshotFile("templates/sinks/big/1.0.0.yaml", b"x: " + b"a" * (2 * 1024 * 1024))
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


def test_snapshot_requires_both_service_files() -> None:
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(_docs_without("services/sink-reliable.yaml")))


def test_roles_must_be_distinct() -> None:
    docs = snapshot_docs()
    docs["services/sink-reliable.yaml"]["process_role"] = "realtime"
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_exactly_one_writer_required() -> None:
    docs = snapshot_docs()
    docs["services/sink-reliable.yaml"]["config_writer"] = True
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))
    docs = snapshot_docs()
    docs["services/sink-realtime.yaml"]["config_writer"] = False
    docs["services/sink-reliable.yaml"]["config_writer"] = False
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_grpc_ports_must_be_distinct() -> None:
    docs = snapshot_docs()
    docs["services/sink-reliable.yaml"]["grpc"]["listen_port"] = 50061
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_config_root_must_match_across_processes() -> None:
    docs = snapshot_docs()
    docs["services/sink-reliable.yaml"]["config_root"] = "/other/root"
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_writer_must_not_forward_to_another_endpoint() -> None:
    docs = snapshot_docs()
    docs["services/sink-realtime.yaml"]["config_sync"]["writer_endpoint"] = "127.0.0.1:9999"
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_reader_requires_writer_endpoint() -> None:
    docs = snapshot_docs()
    docs["services/sink-reliable.yaml"]["config_sync"]["writer_endpoint"] = ""
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


@pytest.mark.parametrize(
    ("section", "field", "bad"),
    [
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
    docs["services/sink-realtime.yaml"][section][field] = bad
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_missing_template_reference_rejected() -> None:
    docs = _docs_without("templates/sinks/redis_realtime/1.0.0.yaml")
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_template_identity_must_match_reference() -> None:
    docs = snapshot_docs()
    docs["templates/sinks/redis_realtime/1.0.0.yaml"]["template_id"] = "other"
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_unknown_sink_reference_rejected() -> None:
    docs = snapshot_docs()
    docs["instances/deliveries.yaml"]["deliveries"]["ems_power"]["sink_id"] = "ghost"
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_incompatible_sink_capability_rejected() -> None:
    docs = snapshot_docs()
    # Redis template declares reliable=false.
    docs["instances/deliveries.yaml"]["deliveries"]["power_history"]["sink_id"] = "ems_redis"
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_kafka_reliable_requires_duplicate_policy() -> None:
    docs = snapshot_docs()
    del docs["templates/sinks/kafka_history/1.0.0.yaml"]["output"]["business_duplicate_policy"]
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_non_concurrent_adapter_requires_inflight_one() -> None:
    docs = snapshot_docs()
    caps = docs["templates/sinks/kafka_history/1.0.0.yaml"]["capabilities"]
    caps["supports_concurrent_write"] = False
    caps["max_safe_inflight"] = 2
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_wind_farm_identity_must_match_service() -> None:
    docs = snapshot_docs()
    docs["instances/deliveries.yaml"]["deliveries"]["ems_power"]["source"]["wind_farm_id"] = (
        "farm-b"
    )
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_invalid_desired_state_rejected() -> None:
    docs = snapshot_docs()
    docs["instances/deliveries.yaml"]["deliveries"]["ems_power"]["desired_state"] = "RUN"
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
    docs["instances/deliveries.yaml"]["deliveries"]["ems_power"]["desired_state"] = "STOP"
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
    docs.pop("instances/sinks.yaml")
    # A stray sink template without the instance file must not pass.
    with pytest.raises(ConfigurationError):
        validate_snapshot(snapshot_files(docs))


def test_copy_helper_is_deep() -> None:
    docs = snapshot_docs()
    docs["services/sink-realtime.yaml"]["grpc"]["listen_port"] = 55555
    fresh = snapshot_docs()
    assert fresh["services/sink-realtime.yaml"]["grpc"]["listen_port"] == 50061
    _ = copy.copy(fresh)
