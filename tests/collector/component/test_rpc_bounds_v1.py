"""Focused adapter regressions: active-I/O deadlines and core error mapping."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, cast

import grpc
import pytest

from collector.infrastructure.adapters.inbound.grpc.collector_service import CollectorDeviceRpc
from contracts import collector_pb2 as pb
from core.application.errors import ProtocolError
from core.domain import PointAccess

pytestmark = [pytest.mark.component]

IEC_POINT_TABLE = SimpleNamespace(
    protocol=SimpleNamespace(name="iec104"),
    points={"meas_power": SimpleNamespace(access=PointAccess.READ, point_id="meas_power")},
)
IEC_PLAN = SimpleNamespace(point_table=IEC_POINT_TABLE)


class _AbortError(Exception):
    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        super().__init__(details)
        self.code = code
        self.details = details


class _FakeContext:
    def __init__(self, token: str) -> None:
        self._metadata = (("x-windhub-admin-token", token),)

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return self._metadata

    async def abort(self, code: grpc.StatusCode, details: str) -> None:
        raise _AbortError(code, details)


class _SlowInterrogationWorker:
    timeout = 0.2
    plan = IEC_PLAN

    def submit_interrogation(self, request: Any) -> asyncio.Task[Any]:
        async def hang() -> None:
            await asyncio.sleep(30)

        return asyncio.ensure_future(hang())


class _FailingReadWorker:
    timeout = 5.0
    plan = IEC_PLAN

    def submit_read(self, point_ids: tuple[str, ...]) -> asyncio.Task[Any]:
        async def fail() -> Any:
            raise ProtocolError("protocol returned an unrequested point")

        return asyncio.ensure_future(fail())


class _Acquisition:
    def __init__(self, workers: dict[str, Any]) -> None:
        self.workers = workers


def _rpc(worker: Any) -> CollectorDeviceRpc:
    return CollectorDeviceRpc(
        cast(Any, _Acquisition({"iec104-1": worker})),
        cast(Any, None),
        cast(Any, None),
        None,
    )


async def test_interrogate_timeout_ms_zero_uses_server_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", "unit-token")
    rpc = _rpc(_SlowInterrogationWorker())

    started = time.monotonic()
    response = await rpc.InterrogateDevice(
        pb.InterrogateDeviceRequest(device_id="iec104-1"),
        _FakeContext("unit-token"),
    )
    elapsed = time.monotonic() - started

    # timeout_ms unset (0) must fall back to the server upper bound, never wait
    # without a deadline and pin the device session lock.
    assert elapsed < 2.0
    assert response.status == "UNKNOWN"
    assert response.error_code == "TIMEOUT_UNCONFIRMED"


async def test_interrogate_explicit_timeout_is_capped_at_server_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", "unit-token")
    rpc = _rpc(_SlowInterrogationWorker())

    started = time.monotonic()
    response = await rpc.InterrogateDevice(
        pb.InterrogateDeviceRequest(device_id="iec104-1", timeout_ms=60_000),
        _FakeContext("unit-token"),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert response.status == "UNKNOWN"


async def test_protocol_error_maps_to_declared_grpc_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", "unit-token")
    rpc = _rpc(_FailingReadWorker())

    # A stable core ProtocolError must not escape the servicer as framework
    # UNKNOWN; it maps to a declared status via the shared error table.
    with pytest.raises(_AbortError) as exc:
        await rpc.ReadPoints(
            pb.ReadPointsRequest(device_id="iec104-1", point_ids=["meas_power"]),
            _FakeContext("unit-token"),
        )
    assert exc.value.code == grpc.StatusCode.FAILED_PRECONDITION
