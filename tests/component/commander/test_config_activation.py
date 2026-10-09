"""新 Commander 配置激活一致性回归测试。

覆盖：

- 进程级 ADS 本机身份变化在 prepare 阶段拒绝（restart-required），
  prepared/active 状态不变；协议环境按身份只初始化一次；
- write_timeout 不再缓存启动值——command.timeout <= 0 时使用本次操作
  固定 generation 的 CommanderConfig.write_timeout；
- 诊断 probe 不捕获 boot 配置：reload 后新增/变更 ADS 设备使用 pinned
  generation 的 endpoint/options。
"""

from __future__ import annotations

import asyncio

import pytest

from commander.application.command import Command
from commander.application.config import ADSLocalIdentity, CommanderConfig
from commander.application.diagnostic import CommanderDiagnosticService
from commander.application.dispatcher import CommandDispatcher
from commander.application.runtime import CommanderRuntime
from core.application import ProtocolWriteResult
from tests.support.new_commander import FakeRegistry, make_commander_config

_IDENTITY_A = ADSLocalIdentity(local_ams_net_id="1.2.3.4.1.1", local_ip="127.0.0.1")
_IDENTITY_B_NET_ID = ADSLocalIdentity(local_ams_net_id="9.9.9.9.1.1", local_ip="127.0.0.1")
_IDENTITY_B_IP = ADSLocalIdentity(local_ams_net_id="1.2.3.4.1.1", local_ip="192.168.0.10")

_ADS_ADDRESS = {"symbol": "MAIN.value", "data_type": "float32"}


def _runtime(
    config: CommanderConfig | None = None,
    registry: FakeRegistry | None = None,
    *,
    prepare_calls: list[CommanderConfig] | None = None,
) -> CommanderRuntime:
    async def _prepare(current: CommanderConfig) -> None:
        if prepare_calls is not None:
            prepare_calls.append(current)

    return CommanderRuntime(
        config or make_commander_config(),
        config_hash="hash-a",
        protocol_registry=registry or FakeRegistry(),  # type: ignore[arg-type]
        prepare_protocols=_prepare if prepare_calls is not None else None,
    )


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
    config = make_commander_config(ads_local=current_identity)
    runtime = _runtime(config)
    candidate = make_commander_config(ads_local=candidate_identity)

    with pytest.raises(ValueError, match="ADS local identity change requires process restart"):
        await runtime.prepare_config("r2", candidate, "hash-b")

    # prepared 不进入可激活状态，active generation/hash/revision 不变。
    assert runtime.prepared_revision is None
    assert runtime.active_revision == "startup"
    assert runtime.active_config_hash == "hash-a"
    assert runtime.config is config


async def test_ads_local_identity_unchanged_reload_and_single_prepare():
    prepare_calls: list[CommanderConfig] = []
    config = make_commander_config(ads_local=_IDENTITY_A)
    runtime = _runtime(config, prepare_calls=prepare_calls)

    await runtime.start()
    assert len(prepare_calls) == 1  # 进程 start 时初始化一次

    candidate = make_commander_config(ads_local=_IDENTITY_A, write_timeout=2.0)
    await runtime.prepare_config("r2", candidate, "hash-b")
    await runtime.activate_config("r2")

    assert runtime.active_revision == "r2"
    assert runtime.active_config_hash == "hash-b"
    # identity 未变——activate 不重复 initialize ADS 本机身份。
    assert len(prepare_calls) == 1
    await runtime.stop()


# ---------------------------------------------------------------------------
# write_timeout：绑定 pinned generation
# ---------------------------------------------------------------------------


async def test_startup_write_timeout_applies_to_default_command_timeout():
    registry = FakeRegistry()
    runtime = _runtime(make_commander_config(write_timeout=0.05), registry)

    async def slow_write(writes):
        await asyncio.sleep(5)
        return tuple(
            ProtocolWriteResult(point_id=w.point_id, success=True, message=None) for w in writes
        )

    runtime.device("dev1").protocol.write = slow_write  # type: ignore[attr-defined]
    dispatcher = CommandDispatcher(runtime)
    result = await dispatcher.send(
        Command(command_id="c1", device_id="dev1", point_id="p1", value=1.0, timeout=0)
    )
    assert not result.success
    assert "write timeout" in (result.error or "")


async def test_reloaded_write_timeout_applies_to_new_operations():
    registry = FakeRegistry()
    runtime = _runtime(make_commander_config(write_timeout=0.05), registry)

    async def slow_write(writes):
        await asyncio.sleep(0.2)
        return tuple(
            ProtocolWriteResult(point_id=w.point_id, success=True, message=None) for w in writes
        )

    runtime.device("dev1").protocol.write = slow_write  # type: ignore[attr-defined]
    dispatcher = CommandDispatcher(runtime)

    timed_out = await dispatcher.send(
        Command(command_id="c1", device_id="dev1", point_id="p1", value=1.0, timeout=0)
    )
    assert not timed_out.success
    assert "write timeout" in (timed_out.error or "")

    await runtime.reload(
        make_commander_config(write_timeout=5.0),
        config_hash="hash-b",
        revision_id="r2",
    )
    # reload 后新 operation 使用新 generation 的 write_timeout——不再用启动值。
    runtime.device("dev1").protocol.write = slow_write  # type: ignore[attr-defined]
    completed = await dispatcher.send(
        Command(command_id="c2", device_id="dev1", point_id="p1", value=1.0, timeout=0)
    )
    assert completed.success
    await runtime.stop()


async def test_inflight_operation_uses_pinned_generation_timeout():
    registry = FakeRegistry()
    runtime = _runtime(make_commander_config(write_timeout=5.0), registry)

    async def slow_write(writes):
        await asyncio.sleep(0.1)
        return tuple(
            ProtocolWriteResult(point_id=w.point_id, success=True, message=None) for w in writes
        )

    runtime.device("dev1").protocol.write = slow_write  # type: ignore[attr-defined]
    dispatcher = CommandDispatcher(runtime)

    # 固定旧 generation；期间 activate 一个 write_timeout 极小的新配置。
    async with runtime.operation():
        await runtime.reload(
            make_commander_config(write_timeout=0.01),
            config_hash="hash-b",
            revision_id="r2",
        )
        result = await dispatcher.send(
            Command(command_id="c1", device_id="dev1", point_id="p1", value=1.0, timeout=0)
        )
        # 本次操作 pin 旧 generation——新配置的超时不影响在途操作。
        assert result.success

    # pin 结束后，新操作使用新 generation 的小超时。
    runtime.device("dev1").protocol.write = slow_write  # type: ignore[attr-defined]
    result = await dispatcher.send(
        Command(command_id="c2", device_id="dev1", point_id="p1", value=1.0, timeout=0)
    )
    assert not result.success
    assert "write timeout" in (result.error or "")
    await runtime.stop()


async def test_explicit_command_timeout_takes_precedence():
    registry = FakeRegistry()
    runtime = _runtime(make_commander_config(write_timeout=0.01), registry)

    async def slow_write(writes):
        await asyncio.sleep(0.1)
        return tuple(
            ProtocolWriteResult(point_id=w.point_id, success=True, message=None) for w in writes
        )

    runtime.device("dev1").protocol.write = slow_write  # type: ignore[attr-defined]
    dispatcher = CommandDispatcher(runtime)
    result = await dispatcher.send(
        Command(command_id="c1", device_id="dev1", point_id="p1", value=1.0, timeout=5.0)
    )
    assert result.success


# ---------------------------------------------------------------------------
# 诊断：probe 不捕获 boot 配置，绑定 pinned generation
# ---------------------------------------------------------------------------


class _FakeProbe:
    """记录 connect/resolve 调用的最小 SymbolProbe。"""

    def __init__(self) -> None:
        self.connected = False
        self.resolved: list[str] = []

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def resolve(self, point):
        from commander.application.diagnostic import ResolvedAddress

        self.resolved.append(point.point_id)
        return ResolvedAddress(
            symbol=point.ext.get("symbol"),
            index_group=0xF020,
            index_offset=0,
            size=4,
            protocol_type="REAL",
        )

    async def read_raw(self, point, resolved) -> object:
        return 1.0


def _diagnostic_service(runtime: CommanderRuntime):
    calls: list[tuple[str, dict[str, object]]] = []

    def probe_factory(device, endpoint, options):
        calls.append((str(device.device_id), dict(options)))
        return _FakeProbe()

    async def _ok(*args) -> bool:
        return True

    service = CommanderDiagnosticService(
        runtime,
        ping=lambda host, timeout: _ok(),
        tcp_connect=lambda host, port, timeout: _ok(),
        probe_factory=probe_factory,
    )
    return service, calls


async def test_diagnostic_probe_for_ads_device_added_by_reload():
    runtime = _runtime(make_commander_config())  # boot：仅 modbus dev1
    service, calls = _diagnostic_service(runtime)

    # reload 新增 ADS 设备 dev2——boot config 中不存在该设备。
    candidate = make_commander_config(
        device_id="dev2",
        protocol="ads",
        address=_ADS_ADDRESS,
        protocol_options_by_device={"ams_net_id": "5.6.7.8.1.1"},
    )
    await runtime.reload(candidate, config_hash="hash-b", revision_id="r2")

    result = await service.resolve_point("dev2", "p1")
    # 不读取 boot config（否则 point_table_for_device('dev2') 已 KeyError）；
    # options 来自新 generation。
    assert result.error is None
    assert calls == [("dev2", {"ams_net_id": "5.6.7.8.1.1"})]
    await runtime.stop()


async def test_diagnostic_uses_new_generation_after_protocol_switch_to_ads():
    runtime = _runtime(make_commander_config())  # boot：dev1 为 modbus
    service, calls = _diagnostic_service(runtime)

    candidate = make_commander_config(
        protocol="ads",
        address=_ADS_ADDRESS,
        protocol_options_by_device={"ams_net_id": "5.6.7.8.1.1"},
    )
    await runtime.reload(candidate, config_hash="hash-b", revision_id="r2")

    result = await service.resolve_point("dev1", "p1")
    assert result.protocol == "ads"
    assert result.error is None
    assert calls == [("dev1", {"ams_net_id": "5.6.7.8.1.1"})]
    await runtime.stop()


async def test_inflight_diagnostic_uses_pinned_generation_options():
    registry = FakeRegistry()
    runtime = _runtime(
        make_commander_config(
            protocol="ads",
            address=_ADS_ADDRESS,
            protocol_options_by_device={"ams_net_id": "old"},
        ),
        registry,
    )
    service, calls = _diagnostic_service(runtime)

    async with runtime.operation():
        # pin 旧 generation 期间 activate 新 options——本次 probe 不受影响。
        await runtime.reload(
            make_commander_config(
                protocol="ads",
                address=_ADS_ADDRESS,
                protocol_options_by_device={"ams_net_id": "new"},
            ),
            config_hash="hash-b",
            revision_id="r2",
        )
        result = await service.resolve_point("dev1", "p1")
        assert result.error is None
        assert calls == [("dev1", {"ams_net_id": "old"})]

    # pin 结束后使用新 generation options。
    result = await service.resolve_point("dev1", "p1")
    assert result.error is None
    assert calls[-1] == ("dev1", {"ams_net_id": "new"})
    await runtime.stop()
