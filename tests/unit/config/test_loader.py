"""Unit tests for configuration loader including cross-file validation."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from wind_hub.config.loader import (
    load_config,
    load_devices,
    load_points,
    load_routing,
    load_system,
)
from wind_hub.domain.model.errors import ConfigError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(dir_path: Path, name: str, data: dict) -> Path:
    p = dir_path / name
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)
    return p


def _write_config_dir(
    base: Path,
    *,
    devices: list[dict[str, Any]],
    point_tables: dict[str, Any],
    sinks: list[dict[str, Any]] | None = None,
    rules: list[dict[str, Any]] | None = None,
) -> None:
    """写出一套最小完整配置目录（system/devices/points/routing）。"""
    _write_yaml(base, "system.yaml", {"sinks": sinks or [{"name": "s1", "type": "file"}]})
    _write_yaml(base, "devices.yaml", {"devices": devices})
    _write_yaml(base, "points.yaml", {"point_tables": point_tables})
    _write_yaml(base, "routing.yaml", {"rules": rules or []})


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
        "endpoint": {"host": "10.0.0.1", "port": 851},
        **extra,
    }


def _ads_table(points: list[dict[str, Any]]) -> dict[str, Any]:
    return {"t1": {"points": points}}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_load_example_configs(self) -> None:
        """Loading the project's configs/ directory succeeds."""
        cfg = load_config("configs")
        assert cfg.system.scheduler.default_interval == 1.0
        assert len(cfg.system.sinks) == 3
        assert len(cfg.devices.devices) == 5
        assert len(cfg.point_tables.tables) == 6
        # 继承展开后的点表：base 4 / 2mw 4 / site_a 4 / diag 2 / iec104 4 / modbus 2
        assert sum(len(t.points) for t in cfg.point_tables.tables.values()) == 20
        assert len(cfg.routing.rules) == 3
        # target 级投递策略：同一规则扇出三 sink、各带不同策略
        rules = {r.name: r for r in cfg.routing.rules}
        fanout = {t.sink: t.delivery for t in rules["default-fanout"].targets}
        assert fanout["kafka_main"].type == "always"
        assert fanout["db_main"].type == "interval"
        assert fanout["db_main"].interval == 10.0
        assert fanout["file_archive"].type == "every_n"
        assert fanout["file_archive"].n == 60
        on_change_targets = rules["telemetry-on-change"].targets
        assert len(on_change_targets) == 1
        assert on_change_targets[0].sink == "kafka_main"
        assert on_change_targets[0].delivery.type == "on_change"
        # 新匹配模型：device_group + point_group 的 AND
        fast = rules["turbine-fast"]
        assert fast.match.device_group == "turbine"
        assert fast.match.point_group == "fast"
        assert rules["default-fanout"].match.all is True
        # 点表继承语义：wtg-003 绑定机型表，wtg-004 绑定其现场变体子表
        dev = {d.device_id: d for d in cfg.devices.devices}
        assert dev["wtg-003"].point_table == "beckhoff_2mw_v1"
        assert dev["wtg-004"].point_table == "beckhoff_2mw_site_a"
        by_device = {d: {p.point_id: p for p in pts} for d, pts in cfg.points_by_device().items()}
        # 原样继承：p001 在机型表与现场表中完全一致
        assert by_device["wtg-003"]["p001"] == by_device["wtg-004"]["p001"]
        # remove_points：p003 已不在任何继承展开结果中
        assert "p003" not in by_device["wtg-003"]
        assert "p003" not in by_device["wtg-004"]
        # group override：现场表 p002 从 fast 改为 slow（机型表仍为 fast）
        assert by_device["wtg-003"]["p002"].group == "fast"
        assert by_device["wtg-004"]["p002"].group == "slow"
        # address 整体替换 + 新增点穿透：现场表 p005 使用现场 Symbol
        assert by_device["wtg-004"]["p005"].address.symbol == "PLC1.Measurements.converterTemp"

    def test_load_minimal_valid_config(self) -> None:
        """A minimal valid configuration loads without error."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_config_dir(
                base,
                devices=[_modbus_device()],
                point_tables={
                    "t1": {
                        "points": [
                            {
                                "point_id": "p1",
                                "address": {"type": "holding_register", "register": 30001},
                                "data_type": "float32",
                            }
                        ]
                    }
                },
                rules=[
                    {
                        "name": "default",
                        "match": {"all": True},
                        "targets": [{"sink": "s1"}],
                        "priority": 0,
                    }
                ],
            )
            cfg = load_config(str(base))
            assert len(cfg.devices.devices) == 1
            assert cfg.points_for_device("d1")[0].point_id == "p1"


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------


class TestMissingFile:
    def test_missing_system_yaml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td, pytest.raises(ConfigError, match="not found"):
            load_config(td)

    def test_missing_devices_yaml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_yaml(base, "system.yaml", {"sinks": []})
            with pytest.raises(ConfigError, match="not found"):
                load_config(str(base))


# ---------------------------------------------------------------------------
# Invalid YAML
# ---------------------------------------------------------------------------


class TestInvalidYaml:
    def test_malformed_yaml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            # Write invalid YAML
            base.joinpath("system.yaml").write_text("::: invalid yaml :::")
            with pytest.raises(ConfigError, match="Invalid YAML"):
                load_system(base / "system.yaml")


# ---------------------------------------------------------------------------
# Cross-file validation
# ---------------------------------------------------------------------------


class TestCrossFileValidation:
    def test_device_references_unknown_point_table_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_config_dir(
                base,
                devices=[_modbus_device(point_table="nonexistent")],
                point_tables={"t1": {"points": []}},
            )
            with pytest.raises(ConfigError, match="unknown point_table"):
                load_config(str(base))

    def test_point_group_not_covered_by_polling_raises(self) -> None:
        """点的 group 必须被设备 polling 分组覆盖（无 polling 时仅 default）。"""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_config_dir(
                base,
                devices=[_modbus_device()],
                point_tables=_ads_table(
                    [
                        {
                            "point_id": "p1",
                            "group": "fast",
                            "address": {"type": "hr"},
                            "data_type": "float32",
                        }
                    ]
                ),
            )
            with pytest.raises(ConfigError, match="group"):
                load_config(str(base))

    def test_point_group_covered_by_polling_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_config_dir(
                base,
                devices=[_modbus_device(polling=[{"group": "fast", "interval": 1.0}])],
                point_tables=_ads_table(
                    [
                        {
                            "point_id": "p1",
                            "group": "fast",
                            "address": {"type": "hr"},
                            "data_type": "float32",
                        }
                    ]
                ),
            )
            cfg = load_config(str(base))
            assert cfg.points_for_device("d1")[0].group == "fast"

    def test_point_sink_references_unknown_sink_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_config_dir(
                base,
                devices=[_modbus_device()],
                point_tables=_ads_table(
                    [
                        {
                            "point_id": "p1",
                            "address": {"type": "hr"},
                            "data_type": "float32",
                            "sinks": ["ghost_sink"],
                        }
                    ]
                ),
            )
            with pytest.raises(ConfigError, match="unknown sink"):
                load_config(str(base))

    def test_route_rule_targets_unknown_sink_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_config_dir(
                base,
                devices=[_modbus_device()],
                point_tables=_ads_table(
                    [{"point_id": "p1", "address": {"type": "hr"}, "data_type": "float32"}]
                ),
                rules=[
                    {
                        "name": "r1",
                        "match": {"all": True},
                        "targets": [{"sink": "ghost_sink"}],
                        "priority": 0,
                    }
                ],
            )
            with pytest.raises(ConfigError, match="unknown sink"):
                load_config(str(base))

    def test_polling_group_without_points_raises(self) -> None:
        """每个 polling 分组必须至少有一个点（否则 Job 永远空跑）。"""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_config_dir(
                base,
                devices=[
                    _modbus_device(
                        polling=[
                            {"group": "fast", "interval": 1.0},
                            {"group": "ghost", "interval": 5.0},
                        ]
                    )
                ],
                point_tables=_ads_table(
                    [
                        {
                            "point_id": "p1",
                            "group": "fast",
                            "address": {"type": "hr"},
                            "data_type": "float32",
                        }
                    ]
                ),
            )
            with pytest.raises(ConfigError, match="has no points"):
                load_config(str(base))


# ---------------------------------------------------------------------------
# ADS 地址形式校验（配置加载阶段）
# ---------------------------------------------------------------------------


class TestADSAddressValidation:
    def _load(self, tmp: str, address: dict[str, Any], **device_extra: Any) -> None:
        base = Path(tmp)
        _write_config_dir(
            base,
            devices=[_ads_device(**device_extra)],
            point_tables=_ads_table(
                [{"point_id": "p1", "address": address, "data_type": "float32"}]
            ),
        )
        load_config(str(base))

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
            base = Path(td)
            _write_config_dir(
                base,
                devices=[_modbus_device()],
                point_tables=_ads_table(
                    [{"point_id": "p1", "address": {"type": "hr"}, "data_type": "float32"}]
                ),
            )
            load_config(str(base))


# ---------------------------------------------------------------------------
# ADS read_mode 与调度权限
# ---------------------------------------------------------------------------


class TestADSReadModeScheduling:
    def test_sequential_with_polling_rejected(self) -> None:
        """sequential 只允许请求驱动的单次读取——配置 polling 是配置错误。"""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_config_dir(
                base,
                devices=[
                    _ads_device(
                        read_mode="sequential",
                        polling=[{"group": "default", "interval": 1.0}],
                    )
                ],
                point_tables=_ads_table(
                    [{"point_id": "p1", "address": {"symbol": "MAIN.p"}, "data_type": "float32"}]
                ),
            )
            with pytest.raises(ConfigError, match="sequential"):
                load_config(str(base))

    def test_sequential_without_polling_accepted(self) -> None:
        """sequential 不配置 polling：不创建轮询 Job，单次读取走 CLI/API。"""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_config_dir(
                base,
                devices=[_ads_device(read_mode="sequential")],
                point_tables=_ads_table(
                    [{"point_id": "p1", "address": {"symbol": "MAIN.p"}, "data_type": "float32"}]
                ),
            )
            cfg = load_config(str(base))
            assert cfg.devices.devices[0].read_mode == "sequential"

    def test_sequential_skips_group_coverage(self) -> None:
        """sequential 不参与周期调度——点组无调度含义，不做覆盖校验。"""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_config_dir(
                base,
                devices=[_ads_device(read_mode="sequential")],
                point_tables=_ads_table(
                    [
                        {
                            "point_id": "p1",
                            "group": "diag",
                            "address": {"symbol": "MAIN.p"},
                            "data_type": "float32",
                        }
                    ]
                ),
            )
            load_config(str(base))

    def test_sum_with_polling_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_config_dir(
                base,
                devices=[
                    _ads_device(
                        read_mode="sum", polling=[{"group": "fast", "interval": 1.0}]
                    )
                ],
                point_tables=_ads_table(
                    [
                        {
                            "point_id": "p1",
                            "group": "fast",
                            "address": {"symbol": "MAIN.p"},
                            "data_type": "float32",
                        }
                    ]
                ),
            )
            load_config(str(base))

    def test_sum_point_without_symbol_rejected(self) -> None:
        """sum 按 Symbol 批量读——绑定表的每个点都必须配置 symbol。"""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_config_dir(
                base,
                devices=[_ads_device(read_mode="sum")],
                point_tables=_ads_table(
                    [
                        {
                            "point_id": "p1",
                            "address": {"index_group": 0x4020, "index_offset": 0x1234},
                            "data_type": "float32",
                        }
                    ]
                ),
            )
            with pytest.raises(ConfigError, match="symbol"):
                load_config(str(base))


# ---------------------------------------------------------------------------
# 点表继承后的绑定校验（作用于 Resolved Point Table）
# ---------------------------------------------------------------------------


class TestInheritanceAwareValidation:
    def test_ads_sum_rejects_inherited_index_only_point(self) -> None:
        """子表把 address 整体覆盖为 index-only 后，sum 设备绑定即报错。"""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_config_dir(
                base,
                devices=[_ads_device(read_mode="sum", point_table="child")],
                point_tables={
                    "base": {
                        "points": [
                            {
                                "point_id": "p1",
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
                load_config(str(base))

    def test_polling_coverage_uses_resolved_group(self) -> None:
        """继承后 group 改变 → polling 覆盖校验按最终 group 执行。"""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_config_dir(
                base,
                devices=[
                    _modbus_device(
                        point_table="child", polling=[{"group": "fast", "interval": 1.0}]
                    )
                ],
                point_tables={
                    "base": {
                        "points": [
                            {
                                "point_id": "p1",
                                "group": "fast",
                                "address": {"type": "hr"},
                                "data_type": "float32",
                            }
                        ]
                    },
                    "child": {
                        "extends": "base",
                        "points": [{"point_id": "p1", "group": "slow"}],
                    },
                },
            )
            # 最终 group 是 slow——只覆盖 fast 的 polling 不再合法
            with pytest.raises(ConfigError, match="group"):
                load_config(str(base))

    def test_polling_coverage_ok_on_resolved_group(self) -> None:
        """正向对照：polling 覆盖最终 group（fast + slow）时加载成功。"""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_config_dir(
                base,
                devices=[
                    _modbus_device(
                        point_table="child",
                        polling=[
                            {"group": "fast", "interval": 1.0},
                            {"group": "slow", "interval": 5.0},
                        ],
                    )
                ],
                point_tables={
                    "base": {
                        "points": [
                            {
                                "point_id": "p1",
                                "group": "fast",
                                "address": {"type": "hr"},
                                "data_type": "float32",
                            },
                            {
                                "point_id": "p2",
                                "group": "fast",
                                "address": {"type": "hr"},
                                "data_type": "float32",
                            },
                        ]
                    },
                    "child": {
                        "extends": "base",
                        "points": [{"point_id": "p2", "group": "slow"}],
                    },
                },
            )
            cfg = load_config(str(base))
            groups = {p.point_id: p.group for p in cfg.points_for_device("d1")}
            assert groups == {"p1": "fast", "p2": "slow"}


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
                    "sinks": [{"name": "s1", "type": "kafka"}],
                },
            )
            cfg = load_system(p)
            assert cfg.sinks[0].name == "s1"

    def test_load_devices(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = _write_yaml(
                Path(td),
                "devices.yaml",
                {
                    "devices": [
                        {
                            "device_id": "d1",
                            "protocol": "ads",
                            "point_table": "t1",
                            "endpoint": {"host": "10.0.0.1", "port": 851},
                        }
                    ],
                },
            )
            cfg = load_devices(p)
            assert cfg.devices[0].protocol == "ads"
            assert cfg.devices[0].point_table == "t1"

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
                                    "address": {"type": "hr", "register": 30001},
                                    "data_type": "float32",
                                }
                            ]
                        }
                    }
                },
            )
            cfg = load_points(p)
            assert cfg.tables["t1"].points[0].unit is None

    def test_load_points_empty_tables(self) -> None:
        """points.yaml 没有 point_tables 键时等价于空点表集。"""
        with tempfile.TemporaryDirectory() as td:
            p = _write_yaml(Path(td), "points.yaml", {"something_else": 1})
            cfg = load_points(p)
            assert cfg.tables == {}

    def test_load_routing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = _write_yaml(
                Path(td),
                "routing.yaml",
                {
                    "rules": [
                        {
                            "name": "default",
                            "match": {"all": True},
                            "targets": [{"sink": "s1"}],
                            "priority": 0,
                        },
                    ],
                },
            )
            cfg = load_routing(p)
            assert len(cfg.rules) == 1
