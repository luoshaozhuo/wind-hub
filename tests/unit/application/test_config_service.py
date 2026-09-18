"""ConfigService 的单元测试。

验证对象：``application/config_service.py`` 的热重载编排——
「load → validate → diff → Runtime.reconfigure → commit current config」。

覆盖点：

- 初始 load 与 ``current_config``；
- diff 计算（设备/sink 增删改、点表/规则/管线变更）；
- 非法配置：中止重载、不触碰 Runtime、旧快照保持；
- 无变更：不调用 reconfigure 直接成功；
- 有变更：以 ``(new_config, diff)`` 调用 ``Runtime.reconfigure`` 一次；
- reconfigure 返回错误：``success=False``、错误透传、**快照仍提交**
  （部分失败语义：已应用的变更不回滚，下次 reload 以新快照为基准）。

Runtime 用 mock——本层只验证编排，重构执行由
``tests/unit/runtime/test_runtime.py`` 覆盖。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from wind_hub.application.config_service import ConfigService, compute_diff
from wind_hub.application.runtime import Runtime
from wind_hub.config.schema import (
    DeviceConfig,
    PointAddress,
    PointConfig,
    SinkConfig,
)
from wind_hub.domain.model.device import Endpoint
from wind_hub.domain.model.route import RouteRule

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_configs(
    base: Path,
    devices: list[DeviceConfig] | None = None,
    sinks: list[SinkConfig] | None = None,
    points: list[PointConfig] | None = None,
    rules: list[RouteRule] | None = None,
    processors: list[str] | None = None,
) -> None:
    yaml.safe_dump(
        {
            "scheduler": {"queue_maxsize": 10},
            "sinks": [s.model_dump() for s in (sinks or [])],
            "pipeline": {"processors": processors or []},
        },
        (base / "system.yaml").open("w"),
    )
    yaml.safe_dump(
        {"devices": [d.model_dump() for d in (devices or [])]},
        (base / "devices.yaml").open("w"),
    )
    yaml.safe_dump(
        {"points": [p.model_dump() for p in (points or [])]},
        (base / "points.yaml").open("w"),
    )
    yaml.safe_dump(
        {"rules": [r.model_dump() for r in (rules or [])]},
        (base / "routing.yaml").open("w"),
    )


def _make_device(device_id: str, protocol: str = "modbus") -> DeviceConfig:
    return DeviceConfig(
        device_id=device_id,
        protocol=protocol,
        endpoint=Endpoint(host="10.0.0.1", port=502),
    )


def _make_point(device_id: str, point_id: str = "p1") -> PointConfig:
    return PointConfig(
        point_id=point_id,
        device_id=device_id,
        address=PointAddress(type="hr"),
    )


def _mock_runtime(reconfigure_errors: list[str] | None = None) -> MagicMock:
    runtime = MagicMock(spec=Runtime)
    runtime.reconfigure = AsyncMock(return_value=reconfigure_errors or [])
    return runtime


# ---------------------------------------------------------------------------
# 初始加载
# ---------------------------------------------------------------------------


async def test_initial_load_exposes_current_config(tmp_path: Path) -> None:
    _write_configs(tmp_path, devices=[_make_device("d1")], points=[_make_point("d1")])
    service = ConfigService(tmp_path, _mock_runtime())

    cfg = service.current_config
    assert [d.device_id for d in cfg.devices.devices] == ["d1"]


async def test_initial_load_invalid_config_raises(tmp_path: Path) -> None:
    (tmp_path / "system.yaml").write_text("not: [valid")
    with pytest.raises(Exception):  # noqa: B017 — 加载失败类型由 loader 决定
        ConfigService(tmp_path, _mock_runtime())


# ---------------------------------------------------------------------------
# reload —— load / validate 阶段
# ---------------------------------------------------------------------------


async def test_reload_invalid_config_aborts_without_touching_runtime(tmp_path: Path) -> None:
    _write_configs(tmp_path, devices=[_make_device("d1")])
    runtime = _mock_runtime()
    service = ConfigService(tmp_path, runtime)
    snapshot_before = service.current_config

    # 破坏 devices.yaml
    (tmp_path / "devices.yaml").write_text("devices: [broken")
    result = await service.reload()

    assert result.success is False
    assert result.errors  # 加载错误如实呈现
    runtime.reconfigure.assert_not_awaited()
    # 旧快照保持——失败的重载不改变 diff 基准
    assert service.current_config is snapshot_before


# ---------------------------------------------------------------------------
# reload —— diff / no-change
# ---------------------------------------------------------------------------


async def test_reload_no_changes_skips_reconfigure(tmp_path: Path) -> None:
    _write_configs(tmp_path, devices=[_make_device("d1")], points=[_make_point("d1")])
    runtime = _mock_runtime()
    service = ConfigService(tmp_path, runtime)

    result = await service.reload()

    assert result.success is True
    assert result.errors == []
    assert result.diff.has_any_changes is False
    runtime.reconfigure.assert_not_awaited()


# ---------------------------------------------------------------------------
# reload —— reconfigure 调用语义
# ---------------------------------------------------------------------------


async def test_reload_calls_runtime_reconfigure_with_new_config_and_diff(
    tmp_path: Path,
) -> None:
    _write_configs(tmp_path, devices=[_make_device("d1")], points=[_make_point("d1")])
    runtime = _mock_runtime()
    service = ConfigService(tmp_path, runtime)

    # 新增一台设备
    _write_configs(
        tmp_path,
        devices=[_make_device("d1"), _make_device("d2")],
        points=[_make_point("d1")],
    )
    result = await service.reload()

    assert result.success is True
    runtime.reconfigure.assert_awaited_once()
    new_cfg, diff = runtime.reconfigure.await_args.args
    assert diff.devices.added == ["d2"]
    assert [d.device_id for d in new_cfg.devices.devices] == ["d1", "d2"]
    # 成功后提交新快照
    assert service.current_config is new_cfg


async def test_reload_commits_snapshot_even_on_partial_failure(tmp_path: Path) -> None:
    """reconfigure 部分失败：success=False、错误透传，但快照仍提交。"""
    _write_configs(tmp_path, devices=[_make_device("d1")])
    runtime = _mock_runtime(reconfigure_errors=["sink: open failed"])
    service = ConfigService(tmp_path, runtime)

    _write_configs(tmp_path, devices=[_make_device("d1"), _make_device("d2")])
    result = await service.reload()

    assert result.success is False
    assert result.errors == ["sink: open failed"]
    # 快照已提交：再次 reload 同一目录内容时 diff 基准是新快照 → 无变更
    result2 = await service.reload()
    assert result2.success is True
    assert result2.diff.has_any_changes is False
    assert runtime.reconfigure.await_count == 1  # 第二轮不再调用


async def test_reload_propagates_diff_details(tmp_path: Path) -> None:
    _write_configs(
        tmp_path,
        devices=[_make_device("d1")],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    runtime = _mock_runtime()
    service = ConfigService(tmp_path, runtime)

    _write_configs(
        tmp_path,
        devices=[_make_device("d2")],  # d1 删除、d2 新增
        sinks=[SinkConfig(name="s1", type="kafka")],  # s1 变更
    )
    result = await service.reload()

    assert result.success is True
    assert result.diff.devices.added == ["d2"]
    assert result.diff.devices.removed == ["d1"]
    assert result.diff.sinks.updated == ["s1"]


# ---------------------------------------------------------------------------
# compute_diff 纯函数
# ---------------------------------------------------------------------------


async def test_compute_diff_detects_points_rules_pipeline_changes(tmp_path: Path) -> None:
    _write_configs(
        tmp_path,
        devices=[_make_device("d1")],
        sinks=[SinkConfig(name="s1", type="file"), SinkConfig(name="s2", type="file")],
        points=[_make_point("d1", "p1")],
        rules=[RouteRule(name="r1", match_point_prefix="a.", targets=["s1"])],
        processors=["scale"],
    )
    service = ConfigService(tmp_path, _mock_runtime())
    old = service.current_config

    _write_configs(
        tmp_path,
        devices=[_make_device("d1")],
        sinks=[SinkConfig(name="s1", type="file"), SinkConfig(name="s2", type="file")],
        points=[_make_point("d1", "p2")],
        rules=[RouteRule(name="r2", match_point_prefix="b.", targets=["s2"])],
        processors=["scale", "filter"],
    )
    service2 = ConfigService(tmp_path, _mock_runtime())
    new = service2.current_config

    diff = compute_diff(old, new)
    assert diff.points_changed is True
    assert diff.rules_changed is True
    assert diff.pipeline_changed is True
    assert diff.has_any_changes is True


async def test_compute_diff_identical_configs_report_no_changes(tmp_path: Path) -> None:
    _write_configs(tmp_path, devices=[_make_device("d1")], points=[_make_point("d1")])
    service = ConfigService(tmp_path, _mock_runtime())
    service2 = ConfigService(tmp_path, _mock_runtime())

    diff = compute_diff(service.current_config, service2.current_config)

    assert diff.has_any_changes is False
    assert diff.devices.unchanged == ["d1"]
