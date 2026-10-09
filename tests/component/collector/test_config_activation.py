"""新 Collector 配置激活一致性回归测试。

覆盖「prepare 报告成功 ⇔ 真实运行态可与该配置一致」的激活语义：

- RuntimeParams 的 restart-required 字段（queue_maxsize / read_timeout）
  变化在 prepare 阶段拒绝，不出现「激活成功但仍使用旧值」的假激活；
- 可热更新字段（backpressure_policy / shutdown_timeout / connect_timeout）
  activate 后真实应用到运行时 owner；
- 进程级 ADS 本机身份（local_ams_net_id / local_ip）变化在 prepare 拒绝。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from collector.application.config import ADSLocalIdentity, CollectorConfig, RuntimeParams
from collector.application.config_service import CollectorConfigService
from tests.support.new_collector import FakeSink, make_collector_config, make_runtime

_IDENTITY_A = ADSLocalIdentity(local_ams_net_id="1.2.3.4.1.1", local_ip="127.0.0.1")
_IDENTITY_B_NET_ID = ADSLocalIdentity(local_ams_net_id="9.9.9.9.1.1", local_ip="127.0.0.1")
_IDENTITY_B_IP = ADSLocalIdentity(local_ams_net_id="1.2.3.4.1.1", local_ip="192.168.0.10")


def _service(config: CollectorConfig, candidate: CollectorConfig):
    runtime, _ = make_runtime(config)
    state = {"config": candidate, "hash": "h2"}
    service = CollectorConfigService(
        runtime,
        config,
        load_config=lambda: state["config"],
        fingerprint=lambda: state["hash"],
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


async def test_queue_maxsize_change_rejected_at_prepare():
    config = make_collector_config()
    candidate = make_collector_config(
        params=replace(config.runtime, queue_maxsize=config.runtime.queue_maxsize + 1)
    )
    service, runtime = _service(config, candidate)

    result = await service.prepare_config("r1")
    assert not result.success
    assert "runtime.queue_maxsize change requires process restart" in result.errors

    _assert_nothing_committed(service, config)
    # Runtime 仍使用启动参数——不存在「激活成功但运行态是旧值」的窗口。
    assert runtime._params.queue_maxsize == config.runtime.queue_maxsize
    assert runtime.sink_runtime._params is config.runtime
    await runtime.stop()


async def test_read_timeout_change_rejected_at_prepare():
    config = make_collector_config()
    candidate = make_collector_config(params=replace(config.runtime, read_timeout=3.0))
    service, runtime = _service(config, candidate)

    result = await service.prepare_config("r1")
    assert not result.success
    assert "runtime.read_timeout change requires process restart" in result.errors

    _assert_nothing_committed(service, config)
    assert runtime._params.read_timeout is None
    await runtime.stop()


async def test_force_reconfigure_does_not_bypass_restart_required():
    config = make_collector_config()
    candidate = make_collector_config(
        params=replace(config.runtime, queue_maxsize=config.runtime.queue_maxsize + 1)
    )
    service, runtime = _service(config, candidate)

    result = await service.prepare_config("r1", force_reconfigure=True)
    assert not result.success
    assert "runtime.queue_maxsize change requires process restart" in result.errors
    _assert_nothing_committed(service, config)
    await runtime.stop()


# ---------------------------------------------------------------------------
# RuntimeParams：可热更新字段 activate 后真实生效
# ---------------------------------------------------------------------------


async def test_hot_runtime_fields_applied_to_runtime_owners():
    config = make_collector_config()
    candidate = make_collector_config(
        params=RuntimeParams(
            queue_maxsize=config.runtime.queue_maxsize,
            backpressure_policy="block",
            shutdown_timeout=2.5,
            connect_timeout=0.7,
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

    # 真实 Runtime owner 已切换到新值。
    assert runtime._params is candidate.runtime
    assert runtime.device_runtime._params.connect_timeout == 0.7
    assert runtime.sink_runtime._params.backpressure_policy == "block"
    assert runtime.sink_runtime._params.shutdown_timeout == 2.5
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
    current_identity: ADSLocalIdentity | None,
    candidate_identity: ADSLocalIdentity | None,
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
    moved_point = replace(
        old_table.points["p1"], ext={"register_type": "holding", "address": 200}
    )
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


# ---------------------------------------------------------------------------
# 阶段隔离：单阶段失败不阻断其余阶段，baseline 未提交时可重试收敛
# ---------------------------------------------------------------------------


def _file_sink_cfg(name: str, path: str):
    from core.application.sink_config import ResolvedSinkConfig

    return ResolvedSinkConfig(name=name, type="file", connection={"path": path})


async def test_sink_failure_does_not_block_task_phase_and_retry_converges():
    """Sink 重建抛错：Task 阶段仍应用，baseline 保持，修复后重试全量收敛。"""
    from collector.application.config import CollectionTask

    config = make_collector_config(sinks={"s1": _file_sink_cfg("s1", "/tmp/a.jsonl")})
    candidate = replace(
        config,
        sinks={"s1": _file_sink_cfg("s1", "/tmp/b.jsonl")},
        tasks={
            "t1": CollectionTask(
                task_id="t1", device="dev1", point_group="g", interval=2.0, targets=("s1",)
            )
        },
    )

    failures = {"count": 0}

    def flaky_factory(cfg):  # noqa: ANN001, ANN202
        if failures["count"] == 0:
            failures["count"] += 1
            raise RuntimeError("simulated sink build failure")
        return FakeSink()

    runtime, _ = make_runtime(config, sinks={"s1": FakeSink()}, sink_factory=flaky_factory)
    state = {"config": candidate, "hash": "h2"}
    service = CollectorConfigService(
        runtime,
        config,
        load_config=lambda: state["config"],
        fingerprint=lambda: state["hash"],
        config_hash="h1",
    )

    prepared = await service.prepare_config("r1")
    assert prepared.success, prepared.errors
    result = await service.activate_config("r1")
    assert not result.success
    assert any(error.startswith("sink:") for error in result.errors)

    # Task 阶段隔离：sink 失败未阻断 task 定义更新
    assert runtime.task_runtime.task_definitions()["t1"].interval == 2.0
    # baseline 未提交，可重试
    assert service.current_config is config

    retry = await service.reload()
    assert retry.success, retry.errors
    assert service.current_config.sinks["s1"].connection.path == "/tmp/b.jsonl"  # type: ignore[union-attr]
    assert "s1" in runtime.sinks
