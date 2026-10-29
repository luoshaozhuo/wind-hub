"""协议无关的压测主体（决策 5/6/11）。

流程（决策 4/6）：

1. 启动对应协议的本地 server（:mod:`tests.collector.perf.servers`）；
2. 在 veth 两端应用 netem 场景（中断场景改为测量中触发一次）；
3. 生成压测专用配置（单设备、NullTransport 内存发布——隔离外部 NATS IO
   耗时，专注采集 + 分发链路；丢点只可能来自网络侧）；
4. ``assemble_collector`` + ``CollectorApp.start`` 起真实引擎；
5. 预热 ``warmup_s``（不计入统计）→ 重置基线 → 测量 ``duration_s``；
6. 停引擎、清 netem、返回 :class:`~tests.collector.perf.collector.PerfMetrics`。

延迟经 engine observer 采集（派发前口径）；重连经每秒轮询
驱动 ``health()`` 的跳变识别（不修改任何驱动）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from collector.application.nats_dispatch import SamplePublisher
from collector.assembly import assemble_collector
from contracts.sample_message import SampleMessage
from core.application.port.protocol import ProtocolPort
from core.domain.point_value import PointValue
from core.infrastructure.config import YamlConfigAdapter
from tests.support.config_helper import write_config_tree
from tests.support.netem import NetemController, NetemScenario
from tests.support.perf_collector import MetricsCollector, PerfMetrics
from tests.support.perf_servers import (
    start_ads_server,
    start_iec104_server,
    start_modbus_server,
)

logger = logging.getLogger(__name__)

# 压测服务监听端口。ADS Router 的 TCP transport 固定使用 48898；
# 这与 DeviceConfig.endpoint.port 表示的 AMS Runtime Port（TC2 默认 801）
# 是不同层次的端口。
DEFAULT_PORTS: dict[str, int] = {"modbus": 10502, "iec104": 12404, "ads": 48898}


@dataclass(frozen=True)
class BenchmarkPlan:
    """单协议的数据规模（决策 5）。"""

    device_id: str
    num_points: int
    poll_interval_s: float


# 决策 5：Modbus 1000 点 × 10Hz；IEC104 500 点 × 10Hz。
# ADS 偏离 spec 的「600 点 × 50Hz」：pyads testserver 按名字线性查符号
# （实测 ~0.45ms/符号，600 点一轮 ~270ms），且单条 Sum 请求超过约
# 330 个子命令会撑爆 testserver 4096 字节的接收缓冲（请求被截断、
# 永不应答、客户端 5s 超时）——真实 PLC 两个瓶颈都不存在。为让延迟
# 分布反映网络而非 server 排队，ADS 取 300 点 × 5Hz（每轮 2 条
# Sum × ~70ms ≈ 140ms < 200ms 周期），偏离已写入报告结论节。
PLANS: dict[str, BenchmarkPlan] = {
    "modbus": BenchmarkPlan(device_id="perf-modbus", num_points=1000, poll_interval_s=0.1),
    "iec104": BenchmarkPlan(device_id="perf-iec104", num_points=500, poll_interval_s=0.1),
    "ads": BenchmarkPlan(device_id="perf-ads", num_points=300, poll_interval_s=0.2),
}

# 中断在测量窗口开始这么久之后触发（留出中断前的基线数据）。
_OUTAGE_TRIGGER_DELAY_S = 2.0


# ---------------------------------------------------------------------------
# 配置生成
# ---------------------------------------------------------------------------


def get_device_config(protocol: str, host: str, port: int) -> dict[str, dict[str, object]]:
    """生成对应协议的设备定义（决策 5 的采集间隔来自 :data:`PLANS`）。

    返回便捷写法（``protocol``/``point_table`` 直挂设备）的 devices
    Mapping 单条目（Key 即设备 ID），由
    :func:`tests.support.config_helper.write_config_tree` 派生型号。
    """
    plan = PLANS[protocol]
    if protocol == "modbus":
        protocol_options: dict[str, object] = {"slave_id": 1}
    elif protocol == "iec104":
        protocol_options = {}
    else:  # ads
        # max_subs_per_sum=256：单条 Sum 请求 ~330+ 子命令会撑爆 pyads
        # testserver 的 4096 接收缓冲（截断后永不应答），256 留足余量
        # （驱动默认 500，经 device protocol_options 覆盖，不改驱动）。
        protocol_options = {
            "target_net_id": f"{host}.1.1",
            "twincat_version": "2",
            "max_subs_per_sum": 256,
        }
    return {
        plan.device_id: {
            "protocol": protocol,
            "point_table": "perf",
            "enabled": True,
            "endpoint": {
                "host": host,
                "port": 801 if protocol == "ads" else port,
            },
            "protocol_options": protocol_options,
        }
    }


def get_point_configs(protocol: str, num_points: int) -> dict[str, dict[str, object]]:
    """生成对应协议的点表（地址格式与生产 configs/points.yaml 一致）。"""
    points: dict[str, dict[str, object]] = {}
    for i in range(num_points):
        if protocol == "modbus":
            # step23 起驱动的组合读取按 125 寄存器上限自动切分，
            # 点表可用纯连续地址（1000 点 → 8 条 125 寄存器请求）。
            address: dict[str, object] = {"register_type": "holding", "address": i}
            data_type = "int16"
            point_id = f"r_{i:04d}"
        elif protocol == "iec104":
            address = {"ca": 1, "ioa": 1001 + i, "type_id": "M_ME_NC_1"}
            data_type = "float32"
            point_id = f"mv_{i:04d}"
        else:  # ads（Sum 批量读要求符号寻址）
            address = {"symbol": f"MAIN.var{i}"}
            # 统一 Schema：ADS 点表 data_type 必须使用 PLC 原生名
            # （REAL→逻辑 float32），与生产 configs/points.yaml 一致。
            data_type = "REAL"
            point_id = f"v_{i:04d}"
        points[point_id] = {
            "point_groups": ["default"],
            "address": address,
            "data_type": data_type,
        }
    return points


def write_perf_config(config_dir: Path, protocol: str, host: str, port: int) -> Path:
    """把压测配置写为自包含配置目录，返回该目录。

    Collector 已不再进程内写 Sink；采样默认发往 NATS。压测在装配后用
    :class:`_NullTransport` 替换发布传输（见 :func:`run_benchmark`），不产生
    真实 NATS IO，故 ``sinks`` 留空、``messaging`` 仅为满足 loader 必填。
    """
    plan = PLANS[protocol]
    devices = get_device_config(protocol, host, port)
    points = get_point_configs(protocol, plan.num_points)

    return write_config_tree(
        config_dir,
        devices=devices,
        point_tables={"perf": {"protocol": protocol, "points": points}},
        system={
            "runtime": {
                "shutdown_timeout": 10.0,
                "connect_timeout": 5.0,
                "read_timeout": 5.0,
            },
            # 发布传输在运行时被内存替身替换，不会真正连接此地址。
            "messaging": {
                "backend": "nats",
                "servers": ["nats://127.0.0.1:4222"],
                "delivery": "realtime",
                "max_staleness_ms": 60_000,
            },
        },
        sinks={},
        tasks={
            "perf": {
                "device_group_id": plan.device_id,
                "point_group": "default",
                "interval": plan.poll_interval_s,
            }
        },
    )


class _NullTransport:
    """空发布传输：接受全部消息且不做任何 IO（压测隔离 NATS 耗时）。"""

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def publish(self, message: SampleMessage) -> bool:
        del message
        return True


def _install_null_publisher(app: object, wind_farm_id: str) -> None:
    """把 Collector 的发布传输替换为内存替身（runtime 与 engine 同一实例）。"""
    config = app.boot_config  # type: ignore[attr-defined]
    publisher = SamplePublisher(
        _NullTransport(),
        wind_farm_id=wind_farm_id,
        identity=app.identity,  # type: ignore[attr-defined]
        delivery=config.messaging.delivery,
        max_staleness_ms=config.messaging.max_staleness_ms,
    )
    runtime = app.runtime  # type: ignore[attr-defined]
    runtime._publisher = publisher
    runtime._engine.attach_sample_publisher(publisher)


# ---------------------------------------------------------------------------
# 压测主体
# ---------------------------------------------------------------------------


@dataclass
class _StatsDelta:
    """测量窗口内的运行时计数差值（满足 collector 的 RuntimeStats 协议）。"""

    points_collected: int
    points_routed: int
    points_dropped: int


@asynccontextmanager
async def _server_for(protocol: str, host: str, port: int) -> AsyncIterator[object]:
    """按协议分派 server 启动（决策 11：统一 CM 形态）。"""
    plan = PLANS[protocol]
    if protocol == "modbus":
        async with start_modbus_server(host, port) as server:
            yield server
    elif protocol == "iec104":
        async with start_iec104_server(
            host, port, num_points=plan.num_points, device_id=plan.device_id
        ) as server:
            yield server
    else:  # ads
        async with start_ads_server(host, port, num_vars=plan.num_points):
            yield None


async def run_benchmark(
    protocol: str,
    scenario: NetemScenario,
    host: str,
    port: int | None = None,
    duration_s: float = 60.0,
    warmup_s: float = 10.0,
    netem_devices: list[str] | None = None,
) -> PerfMetrics:
    """跑一个压测场景（协议无关）。

    Args:
        protocol: ``modbus`` / ``iec104`` / ``ads``。
        scenario: 网络场景；``outage_duration_s > 0`` 时在测量窗口
            早期触发一次中断-恢复。
        host: server 侧 IP（veth 的 10.99.0.2）。
        port: server 端口（默认取 :data:`DEFAULT_PORTS`）。
        duration_s: 测量时长（决策 4：默认 60s）。
        warmup_s: 预热时长（决策 4：默认 10s，不计入统计）。
        netem_devices: 需要挂 netem 的接口（veth 两端）；``None``
            表示不注入（无 root 的调试运行）。

    Raises:
        ValueError: 协议未知，或测量时长放不下中断窗口。
    """
    if protocol not in PLANS:
        raise ValueError(f"未知协议 '{protocol}'（可选：{sorted(PLANS)}）")
    plan = PLANS[protocol]
    port = port if port is not None else DEFAULT_PORTS[protocol]
    if scenario.outage_duration_s > 0 and duration_s < scenario.outage_duration_s + (
        _OUTAGE_TRIGGER_DELAY_S + 5.0
    ):
        raise ValueError(
            f"测量时长 {duration_s:g}s 放不下中断场景（中断 {scenario.outage_duration_s:g}s "
            f"+ 触发延迟 {_OUTAGE_TRIGGER_DELAY_S:g}s + 恢复观察 5s）"
        )

    controllers = [NetemController(dev) for dev in (netem_devices or [])]
    collector = MetricsCollector()

    with tempfile.TemporaryDirectory(prefix=f"windhub-perf-{protocol}-") as tmp:
        config_dir = write_perf_config(Path(tmp), protocol, host, port)

        async with _server_for(protocol, host, port):
            for c in controllers:
                c.apply(scenario)  # 中断场景此处是 clear（稳态无规则）

            app = assemble_collector(config_dir)
            site_id = YamlConfigAdapter(config_dir).load_collector().site.site_id
            _install_null_publisher(app, site_id)
            runtime = app.runtime
            runtime.engine.add_observer(_make_latency_observer(collector))

            await app.start()
            # 实例注册为 STOPPED，压测需显式启动全部 Task Instance
            for instance in runtime.task_instances().values():
                await runtime.start_task_instance(instance.instance_id)
            health_task = asyncio.create_task(
                _health_watch(runtime.devices[plan.device_id].protocol, collector)
            )
            outage_task: asyncio.Task[None] | None = None
            try:
                await collector.start()
                logger.info("[%s/%s] 预热 %gs …", protocol, scenario.name, warmup_s)
                await asyncio.sleep(warmup_s)

                # ---- 测量窗口开始：重置基线 ----
                collector.reset_measurement()
                base = _StatsDelta(
                    points_collected=runtime.points_collected,
                    points_routed=runtime.points_routed,
                    points_dropped=runtime.points_dropped,
                )
                if scenario.outage_duration_s > 0:
                    outage_task = asyncio.create_task(
                        _trigger_outage(controllers, scenario.outage_duration_s)
                    )
                logger.info("[%s/%s] 测量 %gs …", protocol, scenario.name, duration_s)
                await asyncio.sleep(duration_s)

                delta = _StatsDelta(
                    points_collected=runtime.points_collected - base.points_collected,
                    points_routed=runtime.points_routed - base.points_routed,
                    points_dropped=runtime.points_dropped - base.points_dropped,
                )
            finally:
                health_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await health_task
                if outage_task is not None:
                    outage_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await outage_task
                await collector.stop()
                await app.stop()
                for c in controllers:
                    c.clear()

    return collector.build_metrics(protocol, scenario.name, delta, duration_s)


def _make_latency_observer(
    collector: MetricsCollector,
) -> Callable[[list[PointValue]], None]:
    """engine observer：把批次里每个点的「采集 → 处理完成」耗时记入采集器。"""

    def _on_batch(values: list[PointValue]) -> None:
        now = datetime.now(UTC)
        for pv in values:
            collector.record_latency((now - pv.timestamp).total_seconds() * 1000.0)

    return _on_batch


async def _health_watch(protocol_driver: ProtocolPort, collector: MetricsCollector) -> None:
    """每秒轮询驱动健康；「健康 → 不健康 → 健康」计为一次重连。

    首次采样只建立基线（驱动启动时本就处于「未连接」状态，若不建
    基线会把启动建连误记为一次「恢复」）。
    """
    was_healthy: bool | None = None
    down_since = 0.0
    while True:
        await asyncio.sleep(1.0)
        healthy = (await protocol_driver.health()).healthy
        if was_healthy is None:
            was_healthy = healthy
            continue
        if was_healthy and not healthy:
            down_since = time.perf_counter()
        elif not was_healthy and healthy:
            collector.record_reconnect((time.perf_counter() - down_since) * 1000.0)
        was_healthy = healthy


async def _trigger_outage(controllers: list[NetemController], duration_s: float) -> None:
    """测量窗口早期触发一次中断：双方向同时 100% 丢包，到时恢复。"""
    await asyncio.sleep(_OUTAGE_TRIGGER_DELAY_S)
    logger.info("注入链路中断 %gs …", duration_s)
    await asyncio.gather(*(c.simulate_outage(duration_s) for c in controllers))
    logger.info("链路已恢复")
