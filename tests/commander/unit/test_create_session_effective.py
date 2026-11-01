"""create_session 三协议一致性：Modbus / ADS / IEC104 都走 effective 合成。

Core 的 resolve_effective_device 已直接覆盖三协议默认端口；这里钉住
Commander 装配层对三个 protocol.name 都无旁路地调用它，并把合成后的
endpoint/options 原样交给 ProtocolRegistry.create。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from commander.application.config_manager import CommanderConfigManager
from core.application.protocol_registry import ProtocolRegistry
from core.domain import (
    ConnectionEndpoint,
    Device,
    DeviceGroupId,
    DeviceId,
    DeviceModel,
    DeviceModelId,
    DeviceTypeId,
    PointTable,
    PointTableId,
    Protocol,
    ProtocolOptions,
)
from core.infrastructure.config.shared_snapshot import SharedDomainSnapshot


class _RecordingRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[ConnectionEndpoint, PointTable, ProtocolOptions]] = []

    def create(
        self,
        endpoint: ConnectionEndpoint,
        point_table: PointTable,
        protocol_options: ProtocolOptions,
    ) -> object:
        self.calls.append((endpoint, point_table, protocol_options))
        return object()


def _manager_with_snapshot(
    protocol: str,
    *,
    model_options: ProtocolOptions,
    device_options: ProtocolOptions,
) -> tuple[CommanderConfigManager, PointTable]:
    async def _no_quiesce(_devices: frozenset[str]) -> frozenset[str]:
        return frozenset()

    manager = CommanderConfigManager.__new__(CommanderConfigManager)
    table = PointTable(PointTableId(f"tbl-{protocol}"), Protocol(protocol), points={})
    model = DeviceModel(
        DeviceModelId("model-1"),
        DeviceTypeId("turbine"),
        table.point_table_id,
        protocol_options=model_options,
    )
    device = Device(
        DeviceId("WT-1"),
        DeviceModelId("model-1"),
        ConnectionEndpoint("127.0.0.1"),
        protocol_options=device_options,
        device_group_ids=(DeviceGroupId("g1"),),
    )
    snapshot = SimpleNamespace(
        site=SimpleNamespace(devices={DeviceId("WT-1"): device}),
        device_models={DeviceModelId("model-1"): model},
        point_tables={table.point_table_id: table},
    )
    manager._snapshot = cast(SharedDomainSnapshot, snapshot)  # noqa: SLF001
    manager._disabled = set()  # noqa: SLF001
    return manager, table


def test_create_session_merges_model_options_for_all_three_protocols() -> None:
    cases: dict[str, tuple[ProtocolOptions, ProtocolOptions, int]] = {
        "modbus": (
            {"slave_id": 3, "word_order": "big_endian"},
            {"word_order": "little_endian"},
            502,
        ),
        "iec104": ({"k": 12, "t1": 15}, {"k": 24}, 2404),
        "ads": ({"twincat_version": "3", "target_net_id": "1.1.1.1.1.1"}, {}, 851),
    }
    for protocol, (model_options, device_options, default_port) in cases.items():
        manager, table = _manager_with_snapshot(
            protocol, model_options=model_options, device_options=device_options
        )
        registry = _RecordingRegistry()
        manager.create_session("WT-1", cast(ProtocolRegistry, registry))
        assert len(registry.calls) == 1
        endpoint, passed_table, options = registry.calls[0]
        assert passed_table is table
        # 端口：实例/型号均未配置时落到协议代码默认。
        assert endpoint.port == default_port
        # 型号默认全部进入有效参数；实例覆盖型号。
        for key, value in model_options.items():
            assert options.get(key) == device_options.get(key, value)
