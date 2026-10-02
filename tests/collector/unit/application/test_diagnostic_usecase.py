"""DiagnosticUseCase 的 ADS 地址事实验证。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub.application.runtime.device import Device
from wind_hub.application.usecase.diagnostic import DiagnosticUseCase
from wind_hub_core.config.schema import DeviceConfig, Endpoint, PointAddress, PointConfig
from wind_hub_core.protocol.port import ProtocolPort
from wind_hub_core.validation.models import AddressResolution


@pytest.mark.asyncio
async def test_ads_verify_point_reads_probe_resolved_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADS verify-point 必须在同一 Probe session 中 resolve 后按该地址读取。"""
    point = PointConfig(
        point_id="wind_speed",
        point_groups=["g"],
        address=PointAddress(
            symbol=".wind_speed",
            index_group=0x4020,
            index_offset=1,
        ),
        data_type="float32",
        scale=2.0,
        offset=1.0,
    )
    cfg = DeviceConfig(
        device_id="d1",
        protocol="ads",
        point_table="t1",
        endpoint=Endpoint(
            host="192.168.1.10",
            port=48898,
            extensions={"target_net_id": "192.168.1.10.1.1"},
        ),
    )
    protocol = MagicMock(spec=ProtocolPort)
    protocol.read = AsyncMock()
    device = Device(cfg, [point], protocol)
    runtime = MagicMock()
    runtime.devices = {"d1": device}

    class FakeProbe:
        last_read_address: tuple[int, int] | None = None

        def __init__(self, target: object) -> None:
            self.target = target

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def resolve_points(self, specs: list[object]) -> dict[str, AddressResolution]:
            spec = specs[0]
            return {
                spec.point_id: AddressResolution(
                    point_id=spec.point_id,
                    symbol=".wind_speed",
                    index_group=0x4020,
                    index_offset=200,
                    size=4,
                    protocol_type="REAL",
                )
            }

        async def read_value(self, spec: object, resolution: AddressResolution) -> object:
            type(self).last_read_address = (
                int(resolution.index_group),
                int(resolution.index_offset),
            )
            return 12.5

    monkeypatch.setattr(
        "wind_hub.application.usecase.diagnostic.ADSProbe",
        FakeProbe,
    )

    result = await DiagnosticUseCase(runtime).verify_point("d1", "wind_speed")

    assert FakeProbe.last_read_address == (0x4020, 200)
    assert result.resolved_address is not None
    assert result.resolved_address["index_offset"] == 200
    assert result.raw_value == 12.5
    assert result.engineering_value == 26.0
    assert result.ok is False
    assert result.code.value == "POINT_MAPPING_MISMATCH"
    protocol.read.assert_not_awaited()
