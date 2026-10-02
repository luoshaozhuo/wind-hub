"""配置热重载 functional 测试。

真实 Runtime + 真实 Modbus fixture：逐项验证 reload 的增量应用语义——
Task 属性（interval / targets）、点表内容与绑定、device_group 展开、
endpoint 重建、设备与 sink 增删，以及「加载失败不触碰运行态、修复后
重试成功」的失败恢复语义。
"""

from __future__ import annotations


from typing import Any

import pytest

pytestmark = pytest.mark.modbus
from wind_hub_collector.assembly import assemble, start_runtime, stop_runtime
from wind_hub_collector.domain.model.errors import CommandError

from tests.collector.functional.conftest import (
    MODBUS_POINTS,
    FunctionalContext,
    functional_sink_factory,
    modbus_device_dict,
    update_yaml,
    write_functional_config,
)
from tests.fixtures.servers.modbus_server import ModbusMockServer, _holding_registers
from tests.system.process import free_port

TASKS = "tasks.yaml"
POINTS = "points.yaml"
DEVICES = "devices.yaml"
MODELS = "device_models.yaml"
SYSTEM = "system.yaml"


def _group_task() -> dict[str, Any]:
    return {
        "task_id": "grp-telemetry",
        "device_group": "wind",
        "point_group": "telemetry",
        "interval": 0.2,
        "targets": [{"sink": "null_sink"}],
    }


def _second_device(port: int) -> dict[str, Any]:
    """unit 2 的第二台设备（fixture 内置 900.5/700.0 数据块，旧式写法）。"""
    device = modbus_device_dict(port, device_id="modbus-2", device_group="wind")
    device["endpoint"]["extensions"]["unit_id"] = 2
    return device


def _second_device_instance(port: int) -> dict[str, Any]:
    """``_second_device`` 的实例式写法——用于直接改写 devices.yaml。"""
    old = _second_device(port)
    return {
        "device_id": old["device_id"],
        "model": "modbus__modbus",
        "device_group": old["device_group"],
        "endpoint": old["endpoint"],
    }


class TestTaskChanges:
    async def test_interval_change_applies_to_instance(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        ctx = modbus_runtime

        def mutate(data: dict[str, Any]) -> None:
            data["tasks"][0]["interval"] = 0.5

        update_yaml(ctx.config_dir, TASKS, mutate)
        result = await ctx.rt.config.reload()
        assert result.success, result.errors
        assert result.diff is not None
        assert result.diff.tasks.updated == ["modbus-telemetry"]

        instance = await ctx.rt.tasks.get_instance("modbus-telemetry:modbus-1")
        assert instance.interval == 0.5

    async def test_targets_change_applies_to_instance(
        self, runtime_factory, tmp_path
    ) -> None:
        async with runtime_factory() as ctx:
            # 先在 system.yaml 注册第二个 sink，再把 task targets 切过去。
            def add_sink(data: dict[str, Any]) -> None:
                data["sinks"].append(
                    {
                        "name": "file_sink",
                        "type": "file",
                        "params": {"path": str(tmp_path / "out" / "data.jsonl")},
                    }
                )

            update_yaml(ctx.config_dir, SYSTEM, add_sink)

            def retarget(data: dict[str, Any]) -> None:
                data["tasks"][0]["targets"] = [{"sink": "file_sink"}]

            update_yaml(ctx.config_dir, TASKS, retarget)
            result = await ctx.rt.config.reload()
            assert result.success, result.errors

            instance = await ctx.rt.tasks.get_instance("modbus-telemetry:modbus-1")
            assert instance.targets == ["file_sink"]


class TestPointTableChanges:
    async def test_point_table_content_change_rebuilds_points(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        ctx = modbus_runtime

        def mutate(data: dict[str, Any]) -> None:
            data["point_tables"]["modbus"]["points"].append(
                {
                    "point_id": "gen.voltage",
                    "point_groups": ["telemetry"],
                    "address": {"register_type": "holding", "address": 104},
                    "data_type": "int16",
                }
            )

        update_yaml(ctx.config_dir, POINTS, mutate)
        result = await ctx.rt.config.reload()
        assert result.success, result.errors
        assert result.diff is not None
        assert result.diff.points_changed is True
        assert "modbus" in result.diff.point_tables_changed

        point_ids = {p.point_id for p in ctx.rt.runtime.devices["modbus-1"].points}
        assert "gen.voltage" in point_ids

    async def test_point_table_binding_change_rebinds_device(
        self, runtime_factory
    ) -> None:
        alt_table = {
            "points": [
                {
                    "point_id": "alt.metric",
                    "point_groups": ["telemetry"],
                    "address": {"register_type": "holding", "address": 100},
                    "data_type": "float32",
                }
            ]
        }
        async with runtime_factory(
            point_tables={
                "modbus": {"points": list(MODBUS_POINTS)},
                "modbus_alt": alt_table,
            },
        ) as ctx:
            def mutate(data: dict[str, Any]) -> None:
                data["device_models"]["modbus__modbus"]["point_table"] = "modbus_alt"

            update_yaml(ctx.config_dir, MODELS, mutate)
            result = await ctx.rt.config.reload()
            assert result.success, result.errors

            device = ctx.rt.runtime.devices["modbus-1"]
            assert device.config.point_table == "modbus_alt"
            assert [p.point_id for p in device.points] == ["alt.metric"]

            # 新绑定立即可读（轻量路径不断连）。
            value = await ctx.rt.query.read_point("modbus-1", "alt.metric")
            assert value.value == pytest.approx(1200.5)


class TestDeviceGroupChanges:
    async def test_added_device_joins_device_group_task(self, runtime_factory) -> None:
        async with runtime_factory(
            devices=lambda port: [modbus_device_dict(port, device_group="wind")],
            tasks=[_group_task()],
        ) as ctx:
            before = await ctx.rt.tasks.list_instances()
            assert [i.instance_id for i in before] == ["grp-telemetry:modbus-1"]

            def mutate(data: dict[str, Any]) -> None:
                data["devices"].append(_second_device_instance(ctx.port))

            update_yaml(ctx.config_dir, DEVICES, mutate)
            result = await ctx.rt.config.reload()
            assert result.success, result.errors
            assert result.diff is not None
            assert result.diff.devices.added == ["modbus-2"]

            instance_ids = {i.instance_id for i in await ctx.rt.tasks.list_instances()}
            assert instance_ids == {"grp-telemetry:modbus-1", "grp-telemetry:modbus-2"}

            # 新设备立即可读（unit 2 的 fixture 值）。
            value = await ctx.rt.query.read_point("modbus-2", "rotor.speed")
            assert value.value == pytest.approx(900.5)

    async def test_device_group_change_leaves_group_task(self, runtime_factory) -> None:
        # 两台设备同组；把 modbus-1 移出组后组内仍有 modbus-2（空组属于
        # 非法配置，loader 会拒绝——不在此断言）。
        async with runtime_factory(
            devices=lambda port: [
                modbus_device_dict(port, device_group="wind"),
                _second_device(port),
            ],
            tasks=[_group_task()],
        ) as ctx:
            def mutate(data: dict[str, Any]) -> None:
                data["devices"][0]["device_group"] = "other"

            update_yaml(ctx.config_dir, DEVICES, mutate)
            result = await ctx.rt.config.reload()
            assert result.success, result.errors

            # device_group 移出后不再展开该设备的实例。
            instance_ids = {i.instance_id for i in await ctx.rt.tasks.list_instances()}
            assert instance_ids == {"grp-telemetry:modbus-2"}
            # 设备本身仍在注册表中（轻量更新，不删设备）。
            assert "modbus-1" in ctx.rt.runtime.devices

    async def test_removed_device_drops_its_instances(self, runtime_factory) -> None:
        async with runtime_factory(
            devices=lambda port: [
                modbus_device_dict(port, device_group="wind"),
                _second_device(port),
            ],
            tasks=[_group_task()],
        ) as ctx:
            def mutate(data: dict[str, Any]) -> None:
                data["devices"] = [
                    d for d in data["devices"] if d["device_id"] != "modbus-2"
                ]

            update_yaml(ctx.config_dir, DEVICES, mutate)
            result = await ctx.rt.config.reload()
            assert result.success, result.errors
            assert result.diff is not None
            assert result.diff.devices.removed == ["modbus-2"]

            assert "modbus-2" not in ctx.rt.runtime.devices
            instance_ids = {i.instance_id for i in await ctx.rt.tasks.list_instances()}
            assert instance_ids == {"grp-telemetry:modbus-1"}

            with pytest.raises(CommandError, match="unknown device"):
                await ctx.rt.query.read_point("modbus-2", "rotor.speed")


class TestEndpointRebuild:
    async def test_endpoint_port_change_reconnects_to_new_peer(
        self, tmp_path
    ) -> None:
        port_a = free_port()
        port_b = free_port()
        server_a = ModbusMockServer(port=port_a)
        # server B 用不同的 temp.int 值以便区分对端。
        server_b = ModbusMockServer(
            port=port_b, holding=_holding_registers(1200.5, 800.0, temp=77)
        )
        config_dir = write_functional_config(tmp_path / "cfg", port_a)
        await server_a.start()
        await server_b.start()
        rt = assemble(config_dir, sink_factory=functional_sink_factory)
        await start_runtime(rt)
        try:
            value = await rt.query.read_point("modbus-1", "temp.int")
            assert value.value == 25

            def mutate(data: dict[str, Any]) -> None:
                data["devices"][0]["endpoint"]["port"] = port_b

            update_yaml(config_dir, DEVICES, mutate)
            result = await rt.config.reload()
            assert result.success, result.errors
            assert result.diff is not None
            assert result.diff.devices.updated == ["modbus-1"]

            # 重建后读到的是 server B 的值——连接确实切换了对端。
            value = await rt.query.read_point("modbus-1", "temp.int")
            assert value.value == 77
        finally:
            await stop_runtime(rt)
            await server_a.stop()
            await server_b.stop()


class TestSinkChanges:
    async def test_add_sink_registers_new_sink(self, runtime_factory, tmp_path) -> None:
        async with runtime_factory() as ctx:
            def mutate(data: dict[str, Any]) -> None:
                data["sinks"].append(
                    {
                        "name": "file_sink",
                        "type": "file",
                        "params": {"path": str(tmp_path / "added" / "data.jsonl")},
                    }
                )

            update_yaml(ctx.config_dir, SYSTEM, mutate)
            result = await ctx.rt.config.reload()
            assert result.success, result.errors
            assert result.diff is not None
            assert result.diff.sinks.added == ["file_sink"]
            assert "file_sink" in ctx.rt.sinks

    async def test_update_sink_rebuilds_sink_instance(
        self, runtime_factory, tmp_path
    ) -> None:
        async with runtime_factory() as ctx:
            def add_sink(data: dict[str, Any]) -> None:
                data["sinks"].append(
                    {
                        "name": "file_sink",
                        "type": "file",
                        "params": {"path": str(tmp_path / "v1" / "data.jsonl")},
                    }
                )

            update_yaml(ctx.config_dir, SYSTEM, add_sink)
            result = await ctx.rt.config.reload()
            assert result.success, result.errors
            before = ctx.rt.sinks["file_sink"]

            def move_sink(data: dict[str, Any]) -> None:
                data["sinks"][1]["params"]["path"] = str(tmp_path / "v2" / "data.jsonl")

            update_yaml(ctx.config_dir, SYSTEM, move_sink)
            result = await ctx.rt.config.reload()
            assert result.success, result.errors
            assert result.diff is not None
            assert result.diff.sinks.updated == ["file_sink"]
            assert ctx.rt.sinks["file_sink"] is not before


class TestReloadFailureRecovery:
    async def test_invalid_config_keeps_runtime_and_retry_succeeds(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        ctx = modbus_runtime
        baseline = await ctx.rt.tasks.get_instance("modbus-telemetry:modbus-1")

        # 写入引用未知 sink 的 task——schema 校验必须拒绝。
        def break_it(data: dict[str, Any]) -> None:
            data["tasks"][0]["targets"] = [{"sink": "ghost_sink"}]

        update_yaml(ctx.config_dir, TASKS, break_it)
        result = await ctx.rt.config.reload()
        assert result.success is False
        assert result.errors

        # 失败不触碰运行态。
        after = await ctx.rt.tasks.get_instance("modbus-telemetry:modbus-1")
        assert after.targets == baseline.targets
        assert after.interval == baseline.interval

        # 修复后下一次 reload 正常应用。
        def fix_it(data: dict[str, Any]) -> None:
            data["tasks"][0]["targets"] = [{"sink": "null_sink"}]
            data["tasks"][0]["interval"] = 0.7

        update_yaml(ctx.config_dir, TASKS, fix_it)
        result = await ctx.rt.config.reload()
        assert result.success, result.errors
        recovered = await ctx.rt.tasks.get_instance("modbus-telemetry:modbus-1")
        assert recovered.interval == 0.7

    async def test_malformed_yaml_keeps_runtime(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        ctx = modbus_runtime
        (ctx.config_dir / TASKS).write_text("tasks: [unclosed", encoding="utf-8")
        result = await ctx.rt.config.reload()
        assert result.success is False
        assert result.errors
        # 运行态仍是启动基线。
        instances = await ctx.rt.tasks.list_instances()
        assert [i.instance_id for i in instances] == ["modbus-telemetry:modbus-1"]
