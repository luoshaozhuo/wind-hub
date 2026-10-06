"""ProtocolRegistry 与显式内置注册 builder 的单元测试。

架构约束：注册表不存在进程级共享实例——导入协议模块本身不得产生注册
side effect；内置 Driver 的唯一注册路径是组合根显式调用的
``build_protocol_registry()``。
"""

from __future__ import annotations

import pytest

import wind_hub_core.protocol  # noqa: F401 — 仅验证导入无注册 side effect
import wind_hub_core.protocol.registry as registry_module
from wind_hub_core.config import DeviceConfig
from wind_hub_core.model.device import Endpoint
from wind_hub_core.model.errors import ConfigError
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.protocol.registry import ProtocolRegistry, build_protocol_registry


def _make_device_cfg(
    device_id: str = "test-dev",
    protocol: str = "ads",
    host: str = "127.0.0.1",
    port: int = 851,
) -> DeviceConfig:
    return DeviceConfig(
        device_id=device_id,
        protocol=protocol,
        endpoint=Endpoint(host=host, port=port),
        point_table="t1",
    )


class TestFreshRegistry:
    """空注册表的行为契约；测试自行注册 fake factory，不依赖任何共享状态。"""

    def test_register_driver_success(self) -> None:
        reg = ProtocolRegistry()
        reg.register("test", lambda cfg: "fake-driver")  # type: ignore[arg-type,return-value]
        assert reg.create("test", _make_device_cfg()) == "fake-driver"

    def test_register_duplicate_raises(self) -> None:
        reg = ProtocolRegistry()
        reg.register("test", lambda cfg: "fake-driver")  # type: ignore[arg-type,return-value]
        with pytest.raises(ConfigError, match="already registered"):
            reg.register("test", lambda cfg: "other")  # type: ignore[arg-type,return-value]

    def test_create_registered_returns_instance(self) -> None:
        reg = ProtocolRegistry()
        reg.register("test", lambda cfg: f"driver-{cfg.device_id}")
        cfg = _make_device_cfg()
        driver = reg.create("test", cfg)
        assert driver == "driver-test-dev"

    def test_create_for_uses_config_protocol(self) -> None:
        reg = ProtocolRegistry()
        reg.register("ads", lambda cfg: f"driver-{cfg.device_id}")
        cfg = _make_device_cfg(protocol="ads")
        assert reg.create_for(cfg) == "driver-test-dev"

    def test_create_unknown_lists_registered_names(self) -> None:
        reg = ProtocolRegistry()
        reg.register("known", lambda cfg: "fake-driver")  # type: ignore[arg-type,return-value]
        cfg = _make_device_cfg()
        with pytest.raises(ConfigError, match=r"Unknown protocol driver 'unknown'.*known"):
            reg.create("unknown", cfg)

    def test_registered_names_snapshot(self) -> None:
        reg = ProtocolRegistry()
        reg.register("b", lambda cfg: "x")  # type: ignore[arg-type,return-value]
        reg.register("a", lambda cfg: "x")  # type: ignore[arg-type,return-value]
        assert reg.registered_names() == ("a", "b")


class TestExplicitConstruction:
    """内置注册完全显式：import 不产生 side effect，builder 是唯一入口。"""

    def test_no_process_global_registry(self) -> None:
        """registry 模块不得再暴露共享全局注册表或自注册 decorator。"""
        assert not hasattr(registry_module, "protocol_registry")
        assert not hasattr(registry_module, "register_protocol")

    def test_import_does_not_populate_fresh_registry(self) -> None:
        """导入协议包后新建注册表仍为空——注册只发生在 builder 调用时。"""
        assert ProtocolRegistry().registered_names() == ()

    def test_build_registers_builtin_drivers(self) -> None:
        reg = build_protocol_registry()
        assert reg.registered_names() == ("ads", "iec104", "modbus")

    def test_build_returns_independent_instances(self) -> None:
        first = build_protocol_registry()
        second = build_protocol_registry()
        first.register("extra", lambda cfg: "x")  # type: ignore[arg-type,return-value]
        assert second.registered_names() == ("ads", "iec104", "modbus")


class TestBuiltinDrivers:
    """内置 Driver 经显式 builder 创建——构造即未连接，不触发网络 I/O。"""

    def test_create_all_three_returns_instances(self) -> None:
        reg = build_protocol_registry()
        for proto_name in ("ads", "modbus", "iec104"):
            cfg = _make_device_cfg(protocol=proto_name)
            driver = reg.create_for(cfg)
            assert driver is not None

    def test_all_drivers_health_not_connected(self) -> None:
        reg = build_protocol_registry()
        for proto_name in ("ads", "modbus", "iec104"):
            cfg = _make_device_cfg(protocol=proto_name)
            driver = reg.create_for(cfg)
            status = driver.health()
            assert isinstance(status, HealthStatus)
            assert status.healthy is False  # not connected by default
            assert status.message == "not connected"
