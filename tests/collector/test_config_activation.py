"""新 Collector 配置激活一致性回归测试。

覆盖「prepare 报告成功 ⇔ 真实运行态可与该配置一致」的激活语义：

- RuntimeParams 的 restart-required 字段（read_timeout /
  thread_pool_max_workers）变化在 prepare 阶段拒绝，不出现「激活成功但
  仍使用旧值」的假激活；
- 可热更新字段（connect_timeout / watchdog_interval / health_timeout）
  activate 后真实应用到运行时 owner；
- 进程级 ADS 本机身份（local_ams_net_id / local_ip）变化在 prepare 拒绝。

Sink 阶段随 Sink Service 独立服务化已移出 Collector，对应阶段隔离测试
归属 sink_service 侧。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from collector.application.config import CollectorConfig
from collector.application.config_service import CollectorConfigService
from core.application.settings import ADSLocalConfig
from tests.support.new_collector import make_collector_config, make_runtime

pytestmark = pytest.mark.component

_IDENTITY_A = ADSLocalConfig(local_ams_net_id="1.2.3.4.1.1", local_ip="127.0.0.1")
_IDENTITY_B_NET_ID = ADSLocalConfig(local_ams_net_id="9.9.9.9.1.1", local_ip="127.0.0.1")
_IDENTITY_B_IP = ADSLocalConfig(local_ams_net_id="1.2.3.4.1.1", local_ip="192.168.0.10")


def _service(config: CollectorConfig, candidate: CollectorConfig):
    runtime, _ = make_runtime(config)
    state: dict[str, Any] = {"config": candidate, "hash": "h2"}
    service = CollectorConfigService(
        runtime,
        config,
        load_config=lambda: state["config"],
        config_digest=lambda: state["hash"],
        config_hash="h1",
    )
    return service, runtime


def _assert_nothing_committed(service: CollectorConfigService, baseline: CollectorConfig) -> None:
    """prepare 拒绝后：无 prepared revision，active 基线/hash/revision 全部不变。"""
    assert service.prepared_revision is None
    assert service.current_config is baseline
    assert service.config_hash == "h1"
    assert service.active_revision == "startup"


# ---------------------------------------------------------------------------
# RuntimeParams：restart-required 字段不得假激活
# ---------------------------------------------------------------------------


async def test_thread_pool_size_change_rejected_at_prepare():
    config = make_collector_config()
    candidate = make_collector_config(
        params=replace(
            config.runtime, thread_pool_max_workers=config.runtime.thread_pool_max_workers + 1
        )
    )
    service, runtime = _service(config, candidate)

    result = await service.prepare_config("r1")
    assert not result.success
    assert "runtime.thread_pool_max_workers change requires process restart" in result.errors

    _assert_nothing_committed(service, config)
    # Runtime 仍使用启动参数——不存在「激活成功但运行态是旧值」的窗口。
    assert runtime._params.thread_pool_max_workers == config.runtime.thread_pool_max_workers
    assert runtime._params is config.runtime
    await runtime.stop()


async def test_read_timeout_change_rejected_at_prepare():
    config = make_collector_config()
    candidate = make_collector_config(params=replace(config.runtime, read_timeout=3.0))
    service, runtime = _service(config, candidate)

    result = await service.prepare_config("r1")
    assert not result.success
    assert "runtime.read_timeout change requires process restart" in result.errors

    _assert_nothing_committed(service, config)
    # Runtime 仍使用启动参数（默认 1.0s），不进入假激活窗口。
    assert runtime._params.read_timeout == config.runtime.read_timeout
    assert runtime._params is config.runtime
    await runtime.stop()


async def test_watchdog_timing_change_is_hot_reloadable():
    config = make_collector_config()
    candidate = make_collector_config(
        params=replace(
            config.runtime,
            watchdog_interval=2.5,
            health_timeout=0.5,
        )
    )
    service, runtime = _service(config, candidate)

    prepared = await service.prepare_config("r1")
    assert prepared.success
    activated = await service.activate_config("r1")
    assert activated.success
    assert runtime._params.watchdog_interval == 2.5
    assert runtime._params.health_timeout == 0.5
    await runtime.stop()


async def test_force_reconfigure_does_not_bypass_restart_required():
    config = make_collector_config()
    candidate = make_collector_config(
        params=replace(
            config.runtime, thread_pool_max_workers=config.runtime.thread_pool_max_workers + 1
        )
    )
    service, runtime = _service(config, candidate)

    result = await service.prepare_config("r1", force_reconfigure=True)
    assert not result.success
    assert "runtime.thread_pool_max_workers change requires process restart" in result.errors
    _assert_nothing_committed(service, config)
    await runtime.stop()


# ---------------------------------------------------------------------------
# RuntimeParams：可热更新字段 activate 后真实生效
# ---------------------------------------------------------------------------


async def test_hot_runtime_fields_applied_to_runtime_owners():
    config = make_collector_config()
    candidate = make_collector_config(
        params=replace(
            config.runtime,
            connect_timeout=0.7,
            watchdog_interval=2.5,
            health_timeout=0.5,
        )
    )
    service, runtime = _service(config, candidate)

    prepared = await service.prepare_config("r1")
    assert prepared.success
    # 仅 runtime 变化也构成真实变更——不再出现 has_any_changes=False 的漏判。
    assert prepared.diff.runtime_changed
    assert prepared.diff.has_any_changes

    activated = await service.activate_config("r1")
    assert activated.success
    assert service.current_config is candidate
    assert service.config_hash == "h2"
    assert service.active_revision == "r1"

    # 真实 Runtime owner 已切换到新值（DeviceRuntime 在使用点动态读 params）。
    assert runtime._params is candidate.runtime
    assert runtime.device_runtime._params.connect_timeout == 0.7
    assert runtime.device_runtime._params.watchdog_interval == 2.5
    assert runtime.device_runtime._params.health_timeout == 0.5
    await runtime.stop()


# ---------------------------------------------------------------------------
# ADS 本机身份：restart-required
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current_identity", "candidate_identity"),
    [
        (None, _IDENTITY_A),
        (_IDENTITY_A, None),
        (_IDENTITY_A, _IDENTITY_B_NET_ID),
        (_IDENTITY_A, _IDENTITY_B_IP),
    ],
    ids=["none-to-configured", "configured-to-none", "net-id-change", "ip-change"],
)
async def test_ads_local_identity_change_rejected_at_prepare(
    current_identity: ADSLocalConfig | None,
    candidate_identity: ADSLocalConfig | None,
):
    config = make_collector_config()
    config = replace(config, ads_local=current_identity)
    candidate = replace(config, ads_local=candidate_identity)
    service, runtime = _service(config, candidate)

    result = await service.prepare_config("r1")
    assert not result.success
    assert "ADS local identity change requires process restart" in result.errors

    _assert_nothing_committed(service, config)
    await runtime.stop()


async def test_ads_local_identity_unchanged_other_changes_still_reload():
    config = replace(make_collector_config(), ads_local=_IDENTITY_A)
    candidate = replace(
        make_collector_config(tasks={}),
        ads_local=_IDENTITY_A,
    )
    service, runtime = _service(config, candidate)

    result = await service.reload()
    assert result.success
    assert service.current_config is candidate
    assert service.active_revision == "local-1"
    await runtime.stop()


# ---------------------------------------------------------------------------
# 点表内容变化：协议 Driver 寻址必须随轻量重注入同步更新
# ---------------------------------------------------------------------------


async def test_point_table_address_change_updates_driver_mapping():
    """仅改点地址（表名/绑定不变）激活后，Driver 收到新点表并重建寻址。

    回归：旧实现经 protocol.set_points_mapping 无中断更新寻址；新实现曾
    只更新会话级点表，Driver 构造期快照不变，导致热重载后仍按旧地址采集。
    """
    from core.domain import PointTableId

    config = make_collector_config()
    old_table = config.point_tables[PointTableId("tab")]
    moved_point = replace(old_table.points["p1"], ext={"register_type": "holding", "address": 200})
    new_table = replace(old_table, points={"p1": moved_point})
    candidate = replace(config, point_tables={PointTableId("tab"): new_table})

    service, runtime = _service(config, candidate)
    protocol = next(iter(runtime.devices.values()))._protocol

    result = await service.prepare_config("r1")
    assert result.success, result.errors
    assert result.diff.point_tables_changed == ["tab"]
    result = await service.activate_config("r1")
    assert result.success, result.errors

    assert protocol.point_table_updates, "Driver 未收到点表热更新"
    assert protocol.point_table_updates[-1] is new_table
    session = runtime.devices["dev1"]
    assert session.point_table is new_table
    await runtime.stop()
