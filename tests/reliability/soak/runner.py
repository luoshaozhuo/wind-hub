"""soak 负载验收的运行主体——在真实采集引擎上施加目标负载形态并计量。

与 perf runner 的关系：perf 回答「协议在某种网络下有多快」（单设备、
NullSink、netem 矩阵、吞吐/延迟基准）；soak 回答「系统在目标负载形态下
长期跑得对不对」（多组节拍、多设备、真实 sink、写混合、故障风暴下的
节拍保持/丢失/重复/资源泄漏）。两者共享 server、/proc 采样与 netem 组件。

负载模型：一个 :class:`LoadGroup` = N 台设备 × 每台 M 点 × 固定采集间隔；
一个 profile 可含多组（如 100 点 @10Hz + 500 点 @1Hz 的目标负载）。每个
设备一个采集任务（任务即实例），周期观测按设备归属。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from tests.performance.netem import NetemController, NetemScenario
from tests.performance.servers import ModbusServerHandle
from tests.reliability.soak.metrics import SoakMetrics, SoakMetricsCollector
from tests.reliability.soak.sinks import RecordingSink, percentile
from wind_hub_collector.assembly import AssembledRuntime, assemble, start_runtime, stop_runtime
from wind_hub_core.config.sinks import ResolvedSinkConfig
from wind_hub_core.model.command import Command
from wind_hub_core.model.point import PointValue
from wind_hub_core.protocol.port import ProtocolPort

logger = logging.getLogger(__name__)

#: soak 专用 Modbus server 端口基准（与 perf 10502 错开，支持并行）。
DEFAULT_SOAK_PORT = 10512


@dataclass(frozen=True)
class LoadGroup:
    """一组同构负载：``devices`` 台设备 × 每台 ``points_per_device`` 点 ×
    ``interval_s`` 采集间隔（每台设备一个采集任务）。"""

    name: str
    devices: int
    points_per_device: int
    interval_s: float


@dataclass(frozen=True)
class ReconnectStorm:
    """重连风暴：测量窗口内每 ``interval_s`` 停起一次协议 server，共 ``count`` 次。

    ``down_s`` 是单次停服时长——必须明显大于健康轮询周期（1s），否则
    「不健康 → 恢复」跳变可能落在两次轮询之间而漏计重连。
    """

    interval_s: float
    count: int
    down_s: float = 2.0


@dataclass(frozen=True)
class SoakProfile:
    """soak 负载形态定义（模块级常量，可直接扩展）。"""

    name: str
    groups: tuple[LoadGroup, ...]
    sink: str = "null"
    """``null``（仅计量）/ ``kafka`` / ``postgres``（真实 sink + 计量包装）。"""
    write_interval_s: float | None = None
    """混合读写：测量窗口内按该间隔轮询下发写命令；None 表示纯采集。"""
    storm: ReconnectStorm | None = None
    device_extensions: dict[str, object] | None = None
    """合并进每台设备 ``endpoint.extensions`` 的协议级覆盖（如 modbus
    ``timeout``）——风暴等场景需要把断连检测延迟压到远小于停起间隔，
    否则健康采样会把相邻断连窗口合并成一次。"""

    @property
    def total_points_per_cycle(self) -> int:
        return sum(g.devices * g.points_per_device for g in self.groups)

    @property
    def expected_pps(self) -> float:
        return sum(g.devices * g.points_per_device / g.interval_s for g in self.groups)


# 验收要求的负载形态（spec §13）。network_latency / packet_loss 不是独立
# profile——它们是施加在 base 负载上的 netem 场景，见 PROFILES 的注释与
# test_target_load.py 的对应用例。
PROFILES: dict[str, SoakProfile] = {
    # 目标负载：100 点 ×10Hz + 500 点 ×1Hz（= 1500 点/s 稳态）。
    "target_100x10hz_500x1hz": SoakProfile(
        name="target_100x10hz_500x1hz",
        groups=(
            LoadGroup(name="fast", devices=1, points_per_device=100, interval_s=0.1),
            LoadGroup(name="slow", devices=1, points_per_device=500, interval_s=1.0),
        ),
    ),
    # 规模负载：200 台设备 × 10 点 × 2s（= 1000 点/s，设备连接数维度）。
    "multi_device_200": SoakProfile(
        name="multi_device_200",
        groups=(LoadGroup(name="dev", devices=200, points_per_device=10, interval_s=2.0),),
    ),
    # 混合读写：采集 + 每 0.2s 一条写命令。
    "mixed_read_write": SoakProfile(
        name="mixed_read_write",
        groups=(LoadGroup(name="rw", devices=2, points_per_device=50, interval_s=0.2),),
        write_interval_s=0.2,
    ),
    # 真实 sink 输出开销。
    "kafka_output": SoakProfile(
        name="kafka_output",
        groups=(LoadGroup(name="out", devices=2, points_per_device=100, interval_s=0.2),),
        sink="kafka",
    ),
    "postgres_output": SoakProfile(
        name="postgres_output",
        groups=(LoadGroup(name="out", devices=2, points_per_device=100, interval_s=0.2),),
        sink="postgres",
    ),
    # 重连风暴：5 次停起（每次停 2s，间隔 4s），验证节拍在反复断连下的
    # 保持能力。风暴总时长 = 5 × (4 + 2) = 30s，测量窗口须覆盖。
    #
    # 场景参数对断连检测延迟的约束（否则 1s 健康采样会把相邻停起合并
    # 成一次「不健康→健康」跳变，重连计数偏低）：
    # - modbus timeout 固定 1s：停服瞬间若恰好有在途读，最坏检测延迟
    #   ≈ timeout + 轮询间隔 ≈ 1.2s（默认 5s 会横跨整个停起周期）；
    # - 间隔 4s：重连 worst case ≈ 检测 1.2s + 两次退避尝试 ≈ 3.2s，
    #   恢复后距下次停服仍 ≥ 2s，健康采样必能采到「健康」样本。
    "reconnect_storm": SoakProfile(
        name="reconnect_storm",
        groups=(LoadGroup(name="storm", devices=1, points_per_device=50, interval_s=0.2),),
        storm=ReconnectStorm(interval_s=4.0, count=5),
        device_extensions={"timeout": 1.0},
    ),
    # 施加 network_latency / packet_loss netem 场景时使用的基准负载。
    "netem_base": SoakProfile(
        name="netem_base",
        groups=(LoadGroup(name="base", devices=1, points_per_device=100, interval_s=0.2),),
    ),
}


@dataclass
class WriteStats:
    """混合读写的写命令统计。"""

    commands: int = 0
    failures: int = 0


def _points_payload(group: LoadGroup) -> list[dict[str, object]]:
    return [
        {
            "point_id": f"r.{i:04d}",
            "point_groups": ["default"],
            "address": {"register_type": "holding", "address": i},
            "data_type": "int16",
        }
        for i in range(group.points_per_device)
    ]


def write_soak_config(
    config_dir: Path,
    profile: SoakProfile,
    host: str,
    port: int,
    *,
    kafka_bootstrap: str | None = None,
    kafka_topic: str | None = None,
    postgres_dsn: str | None = None,
    postgres_table: str | None = None,
) -> Path:
    """生成 soak 配置目录（多设备/多任务/可选真实 sink）。

    背压队列放大到 100 万：丢点只反映真实瓶颈而非人为触顶。sink 实例
    由 ``assemble`` 的 ``sink_factory`` 注入计量包装，``type`` 字段决定
    工厂创建哪种真实内部 sink。
    """
    devices: list[dict[str, object]] = []
    device_models: dict[str, object] = {}
    point_tables: dict[str, object] = {}
    tasks: list[dict[str, object]] = []

    for group in profile.groups:
        model_name = f"model_{group.name}"
        table_name = f"pts_{group.name}"
        device_models[model_name] = {
            "device_type": "turbine",
            "protocol": "modbus",
            "point_table": table_name,
        }
        point_tables[table_name] = {
            "protocol": "modbus",
            "points": _points_payload(group),
        }
        for i in range(group.devices):
            device_id = f"{group.name}-{i:03d}"
            devices.append(
                {
                    "device_id": device_id,
                    "model": model_name,
                    "enabled": True,
                    "endpoint": {
                        "host": host,
                        "port": port,
                        "extensions": {"unit_id": 1, **(profile.device_extensions or {})},
                    },
                }
            )
            tasks.append(
                {
                    "task_id": device_id,
                    "device": device_id,
                    "point_group": "default",
                    "interval": group.interval_s,
                    "targets": [{"sink": "soak_sink"}],
                }
            )

    sink_params: dict[str, object] = {}
    if profile.sink == "kafka":
        if kafka_bootstrap is None or kafka_topic is None:
            raise ValueError("kafka profile 需要 kafka_bootstrap / kafka_topic")
        sink_params = {"bootstrap_servers": kafka_bootstrap, "topic": kafka_topic}
    elif profile.sink == "postgres":
        if postgres_dsn is None or postgres_table is None:
            raise ValueError("postgres profile 需要 postgres_dsn / postgres_table")
        sink_params = {
            "dsn": postgres_dsn,
            "table": postgres_table,
            "create_table": True,
        }

    sink_type = {
        "null": "file",
        "kafka": "kafka",
        "postgres": "db",
    }[profile.sink]
    sink_connection = (
        {"path": str(config_dir / "soak-null.jsonl")}
        if profile.sink == "null"
        else sink_params
    )
    system = {
        "runtime": {
            "queue_maxsize": 1_000_000,
            "backpressure_policy": "drop_old",
            "shutdown_timeout": 10.0,
            "connect_timeout": 5.0,
            "read_timeout": 5.0,
        },
        "interfaces": {"api": {"enabled": False}},
    }
    sinks = {
        "sinks": [
            {
                "name": "soak_sink",
                "type": sink_type,
                "enabled": True,
                "connection": sink_connection,
                "points": [],
            }
        ]
    }
    files = {
        config_dir / "units.yaml": {"units": {"none": {"symbol": "", "name": "Dimensionless"}}},
        config_dir / "device_models.yaml": {
            "device_types": {"turbine": {"name": "风机"}},
            "device_models": device_models,
        },
        config_dir / "points.yaml": {"point_tables": point_tables},
        config_dir / "system.yaml": system,
        config_dir / "sinks.yaml": sinks,
        config_dir / "devices.yaml": {"devices": devices},
        config_dir / "tasks.yaml": {"tasks": tasks},
    }
    config_dir.mkdir(parents=True, exist_ok=True)
    for path, payload in files.items():
        path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return config_dir


async def run_soak(
    profile: SoakProfile,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_SOAK_PORT,
    duration_s: float = 30.0,
    warmup_s: float = 3.0,
    kafka_bootstrap: str | None = None,
    postgres_dsn: str | None = None,
    kafka_topic: str | None = None,
    postgres_table: str | None = None,
    netem_devices: list[str] | None = None,
    netem_scenario: NetemScenario | None = None,
    on_ready: Callable[[], None] | None = None,
) -> SoakMetrics:
    """跑一个 soak 负载形态，返回验收指标。

    Args:
        profile: 负载形态（:data:`PROFILES` 或自定义）。
        host: Modbus server 绑定地址（netem 场景传 veth 的 server 侧 IP）。
        port: Modbus server 端口。
        duration_s: 测量时长（秒）。
        warmup_s: 预热时长（秒，不计入统计）。
        kafka_bootstrap / postgres_dsn: 真实 sink profile 的服务地址。
        kafka_topic / postgres_table: 显式 topic/表名（测试要做独立消费/
            SQL 核验时传入）；缺省每次运行随机生成，避免历史数据干扰计数。
        netem_devices / netem_scenario: 同时给出时在测量前应用 netem 场景。
        on_ready: 预热结束、测量窗口开始的回调（长 soak 用来打边界日志）。
    """
    if duration_s <= 0 or warmup_s < 0:
        raise ValueError("duration_s 必须为正，warmup_s 不能为负")

    topic = kafka_topic or f"windhub-soak-{uuid.uuid4().hex[:12]}"
    table = postgres_table or f"windhub_soak_{uuid.uuid4().hex[:12]}"
    recording_holder: list[RecordingSink] = []

    def _sink_factory(cfg: ResolvedSinkConfig) -> RecordingSink:
        inner = None
        if profile.sink == "kafka":
            from wind_hub_collector.adapter.outbound.sink.mq.kafka import KafkaSink

            inner = KafkaSink(cfg)
        elif profile.sink == "postgres":
            from wind_hub_collector.adapter.outbound.sink.db.postgres import DBSink

            inner = DBSink(cfg)
        sink = RecordingSink(inner)
        recording_holder.append(sink)
        return sink

    controllers = [NetemController(dev) for dev in (netem_devices or [])]
    task_intervals = {
        f"{g.name}-{i:03d}": g.interval_s for g in profile.groups for i in range(g.devices)
    }
    collector = SoakMetricsCollector(task_intervals)
    write_stats = WriteStats()
    server = ModbusServerHandle(host, port)

    with tempfile.TemporaryDirectory(prefix=f"windhub-soak-{profile.name}-") as tmp:
        config_dir = write_soak_config(
            Path(tmp),
            profile,
            host,
            port,
            kafka_bootstrap=kafka_bootstrap,
            kafka_topic=topic,
            postgres_dsn=postgres_dsn,
            postgres_table=table,
        )
        await server.start()
        try:
            for c in controllers:
                if netem_scenario is not None:
                    c.apply(netem_scenario)

            rt = assemble(config_dir, sink_factory=_sink_factory)
            runtime = rt.runtime
            rt.engine.add_observer(_make_cycle_observer(collector))
            collector.set_queue_depth_provider(runtime.sink_queue_depths)

            await start_runtime(rt)
            for instance in runtime.task_instances().values():
                await runtime.start_task_instance(instance.instance_id)

            devices = [runtime.devices[did].protocol for did in sorted(runtime.devices)]
            # 风暴场景：包装驱动层建连，精确计数重连事件（健康采样会漏，
            # 见 _ConnectEventCounter 注释）；在 start_runtime 之后包装，
            # 初次建连不计入。
            connect_events = _ConnectEventCounter()
            if profile.storm is not None:
                for driver in devices:
                    connect_events.wrap(driver)
            health_task = asyncio.create_task(_health_watch(devices, collector))
            writer_task: asyncio.Task[None] | None = None
            storm_task: asyncio.Task[None] | None = None
            try:
                await collector.start()
                logger.info("[%s] 预热 %gs …", profile.name, warmup_s)
                await asyncio.sleep(warmup_s)

                # ---- 测量窗口开始：重置基线 ----
                collector.reset_measurement()
                sink0 = recording_holder[0]
                base_received = sink0.stats.received
                base_duplicates = sink0.stats.duplicates
                sink0.stats.latencies_ms.clear()
                base = (
                    runtime.points_collected,
                    runtime.points_routed,
                    runtime.points_dropped,
                )
                if on_ready is not None:
                    on_ready()
                if profile.write_interval_s is not None:
                    writer_task = asyncio.create_task(
                        _write_loop(rt, profile, write_stats)
                    )
                if profile.storm is not None:
                    storm_task = asyncio.create_task(_storm_loop(server, profile.storm))
                logger.info("[%s] 测量 %gs …", profile.name, duration_s)
                await asyncio.sleep(duration_s)

                collected = runtime.points_collected - base[0]
                routed = runtime.points_routed - base[1]
                dropped = runtime.points_dropped - base[2]
            finally:
                for task in (writer_task, storm_task):
                    if task is not None:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                health_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await health_task
                await collector.stop()
                await stop_runtime(rt)
                for c in controllers:
                    c.clear()
        finally:
            await server.stop()

    sink = recording_holder[0]
    resources = collector.resource_summary()
    sampled_reconnects, reconnect_time_ms = collector.reconnect_summary()
    # 风暴场景以驱动层精确建连事件为准；采样计数仅供对照（短不健康
    # 窗口会被 1s 采样漏掉，见 _ConnectEventCounter）。
    reconnect_count = (
        connect_events.count if profile.storm is not None else sampled_reconnects
    )
    return SoakMetrics(
        profile=profile.name,
        duration_s=duration_s,
        cycles=collector.build_cycles(),
        points_collected=collected,
        points_routed=routed,
        points_dropped=dropped,
        throughput_pps=collected / duration_s if duration_s > 0 else 0.0,
        sink_received=sink.stats.received - base_received,
        sink_duplicates=sink.stats.duplicates - base_duplicates,
        sink_received_total=sink.stats.received,
        sink_latency_p50_ms=percentile(sink.stats.latencies_ms, 50),
        sink_latency_p99_ms=percentile(sink.stats.latencies_ms, 99),
        write_commands=write_stats.commands,
        write_failures=write_stats.failures,
        queue_depth_max=int(resources["queue_depth_max"]),
        asyncio_tasks_max=int(resources["asyncio_tasks_max"]),
        cpu_percent=resources["cpu_percent"],
        memory_mb=resources["memory_mb"],
        fd_count=int(resources["fd_count"]),
        reconnect_count=reconnect_count,
        reconnect_time_ms=reconnect_time_ms,
        memory_start_mb=resources["memory_start_mb"],
        memory_end_mb=resources["memory_end_mb"],
        asyncio_tasks_start=int(resources["asyncio_tasks_start"]),
        asyncio_tasks_end=int(resources["asyncio_tasks_end"]),
    )


def _make_cycle_observer(
    collector: SoakMetricsCollector,
) -> Callable[[list[PointValue]], None]:
    """engine observer：批次到达即记该设备一轮采集（soak 任务按设备命名）。"""

    def _on_batch(values: list[PointValue]) -> None:
        if values:
            collector.record_batch(values[0].device_id)

    return _on_batch


class _ConnectEventCounter:
    """精确统计驱动层 TCP 建连成功次数（风暴场景的重连计数）。

    为什么不能依赖 1s 健康采样：pymodbus 内部重试会吸收停服——在途读
    要等重试预算耗尽（约 3 × transaction timeout）才向驱动抛出失败，
    期间 ``driver.health()`` 自认健康；真正断连后驱动的后台 monitor 又
    在百毫秒内重连成功。不健康窗口可能只有 ~0.1s，任何固定频率采样
    都会漏计，且漏不漏取决于停服与轮询的相位对齐（抖动即 flake）。

    驱动每次成功建立 TCP 连接都要经过 ``_do_connect``（显式 connect 与
    后台 monitor 两条路径共用）——「重连发生」的精确边界信号，不存在
    采样混叠。风暴 profile 只用 modbus，故按鸭子类型包装该私有方法。
    """

    def __init__(self) -> None:
        self.count = 0

    def wrap(self, driver: object) -> None:
        original = driver._do_connect  # type: ignore[attr-defined]

        async def _spy() -> None:
            await original()
            self.count += 1

        driver._do_connect = _spy  # type: ignore[attr-defined, method-assign]


async def _health_watch(
    drivers: list[ProtocolPort], collector: SoakMetricsCollector
) -> None:
    """每秒轮询全部驱动健康；任一「健康 → 不健康 → 健康」计一次重连。

    首次采样只建基线（启动建连不记为恢复）。风暴场景设备少；200 设备
    场景的 health() 是缓存同步读，每秒 200 次开销可忽略。
    """
    states: dict[int, bool] = {}
    down_since: dict[int, float] = {}
    while True:
        await asyncio.sleep(1.0)
        for idx, driver in enumerate(drivers):
            healthy = driver.health().healthy
            if idx not in states:
                states[idx] = healthy
                continue
            was = states[idx]
            if was and not healthy:
                down_since[idx] = time.perf_counter()
            elif not was and healthy:
                collector.record_reconnect((time.perf_counter() - down_since[idx]) * 1000.0)
            states[idx] = healthy


async def _write_loop(rt: AssembledRuntime, profile: SoakProfile, stats: WriteStats) -> None:
    """混合读写：按 write_interval_s 轮询向各设备下发写命令（值确定性轮换）。"""
    assert profile.write_interval_s is not None
    device_ids = [f"{g.name}-{i:03d}" for g in profile.groups for i in range(g.devices)]
    tick = 0
    while True:
        await asyncio.sleep(profile.write_interval_s)
        device_id = device_ids[tick % len(device_ids)]
        result = await rt.command.send(
            Command(
                command_id=uuid.uuid4().hex,
                device_id=device_id,
                point_id="r.0000",
                value=tick % 0x8000,
            )
        )
        stats.commands += 1
        if not result.success:
            stats.failures += 1
        tick += 1


async def _storm_loop(server: ModbusServerHandle, storm: ReconnectStorm) -> None:
    """重连风暴：周期停起协议 server（驱动侧表现为连接丢失 → 重连）。"""
    for i in range(storm.count):
        await asyncio.sleep(storm.interval_s)
        logger.info("风暴 %d/%d：停 server %gs …", i + 1, storm.count, storm.down_s)
        await server.stop()
        await asyncio.sleep(storm.down_s)
        await server.start()
