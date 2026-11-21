"""Full-root service source references must be canonical and not escape publication."""

from __future__ import annotations

import pytest

from configuration_service.application.service import ConfigurationApplication
from configuration_service.domain.models import ConfigIdentity, PreparedDocument


def _document(config_id: str) -> PreparedDocument:
    return PreparedDocument(
        identity=ConfigIdentity("device", config_id),
        schema_ref="device:1",
        file_version="1.0.0",
        content_sha256="0" * 64,
        canonical_payload={"schema_version": 1, "file_version": "1.0.0", "devices": {}},
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("configs/instances/devices.yaml", "real"),
        ("configs/instances/devices_sim.yaml", "sim"),
    ],
)
def test_source_paths_bind_only_available_device_instances(
    app: ConfigurationApplication, path: str, expected: str
) -> None:
    documents = (_document("devices"), _document("devices_sim"))
    version_ids = {
        ("device", "devices", "1.0.0"): "real",
        ("device", "devices_sim", "1.0.0"): "sim",
    }
    result = app._resolve_legacy_sources(
        {"instances": {"devices": path}},
        documents,
        version_ids,
    )
    assert result == [(expected, "config_sources.instances")]


@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",
        "configs/instances/../instances/devices.yaml",
        "configs/instances/devices.yml",
        "configs/instances/sinks.yaml",
        "/configs/instances/devices.yaml",
    ],
)
def test_source_paths_reject_ambiguous_or_noncanonical_instance(
    app: ConfigurationApplication, path: str
) -> None:
    document = _document("devices")
    with pytest.raises(ValueError, match="invalid config_sources.instances.devices path"):
        app._resolve_legacy_sources(
            {"instances": {"devices": path}},
            (document,),
            {("device", "devices", "1.0.0"): "real"},
        )
