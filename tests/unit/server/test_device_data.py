"""Server Device Data 使用 Collector 真实采集读模型。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from wind_hub_core.model.point import PointValue
from wind_hub_server.application.usecase.collector_aggregate import (
    CollectorAggregateUseCase,
)
from wind_hub_server.application.usecase.device_data import DeviceDataUseCase


class _Collector:
    def __init__(
        self,
        worker_id: str,
        *,
        latest: list[PointValue],
        trend: dict[str, list[PointValue]],
    ) -> None:
        self.worker_id = worker_id
        self._latest = latest
        self._trend = trend

    async def config_status(self) -> dict[str, object]:
        return {"collector_id": self.worker_id}

    async def latest_telemetry(self, device_id: str) -> list[PointValue]:
        assert device_id == "wtg-01"
        return list(self._latest)

    async def telemetry_trend(
        self,
        device_id: str,
        point_ids: list[str],
        *,
        since: datetime | None = None,
        limit_per_point: int = 600,
    ) -> dict[str, list[PointValue]]:
        assert device_id == "wtg-01"
        del since
        return {
            point_id: list(self._trend.get(point_id, []))[-limit_per_point:]
            for point_id in point_ids
        }


class _Directory:
    def __init__(self, collectors: dict[str, _Collector]) -> None:
        self._collectors = collectors

    def get(self, worker_id: str) -> _Collector:
        return self._collectors[worker_id]

    def list_worker_ids(self) -> list[str]:
        return sorted(self._collectors)


class _Assignments:
    def worker_ids_for_device(self, device_id: str) -> list[str]:
        assert device_id == "wtg-01"
        return ["collector-a", "collector-b"]


def _config() -> SimpleNamespace:
    point = SimpleNamespace(
        point_id="power",
        variable_name=".grid_active_power",
        point_groups=["telemetry"],
        data_type="float32",
        unit="kw",
        description="Active power",
    )
    current = SimpleNamespace(
        devices=SimpleNamespace(
            devices=[SimpleNamespace(device_id="wtg-01", point_table="wtg")]
        ),
        point_tables=SimpleNamespace(
            tables={"wtg": SimpleNamespace(points=[point])}
        ),
        units=SimpleNamespace(units={"kw": SimpleNamespace(symbol="kW")}),
    )
    return SimpleNamespace(current_config=current)


@pytest.mark.asyncio
async def test_device_data_uses_newest_collector_sample() -> None:
    now = datetime.now(UTC)
    collectors = _Directory(
        {
            "collector-a": _Collector(
                "collector-a",
                latest=[
                    PointValue(
                        device_id="wtg-01",
                        point_id="power",
                        value=1.0,
                        timestamp=now,
                    )
                ],
                trend={},
            ),
            "collector-b": _Collector(
                "collector-b",
                latest=[
                    PointValue(
                        device_id="wtg-01",
                        point_id="power",
                        value=2.0,
                        timestamp=now + timedelta(seconds=1),
                    )
                ],
                trend={},
            ),
        }
    )
    aggregate = CollectorAggregateUseCase(
        collectors,  # type: ignore[arg-type]
        _Assignments(),  # type: ignore[arg-type]
        _config(),  # type: ignore[arg-type]
    )
    usecase = DeviceDataUseCase(_config(), aggregate)  # type: ignore[arg-type]

    rows = await usecase.list_data("wtg-01")

    assert len(rows) == 1
    assert rows[0].value == pytest.approx(2.0)
    assert rows[0].unit_symbol == "kW"


@pytest.mark.asyncio
async def test_trend_merges_collectors_and_applies_limit() -> None:
    now = datetime.now(UTC)
    collectors = _Directory(
        {
            "collector-a": _Collector(
                "collector-a",
                latest=[],
                trend={
                    "power": [
                        PointValue(
                            device_id="wtg-01",
                            point_id="power",
                            value=1.0,
                            timestamp=now,
                        ),
                        PointValue(
                            device_id="wtg-01",
                            point_id="power",
                            value=3.0,
                            timestamp=now + timedelta(seconds=2),
                        ),
                    ]
                },
            ),
            "collector-b": _Collector(
                "collector-b",
                latest=[],
                trend={
                    "power": [
                        PointValue(
                            device_id="wtg-01",
                            point_id="power",
                            value=2.0,
                            timestamp=now + timedelta(seconds=1),
                        )
                    ]
                },
            ),
        }
    )
    aggregate = CollectorAggregateUseCase(
        collectors,  # type: ignore[arg-type]
        _Assignments(),  # type: ignore[arg-type]
        _config(),  # type: ignore[arg-type]
    )
    usecase = DeviceDataUseCase(_config(), aggregate)  # type: ignore[arg-type]

    series = await usecase.trend(
        "wtg-01",
        ["power"],
        window_seconds=3600,
        limit_per_point=2,
    )

    assert [sample.value for sample in series[0].samples] == [2.0, 3.0]
