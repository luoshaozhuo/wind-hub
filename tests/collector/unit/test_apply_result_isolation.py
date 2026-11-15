"""Collector task Apply failures are scoped to the requested device."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from collector.infrastructure.adapters.inbound.grpc.collector_service import CollectorDeviceRpc
from collector.infrastructure.adapters.inbound.grpc.config_service import CollectorConfigRpc
from contracts import common_pb2 as common


async def test_other_device_apply_error_does_not_contaminate_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def authorize(context: Any) -> None:
        return None

    monkeypatch.setattr(CollectorConfigRpc, "_authorize", authorize)
    status = SimpleNamespace(disk_revision="rev-2", apply_errors={"device-a": "close unsettled"})

    async def set_desired(device_id: str, state: str, *, expected_revision: str) -> Any:
        assert (device_id, state, expected_revision) == ("device-b", "START", "rev-1")
        return status

    service = CollectorDeviceRpc.__new__(CollectorDeviceRpc)
    service._control = SimpleNamespace(set_desired=set_desired)
    service._acquisition = SimpleNamespace(
        workers={"device-b": SimpleNamespace(actual_state="RUNNING")}
    )

    result = await service._control_task(
        common.OperationRequest(id="device-b", expected_revision="rev-1"), object(), "START"
    )
    assert result.accepted
    assert result.actual_state == "RUNNING"
    assert result.error_code == ""
    assert result.message == ""


async def test_requested_device_apply_error_is_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def authorize(context: Any) -> None:
        return None

    monkeypatch.setattr(CollectorConfigRpc, "_authorize", authorize)
    status = SimpleNamespace(disk_revision="rev-2", apply_errors={"device-a": "apply failed"})

    async def set_desired(device_id: str, state: str, *, expected_revision: str) -> Any:
        return status

    service = CollectorDeviceRpc.__new__(CollectorDeviceRpc)
    service._control = SimpleNamespace(set_desired=set_desired)
    service._acquisition = SimpleNamespace(
        workers={"device-a": SimpleNamespace(actual_state="FAULTED")}
    )

    result = await service._control_task(
        common.OperationRequest(id="device-a", expected_revision="rev-1"), object(), "START"
    )
    assert result.accepted
    assert result.error_code == "APPLY_FAILED"
    assert result.message == "apply failed"
