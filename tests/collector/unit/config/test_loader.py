"""Unit tests for configuration loader including cross-file validation."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.config_helper import write_config_tree
from wind_hub.config.loader import (
    load_config,
    load_device_models,
    load_devices,
    load_points,
    load_system,
    load_tasks,
)
from wind_hub.domain.model.errors import ConfigError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(dir_path: Path, name: str, data: dict) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    p = dir_path / name
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)
    return p


def _write_config_dir(
    base: Path,
    *,
    devices: list[dict[str, Any]],
    point_tables: dict[str, Any],
    device_models: dict[str, Any] | None = None,
    sinks: list[dict[str, Any]] | None = None,
    tasks: list[dict[str, Any]] | None = None,
) -> Path:
    """写出一套最小自包含配置目录，返回该目录。"""
    return write_config_tree(
        base,
        devices=devices,
        point_tables=point_tables,
        device_models=device_models,
        sinks=sinks,
        tasks=tasks,
    )


def _modbus_device(device_id: str = "d1", point_table: str = "t1", **extra: Any) -> dict[str, Any]:
    return {
        "device_id": device_id,
        "protocol": "modbus",
        "point_table": point_table,
        "endpoint": {"host": "10.0.0.1", "port": 502},
        **extra,
    }


def _ads_device(device_id: str = "d1", point_table: str = "t1", **extra: Any) -> dict[str, Any]:
    return {
        "device_id": device_id,
        "protocol": "ads",
        "point_table": point_table,
        "endpoint": {"host": "10.0.0.1", "port": 48898},
        **extra,
    }


def _table(points: list[dict[str, Any]]) -> dict[str, Any]:
    return {"t1": {"points": points}}


def _modbus_point(point_id: str = "p1", point_groups: list[str] | None = None) -> dict[str, Any]:
    return {
        "point_id": point_id,
        "point_groups": point_groups or ["default"],
        "address": {"type": "holding_register", "address": 30001},
        "data_type": "float32",
    }


def _task(task_id: str = "task1", **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "task_id": task_id,
        "device": "d1",
        "point_group": "default",
        "interval": 1.0,
        "targets": [{"sink": "s1"}],
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_load_template_config_dir(self) -> None:
        """configs/template 是完整自包含配置集，可独立加载。"""
        cfg = load_config("configs/template")
        assert cfg.system.site is not None
        assert "none" in cfg.units.units
        assert cfg.units.units["kilowatt"].symbol == "kW"
        assert len(cfg.devices.devices) == 2
        # 点表 protocol 进入 resolved 结果
        assert cfg.point_tables.tables["modbus_wtg_v1"].protocol == "modbus"
        assert cfg.point_tables.tables["beckhoff_wtg_v1"].protocol == "ads"
        # 继承展开：子表带父表点 + 新增点
        child = cfg.point_tables.tables["beckhoff_wtg_v1"].points
        assert {p.point_id for p in child} == {"rotor_speed", "gen_power", "converter_temp"}

    def test_load_example_modbus_config_dir(self) -> None:
        """configs/example_modbus 可独立加载。"""
        cfg = load_config("configs/example_modbus")
        assert cfg.system.site is not None
        assert cfg.system.site.site_id == "example_modbus"
        assert {d.device_id for d in cfg.devices.devices} == {"wtg-002", "wtg-003"}
        assert cfg.point_tables.tables["wtg_modbus_site_v1"].protocol == "modbus"

    def test_load_example_ads_config_dir(self) -> None:
        """configs/example_ads 可独立加载。"""
        cfg = load_config("configs/example_ads")
        assert cfg.system.site is not None
        assert cfg.system.site.site_id == "example_ads"
        assert len(cfg.devices.devices) == 34
        assert cfg.point_tables.tables["wtg_ads_site_v1"].protocol == "ads"

    def test_load_minimal_valid_config(self) -> None:
        """A minimal valid configuration loads without error."""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device()],
                point_tables=_table([_modbus_point()]),
                tasks=[_task()],
            )
            cfg = load_config(site)
            assert len(cfg.devices.devices) == 1
            assert cfg.points_for_device("d1")[0].point_id == "p1"
            assert cfg.tasks.tasks[0].task_id == "task1"

    def test_ads_sum_accepts_index_only_points(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_ads_device(read_mode="sum")],
                point_tables=_table(
                    [
                        {
                            "point_id": "p1",
                            "point_groups": ["default"],
                            "address": {"index_group": 16448, "index_offset": 100},
                            "data_type": "float32",
                        }
                    ]
                ),
                tasks=[_task()],
            )
            cfg = load_config(site)
            point = cfg.points_for_device("d1")[0]
            assert point.address.model_extra["index_group"] == 16448
            assert point.address.model_extra["index_offset"] == 100
    def test_config_without_tasks_is_valid(self) -> None:
        """tasks.yaml 为空 tasks 列表：合法，只是不做周期采集。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device()],
                point_tables=_table([_modbus_point()]),
            )
            cfg = load_config(site)
            assert cfg.tasks.tasks == []

    def test_reporting_yaml_optional(self) -> None:
        """reporting.yaml 缺失时 reporting 为 None。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device()],
                point_tables=_table([_modbus_point()]),
            )
            cfg = load_config(site)
            assert cfg.reporting is None


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------


class TestMissingFile:
    def test_missing_system_yaml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td, pytest.raises(ConfigError, match="not found"):
            load_config(td)

    def test_missing_device_models_yaml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td), devices=[_modbus_device()], point_tables=_table([_modbus_point()])
            )
            (site / "device_models.yaml").unlink()
            with pytest.raises(ConfigError, match="not found"):
                load_config(site)

    def test_missing_devices_yaml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td), devices=[_modbus_device()], point_tables=_table([_modbus_point()])
            )
            (site / "devices.yaml").unlink()
            with pytest.raises(ConfigError, match="not found"):
                load_config(site)

    def test_missing_points_yaml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td), devices=[_modbus_device()], point_tables=_table([_modbus_point()])
            )
            (site / "points.yaml").unlink()
            with pytest.raises(ConfigError, match="not found"):
                load_config(site)

    def test_missing_units_yaml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td), devices=[_modbus_device()], point_tables=_table([_modbus_point()])
            )
            (site / "units.yaml").unlink()
            with pytest.raises(ConfigError, match="not found"):
                load_config(site)

    def test_missing_tasks_yaml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td), devices=[_modbus_device()], point_tables=_table([_modbus_point()])
            )
            (site / "tasks.yaml").unlink()
            with pytest.raises(ConfigError, match="not found"):
                load_config(site)


# ---------------------------------------------------------------------------
# Invalid YAML
# ---------------------------------------------------------------------------


class TestInvalidYaml:
    def test_malformed_yaml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            base.joinpath("system.yaml").write_text("::: invalid yaml :::")
            with pytest.raises(ConfigError, match="Invalid YAML"):
                load_system(base / "system.yaml")

    def test_malformed_tasks_yaml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            base.joinpath("tasks.yaml").write_text("::: invalid yaml :::")
            with pytest.raises(ConfigError, match="Invalid YAML"):
                load_tasks(base / "tasks.yaml")


# ---------------------------------------------------------------------------
# Cross-file validation — 型号 / 实例引用
# ---------------------------------------------------------------------------


class TestModelValidation:
    def test_unknown_model_raises(self) -> None:
        """实例引用不存在的 model → 加载期报错并指出 device 与 model。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[
                    {
                        "device_id": "d1",
                        "model": "ghost_model",
                        "endpoint": {"host": "10.0.0.1", "port": 502},
                    }
                ],
                device_models={},
                point_tables=_table([_modbus_point()]),
            )
            with pytest.raises(ConfigError, match="d1.*ghost_model"):
                load_config(site)

    def test_model_references_unknown_point_table_raises(self) -> None:
        """型号引用的 point_table 不存在 → 报错并指出型号名。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device(point_table="nonexistent")],
                point_tables={"t1": {"points": []}},
            )
            with pytest.raises(ConfigError, match="unknown point_table"):
                load_config(site)

    def test_model_references_unknown_device_type_raises(self) -> None:
        """型号引用不存在的 device_type → 报错并指出型号与类型。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[
                    {
                        "device_id": "d1",
                        "model": "m1",
                        "endpoint": {"host": "10.0.0.1", "port": 502},
                    }
                ],
                device_models={
                    "m1": {
                        "device_type": "ghost_type",
                        "protocol": "modbus",
                        "point_table": "t1",
                    }
                },
                point_tables=_table([_modbus_point()]),
            )
            with pytest.raises(ConfigError, match="m1.*ghost_type"):
                load_config(site)

    def test_model_unknown_protocol_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[
                    {
                        "device_id": "d1",
                        "model": "m1",
                        "endpoint": {"host": "10.0.0.1", "port": 502},
                    }
                ],
                device_models={
                    "m1": {"device_type": "turbine", "protocol": "opcua", "point_table": "t1"}
                },
                point_tables=_table([_modbus_point()]),
            )
            with pytest.raises(ConfigError, match="opcua"):
                load_config(site)

    def test_read_mode_on_non_ads_model_raises(self) -> None:
        """read_mode 是 ADS 专属——其他协议型号配置即报错。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[
                    {
                        "device_id": "d1",
                        "model": "m1",
                        "endpoint": {"host": "10.0.0.1", "port": 502},
                    }
                ],
                device_models={
                    "m1": {
                        "device_type": "turbine",
                        "protocol": "modbus",
                        "point_table": "t1",
                        "read_mode": "sum",
                    }
                },
                point_tables=_table([_modbus_point()]),
            )
            with pytest.raises(ConfigError, match="read_mode"):
                load_config(site)

    def test_endpoint_overrides_connection_defaults(self) -> None:
        """实例 endpoint 覆盖型号 connection_defaults（实例优先）。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[
                    {
                        "device_id": "d1",
                        "model": "m1",
                        "endpoint": {
                            "host": "10.0.0.9",
                            "port": 1502,
                            "extensions": {"unit_id": 7},
                        },
                    }
                ],
                device_models={
                    "m1": {
                        "device_type": "turbine",
                        "protocol": "modbus",
                        "point_table": "t1",
                        "connection_defaults": {"port": 502, "unit_id": 1, "timeout": 3.0},
                    }
                },
                point_tables=_table([_modbus_point()]),
            )
            cfg = load_config(site)
            ep = cfg.devices.devices[0].endpoint
            assert ep.host == "10.0.0.9"
            assert ep.port == 1502  # 实例覆盖型号默认 502
            assert ep.extensions == {"unit_id": 7, "timeout": 3.0}  # 同名键实例优先

    def test_endpoint_port_from_connection_defaults(self) -> None:
        """实例省略 port 时取型号 connection_defaults.port。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[
                    {"device_id": "d1", "model": "m1", "endpoint": {"host": "10.0.0.1"}}
                ],
                device_models={
                    "m1": {
                        "device_type": "turbine",
                        "protocol": "modbus",
                        "point_table": "t1",
                        "connection_defaults": {"port": 502},
                    }
                },
                point_tables=_table([_modbus_point()]),
            )
            cfg = load_config(site)
            assert cfg.devices.devices[0].endpoint.port == 502

    def test_endpoint_missing_port_everywhere_raises(self) -> None:
        """实例与型号都没有 port → 报错并指出设备。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[
                    {"device_id": "d1", "model": "m1", "endpoint": {"host": "10.0.0.1"}}
                ],
                device_models={
                    "m1": {"device_type": "turbine", "protocol": "modbus", "point_table": "t1"}
                },
                point_tables=_table([_modbus_point()]),
            )
            with pytest.raises(ConfigError, match="d1.*port"):
                load_config(site)

    def test_duplicate_device_id_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device("d1"), _modbus_device("d1")],
                point_tables=_table([_modbus_point()]),
            )
            with pytest.raises(ConfigError, match="Duplicate device_id"):
                load_config(site)


# ---------------------------------------------------------------------------
# Cross-file validation — Task 引用
# ---------------------------------------------------------------------------


class TestCrossFileValidation:
    def test_task_targets_unknown_sink_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device()],
                point_tables=_table([_modbus_point()]),
                tasks=[_task(targets=[{"sink": "ghost_sink"}])],
            )
            with pytest.raises(ConfigError, match="unknown sink"):
                load_config(site)

    def test_task_targets_disabled_sink_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device()],
                point_tables=_table([_modbus_point()]),
                sinks=[{"name": "s1", "type": "file", "enabled": False}],
                tasks=[_task()],
            )
            with pytest.raises(ConfigError, match="disabled sink"):
                load_config(site)
    def test_task_references_unknown_device_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device()],
                point_tables=_table([_modbus_point()]),
                tasks=[_task(device="ghost_device")],
            )
            with pytest.raises(ConfigError, match="unknown device 'ghost_device'"):
                load_config(site)

    def test_task_device_group_matches_no_device_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device(device_group="turbine")],
                point_tables=_table([_modbus_point()]),
                tasks=[_task(device=None, device_group="pcs")],
            )
            with pytest.raises(ConfigError, match="matches no device"):
                load_config(site)

    def test_task_without_interval_on_poll_device_raises(self) -> None:
        """Modbus（主动轮询）Task 省略 interval → 加载期报错。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device()],
                point_tables=_table([_modbus_point()]),
                tasks=[_task(interval=None)],
            )
            with pytest.raises(ConfigError, match="interval is required"):
                load_config(site)

    def test_task_without_interval_on_iec104_only_accepted(self) -> None:
        """纯 IEC104（订阅式）Task 可省略 interval。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[
                    {
                        "device_id": "d1",
                        "protocol": "iec104",
                        "point_table": "t1",
                        "endpoint": {"host": "10.0.0.1", "port": 2404},
                    }
                ],
                point_tables=_table(
                    [
                        {
                            "point_id": "p1",
                            "point_groups": ["default"],
                            "address": {"ioa": 100},
                            "data_type": "float32",
                        }
                    ]
                ),
                tasks=[_task(interval=None)],
            )
            cfg = load_config(site)
            assert cfg.tasks.tasks[0].interval is None

    def test_task_without_interval_on_mixed_group_raises(self) -> None:
        """device_group 同时命中 IEC104 与 Modbus → interval 必填。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[
                    _modbus_device(device_id="d1", device_group="mixed"),
                    {
                        "device_id": "d2",
                        "protocol": "iec104",
                        "point_table": "t2",
                        "device_group": "mixed",
                        "endpoint": {"host": "10.0.0.2", "port": 2404},
                    },
                ],
                point_tables={
                    "t1": {"points": [_modbus_point()]},
                    "t2": {
                        "points": [
                            {
                                "point_id": "p2",
                                "point_groups": ["default"],
                                "address": {"ioa": 100},
                            }
                        ]
                    },
                },
                tasks=[_task(device=None, device_group="mixed", interval=None)],
            )
            with pytest.raises(ConfigError, match="interval is required"):
                load_config(site)

    def test_interval_configured_on_iec104_only_accepted(self) -> None:
        """纯 IEC104 Task 配置 interval 允许存在（仅不用于 IEC104 调度）。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[
                    {
                        "device_id": "d1",
                        "protocol": "iec104",
                        "point_table": "t1",
                        "endpoint": {"host": "10.0.0.1", "port": 2404},
                    }
                ],
                point_tables=_table(
                    [
                        {
                            "point_id": "p1",
                            "point_groups": ["default"],
                            "address": {"ioa": 100},
                            "data_type": "float32",
                        }
                    ]
                ),
                tasks=[_task(interval=5.0)],
            )
            cfg = load_config(site)
            assert cfg.tasks.tasks[0].interval == 5.0

    def test_task_point_group_missing_in_device_table_raises(self) -> None:
        """task.point_group 必须存在于命中设备的点表——报错信息列出缺失设备。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device()],
                point_tables=_table([_modbus_point(point_groups=["default"])]),
                tasks=[_task(point_group="ghost_group")],
            )
            with pytest.raises(ConfigError, match=r"point_group.*devices \['d1'\]"):
                load_config(site)

    def test_task_point_group_must_exist_in_every_matched_device(self) -> None:
        """device_group Task：命中多台设备时，任一台缺失 point_group 即报错并列出该设备。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[
                    _modbus_device(device_id="d1", device_group="turbine"),
                    _modbus_device(device_id="d2", point_table="t2", device_group="turbine"),
                ],
                point_tables={
                    "t1": {"points": [_modbus_point(point_groups=["fast"])]},
                    "t2": {"points": [_modbus_point(point_id="p2", point_groups=["slow"])]},
                },
                tasks=[_task(device=None, device_group="turbine", point_group="fast")],
            )
            with pytest.raises(ConfigError, match=r"devices \['d2'\]"):
                load_config(site)

    def test_task_point_group_checked_on_resolved_table(self) -> None:
        """point_group 存在性校验作用于继承展开后的最终点集。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device(point_table="child")],
                point_tables={
                    "base": {"points": [_modbus_point(point_groups=["fast"])]},
                    "child": {
                        "extends": "base",
                        "points": [{"point_id": "p1", "point_groups": ["slow"]}],
                    },
                },
                tasks=[_task(point_group="fast")],
            )
            # 继承后 p1 的最终分组是 slow——引用 fast 的 Task 不再合法
            with pytest.raises(ConfigError, match="point_group"):
                load_config(site)

    def test_task_referencing_sequential_ads_device_raises(self) -> None:
        """ADS sequential 设备只允许单次读取——任何 Task 引用都是配置错误。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_ads_device(read_mode="sequential")],
                point_tables=_table(
                    [
                        {
                            "point_id": "p1",
                            "point_groups": ["default"],
                            "address": {"symbol": "MAIN.p"},
                            "data_type": "float32",
                        }
                    ]
                ),
                tasks=[_task()],
            )
            with pytest.raises(ConfigError, match="sequential"):
                load_config(site)

    def test_group_task_matching_sequential_ads_device_raises(self) -> None:
        """device_group Task 命中 sequential 设备同样报错。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[
                    _ads_device(device_id="d1", device_group="turbine_ads", read_mode="sequential"),
                    _ads_device(device_id="d2", device_group="turbine_ads", read_mode="sum"),
                ],
                point_tables=_table(
                    [
                        {
                            "point_id": "p1",
                            "point_groups": ["default"],
                            "address": {"symbol": "MAIN.p"},
                            "data_type": "float32",
                        }
                    ]
                ),
                tasks=[_task(device=None, device_group="turbine_ads")],
            )
            with pytest.raises(ConfigError, match=r"\['d1'\].*sequential"):
                load_config(site)

    def test_task_referencing_disabled_device_accepted(self) -> None:
        """device 任务指向 disabled 设备：加载合法（运行时不展开实例）。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device(enabled=False)],
                point_tables=_table([_modbus_point()]),
                tasks=[_task()],
            )
            cfg = load_config(site)
            assert cfg.tasks.tasks[0].device == "d1"

    def test_disabled_task_skips_cross_validation_of_point_group(self) -> None:
        """加载期校验不看 enabled 标志——disabled Task 同样校验 point_group。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device()],
                point_tables=_table([_modbus_point()]),
                tasks=[_task(point_group="ghost", enabled=False)],
            )
            with pytest.raises(ConfigError, match="point_group"):
                load_config(site)

    def test_duplicate_task_id_in_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device()],
                point_tables=_table([_modbus_point()]),
                tasks=[_task(task_id="t1"), _task(task_id="t1", interval=2.0)],
            )
            with pytest.raises(ConfigError, match="Duplicate task_id"):
                load_config(site)


# ---------------------------------------------------------------------------
# ADS 地址形式校验（配置加载阶段）
# ---------------------------------------------------------------------------


class TestADSAddressValidation:
    def _load(self, tmp: str, address: dict[str, Any], **device_extra: Any) -> None:
        site = _write_config_dir(
            Path(tmp),
            devices=[_ads_device(**device_extra)],
            point_tables=_table(
                [
                    {
                        "point_id": "p1",
                        "point_groups": ["default"],
                        "address": address,
                        "data_type": "float32",
                    }
                ]
            ),
        )
        load_config(site)

    def test_symbol_only_legal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._load(td, {"symbol": "MAIN.rotorSpeed"})

    def test_index_pair_legal(self) -> None:
        """仅 index 成对寻址合法——但 sum 模式要求 symbol，故以 sequential 验证。"""
        with tempfile.TemporaryDirectory() as td:
            self._load(
                td,
                {"index_group": 0x4020, "index_offset": 0x1234},
                read_mode="sequential",
            )

    def test_symbol_and_index_legal(self) -> None:
        """symbol 与 index 同时配置是允许的（读写以 symbol 优先）。"""
        with tempfile.TemporaryDirectory() as td:
            self._load(td, {"symbol": "MAIN.p", "index_group": 0x4020, "index_offset": 0x1234})

    def test_index_group_only_illegal(self) -> None:
        with (
            tempfile.TemporaryDirectory() as td,
            pytest.raises(ConfigError, match="together"),
        ):
            self._load(td, {"index_group": 0x4020})

    def test_index_offset_only_illegal(self) -> None:
        with (
            tempfile.TemporaryDirectory() as td,
            pytest.raises(ConfigError, match="together"),
        ):
            self._load(td, {"index_offset": 0x1234})

    def test_empty_address_illegal(self) -> None:
        with (
            tempfile.TemporaryDirectory() as td,
            pytest.raises(ConfigError, match="symbol"),
        ):
            self._load(td, {}, read_mode="sequential")

    def test_address_validation_skipped_for_non_ads(self) -> None:
        """非 ADS 设备不做 ADS 地址校验。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device()],
                point_tables=_table([_modbus_point()]),
            )
            load_config(site)


# ---------------------------------------------------------------------------
# ADS read_mode 与点表约束
# ---------------------------------------------------------------------------


class TestADSReadMode:
    def test_sequential_device_accepted_without_tasks(self) -> None:
        """sequential 不配置 Task：单次读取走 CLI/API，加载合法。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_ads_device(read_mode="sequential")],
                point_tables=_table(
                    [
                        {
                            "point_id": "p1",
                            "point_groups": ["diag"],
                            "address": {"symbol": "MAIN.p"},
                            "data_type": "float32",
                        }
                    ]
                ),
            )
            cfg = load_config(site)
            assert cfg.devices.devices[0].read_mode == "sequential"

    def test_sum_point_without_symbol_rejected(self) -> None:
        """sum 按 Symbol 批量读——绑定表的每个点都必须配置 symbol。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_ads_device(read_mode="sum")],
                point_tables=_table(
                    [
                        {
                            "point_id": "p1",
                            "point_groups": ["default"],
                            "address": {"index_group": 0x4020, "index_offset": 0x1234},
                            "data_type": "float32",
                        }
                    ]
                ),
            )
            with pytest.raises(ConfigError, match="symbol"):
                load_config(site)


# ---------------------------------------------------------------------------
# 点表继承后的绑定校验（作用于 Resolved Point Table）
# ---------------------------------------------------------------------------


class TestInheritanceAwareValidation:
    def test_ads_sum_rejects_inherited_index_only_point(self) -> None:
        """子表把 address 整体覆盖为 index-only 后，sum 设备绑定即报错。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_ads_device(read_mode="sum", point_table="child")],
                point_tables={
                    "base": {
                        "points": [
                            {
                                "point_id": "p1",
                                "point_groups": ["default"],
                                "address": {"symbol": "MAIN.p"},
                                "data_type": "float32",
                            }
                        ]
                    },
                    "child": {
                        "extends": "base",
                        "points": [
                            {
                                "point_id": "p1",
                                "address": {"index_group": 0x4020, "index_offset": 0x1234},
                            }
                        ],
                    },
                },
            )
            with pytest.raises(ConfigError, match="symbol"):
                load_config(site)

    def test_resolved_point_groups_visible_to_tasks(self) -> None:
        """正向对照：Task 引用继承后最终 point_groups 加载成功。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device(point_table="child")],
                point_tables={
                    "base": {
                        "points": [
                            _modbus_point("p1", point_groups=["fast"]),
                            _modbus_point("p2", point_groups=["fast"]),
                        ]
                    },
                    "child": {
                        "extends": "base",
                        "points": [{"point_id": "p2", "point_groups": ["slow"]}],
                    },
                },
                tasks=[
                    _task(task_id="t-fast", point_group="fast"),
                    _task(task_id="t-slow", point_group="slow"),
                ],
            )
            cfg = load_config(site)
            groups = {p.point_id: p.point_groups for p in cfg.points_for_device("d1")}
            assert groups == {"p1": ["fast"], "p2": ["slow"]}


# ---------------------------------------------------------------------------
# Individual loaders
# ---------------------------------------------------------------------------


class TestIndividualLoaders:
    def test_load_system(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = _write_yaml(
                Path(td),
                "system.yaml",
                {
                    "site": {"site_id": "wind_farm_a", "name": "某某风电场"},
                    "sinks": [{"name": "s1", "type": "kafka"}],
                },
            )
            cfg = load_system(p)
            assert cfg.sinks[0].name == "s1"
            assert cfg.site is not None
            assert cfg.site.site_id == "wind_farm_a"
            assert cfg.site.name == "某某风电场"

    def test_load_device_models(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = _write_yaml(
                Path(td),
                "device_models.yaml",
                {
                    "device_types": {"turbine": {"name": "风力发电机组"}},
                    "device_models": {
                        "m1": {
                            "device_type": "turbine",
                            "protocol": "ads",
                            "point_table": "t1",
                            "read_mode": "sum",
                            "properties": {"rated_power_kw": 2000},
                            "connection_defaults": {"port": 48898},
                        }
                    },
                },
            )
            cfg = load_device_models(p)
            assert cfg.device_types["turbine"].name == "风力发电机组"
            m = cfg.device_models["m1"]
            assert m.protocol == "ads"
            assert m.read_mode == "sum"
            assert m.properties == {"rated_power_kw": 2000}
            assert m.connection_defaults == {"port": 48898}

    def test_load_devices(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = _write_yaml(
                Path(td),
                "devices.yaml",
                {
                    "devices": [
                        {
                            "device_id": "d1",
                            "model": "m1",
                            "device_group": "turbine",
                            "endpoint": {"host": "10.0.0.1"},
                        }
                    ],
                },
            )
            cfg = load_devices(p)
            inst = cfg.devices[0]
            assert inst.device_id == "d1"
            assert inst.model == "m1"
            assert inst.device_group == "turbine"
            assert inst.endpoint.host == "10.0.0.1"
            assert inst.endpoint.port is None

    def test_load_points(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = _write_yaml(
                Path(td),
                "points.yaml",
                {
                    "point_tables": {
                        "t1": {
                            "points": [
                                {
                                    "point_id": "p1",
                                    "point_groups": ["default"],
                                    "address": {"type": "hr", "address": 30001},
                                    "data_type": "float32",
                                }
                            ]
                        }
                    }
                },
            )
            cfg = load_points(p)
            assert cfg.tables["t1"].points[0].unit is None  # Raw Patch：未写即「未设置」
            assert cfg.tables["t1"].points[0].point_groups == ["default"]

    def test_load_points_empty_tables(self) -> None:
        """points.yaml 没有 point_tables 键时等价于空点表集。"""
        with tempfile.TemporaryDirectory() as td:
            p = _write_yaml(Path(td), "points.yaml", {"something_else": 1})
            cfg = load_points(p)
            assert cfg.tables == {}

    def test_load_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = _write_yaml(
                Path(td),
                "tasks.yaml",
                {
                    "tasks": [
                        {
                            "task_id": "t1",
                            "device_group": "turbine",
                            "point_group": "fast",
                            "interval": 1.0,
                            "targets": [{"sink": "s1"}],
                        },
                    ],
                },
            )
            cfg = load_tasks(p)
            assert len(cfg.tasks) == 1
            assert cfg.tasks[0].device_group == "turbine"
            assert cfg.tasks[0].enabled is True


# ---------------------------------------------------------------------------
# Cross-file validation — 单位与点表 protocol
# ---------------------------------------------------------------------------


class TestUnitValidation:
    def test_unknown_unit_raises(self) -> None:
        """点引用 units.yaml 中不存在的 unit ID → 加载失败。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device()],
                point_tables=_table([{**_modbus_point(), "unit": "KW"}]),
            )
            with pytest.raises(ConfigError, match="p1.*unknown unit 'KW'"):
                load_config(site)

    def test_valid_unit_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device()],
                point_tables=_table([{**_modbus_point(), "unit": "kilowatt"}]),
            )
            cfg = load_config(site)
            assert cfg.points_for_device("d1")[0].unit == "kilowatt"
            assert cfg.units.units["kilowatt"].symbol == "kW"

    def test_unit_validated_after_inheritance_override(self) -> None:
        """继承 override 后的最终 unit 同样校验。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device(point_table="child")],
                point_tables={
                    "base": {"points": [{**_modbus_point(), "unit": "kilowatt"}]},
                    "child": {
                        "extends": "base",
                        "points": [{"point_id": "p1", "unit": "ghost_unit"}],
                    },
                },
            )
            with pytest.raises(ConfigError, match="unknown unit 'ghost_unit'"):
                load_config(site)


class TestPointTableProtocolValidation:
    def test_model_protocol_mismatch_with_table_raises(self) -> None:
        """DeviceModel.protocol 与绑定点表 protocol 不一致 → 加载失败。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[
                    {
                        "device_id": "d1",
                        "model": "m1",
                        "endpoint": {"host": "10.0.0.1", "port": 502},
                    }
                ],
                device_models={
                    "m1": {
                        "device_type": "turbine",
                        "protocol": "modbus",
                        "point_table": "t1",
                    }
                },
                point_tables={
                    "t1": {
                        "protocol": "ads",
                        "points": [
                            {
                                "point_id": "p1",
                                "point_groups": ["default"],
                                "address": {"symbol": "MAIN.p"},
                            }
                        ],
                    }
                },
            )
            with pytest.raises(ConfigError, match="m1.*does not match"):
                load_config(site)

    def test_cross_protocol_inheritance_raises(self) -> None:
        """点表跨协议继承 → 加载失败。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device(point_table="child")],
                point_tables={
                    "base": {"protocol": "ads", "points": []},
                    "child": {"extends": "base", "protocol": "modbus"},
                },
            )
            with pytest.raises(ConfigError, match="cross-protocol"):
                load_config(site)

    def test_modbus_address_validated_by_table_protocol(self) -> None:
        """Modbus 点缺 address → 加载失败（按点表 protocol 校验）。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[_modbus_device()],
                point_tables=_table(
                    [
                        {
                            "point_id": "p1",
                            "point_groups": ["default"],
                            "address": {"type": "holding_register"},
                        }
                    ]
                ),
            )
            with pytest.raises(ConfigError, match="Modbus point 'p1'.*address"):
                load_config(site)

    def test_iec104_address_validated_by_table_protocol(self) -> None:
        """IEC104 点缺 ioa → 加载失败。"""
        with tempfile.TemporaryDirectory() as td:
            site = _write_config_dir(
                Path(td),
                devices=[
                    {
                        "device_id": "d1",
                        "protocol": "iec104",
                        "point_table": "t1",
                        "endpoint": {"host": "10.0.0.1", "port": 2404},
                    }
                ],
                point_tables=_table(
                    [
                        {
                            "point_id": "p1",
                            "point_groups": ["default"],
                            "address": {"type": "measured_value"},
                        }
                    ]
                ),
            )
            with pytest.raises(ConfigError, match="IEC104 point 'p1'.*ioa"):
                load_config(site)
