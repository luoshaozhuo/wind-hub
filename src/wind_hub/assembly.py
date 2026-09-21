"""组合根（composition root）——依赖装配与生命周期编排。

职责：把 config 层加载出的配置，装配成完整的对象图（协议驱动 / sink /
处理器 / 采集引擎 / Runtime / Use Case），并通过
:func:`start_runtime` / :func:`stop_runtime` 编排运行时的启动与优雅停机。

装配顺序：Protocol/Sink/Processor/Pipeline → AcquisitionEngine →
Runtime（持有引擎、Task 定义与 Dispatcher）→ Use Case。
``assemble()`` 返回的 :class:`AssembledRuntime` 以 ``runtime`` 为运行核心。

不负责：Web API 的真实启动与 SIGHUP 热重载监听（见 ``main.py``）、
``/metrics`` 端点（见 webapi 适配器）。这里负责把采集计数器回调
（``on_points_collected``）注入采集引擎，并把命令/查询/配置/Task
用例装配为对 Dispatcher / Runtime 的真实委托。

关键 side effect：导入协议驱动包触发自注册（见
:mod:`wind_hub.infra.registry`）；构造过程纯同步、无网络 I/O，
真正的连接发生在 :func:`start_runtime` 时由 Runtime 完成。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import uvicorn

import wind_hub.adapter.outbound.processor  # noqa: F401 触发处理器自注册
import wind_hub.adapter.outbound.protocol  # noqa: F401 触发协议驱动自注册
from wind_hub.adapter.inbound.iec104_slave import (
    DataSnapshot,
    IEC104SlaveHandlers,
    IEC104SlaveServer,
    SlaveBridge,
    build_data_type_mapping,
    build_ioa_mapping,
    build_reverse_mapping,
)
from wind_hub.adapter.outbound.sink.db.postgres import DBSink
from wind_hub.adapter.outbound.sink.file.csv import FileSink
from wind_hub.adapter.outbound.sink.mq.kafka import KafkaSink
from wind_hub.application.port.sink import SinkPort
from wind_hub.application.runtime import Runtime
from wind_hub.application.usecase.command import CommandUseCase
from wind_hub.application.usecase.config import ConfigUseCase
from wind_hub.application.usecase.query import QueryUseCase
from wind_hub.application.usecase.task import TaskUseCase
from wind_hub.config.loader import load_config
from wind_hub.config.schema import (
    Config,
    DeviceConfig,
    PointConfig,
    ReportingConfig,
    SinkConfig,
)
from wind_hub.domain.acquisition import AcquisitionEngine
from wind_hub.domain.command import Dispatcher
from wind_hub.domain.model.errors import ConfigError
from wind_hub.domain.port.outbound import (
    PointsConfigurable,
    ProcessorPort,
    ProtocolPort,
)
from wind_hub.domain.processing import Pipeline
from wind_hub.infra import metrics
from wind_hub.infra.processor_registry import processor_registry
from wind_hub.infra.registry import protocol_registry

logger = logging.getLogger(__name__)


@dataclass
class AssembledRuntime:
    """一次装配的产物——完整的运行时对象图。

    ``start_runtime`` / ``stop_runtime`` 依赖其中 ``runtime`` 完成启动与
    停机；``config`` / ``tasks`` / ``command`` / ``query``
    四个 Use Case 暴露给 inbound 适配器（通过 ``AppContext``）。
    """

    boot_config: Config
    """启动时装载的完整配置快照；热重载后的生效配置以
    ``config.current_config`` 为准。"""

    runtime: Runtime
    """运行时——组件生命周期与状态编排核心，持有引擎/Task 定义/分发器。"""

    engine: AcquisitionEngine
    """采集引擎（与 ``runtime.engine`` 同一实例，便于直接注册观察者）。"""

    dispatcher: Dispatcher
    """指令分发器，负责写指令路由与幂等。"""

    pipeline: Pipeline
    """初始处理器链（热重载后同样可能被替换）。"""

    sinks: dict[str, SinkPort]
    """按 sink 名索引的 sink 实例。"""

    protocols: dict[str, ProtocolPort]
    """按 device_id 索引的协议驱动实例。"""

    config: ConfigUseCase
    """配置热重载用例。"""

    tasks: TaskUseCase
    """采集 Task 生命周期管理用例。"""

    command: CommandUseCase
    """指令下发用例。"""

    query: QueryUseCase
    """只读查询用例（含系统状态查询）。"""

    iec104_slave: IEC104SlaveServer | None = None
    """可选的 IEC104 从站代理（reporting.yaml 存在时装配），否则 ``None``."""


def assemble(
    config_dir: str | Path,
    sink_factory: Callable[[SinkConfig], SinkPort] | None = None,
) -> AssembledRuntime:
    """同步纯装配——从配置目录构建完整对象图，不做任何网络 I/O。

    Args:
        config_dir: 配置目录，含 system/devices/points/tasks.yaml。
        sink_factory: 可选 sink 工厂，覆盖默认的 ``kafka``/``file``/``db``
            dispatching。供测试注入 ``null`` sink 等非生产实现；为 ``None``
            时回落到 :func:`_create_sink`。

    Returns:
        装配完成的 :class:`AssembledRuntime`。

    Raises:
        ConfigError: 配置缺失/非法，或引用了未注册的协议/sink/处理器。
    """
    cfg = load_config(config_dir)

    make_sink = sink_factory or _create_sink

    # 设备无关点表经设备绑定解析为 {device_id: [PointConfig, …]}——同一表
    # 被多设备共享时，各设备键指向同一 list 对象（不逐设备复制）。
    points_by_device = cfg.points_by_device()

    protocols = {d.device_id: _create_protocol(d) for d in cfg.devices.devices}
    sinks = {s.name: make_sink(s) for s in cfg.system.sinks}
    processors = [
        _create_processor(name, points_by_device) for name in cfg.system.pipeline.processors
    ]
    pipeline = Pipeline(processors)

    dispatcher = Dispatcher(
        protocols,
        # 命令计数回调接到 Prometheus 计数器：domain 不依赖 infra，由组合根注入。
        # 默认写超时来自 system.yaml（Command.timeout > 0 时以命令自带值优先）。
        default_timeout=cfg.system.runtime.write_timeout,
        on_command_sent=metrics.commands_sent_total.inc,
        on_command_failed=metrics.commands_failed_total.inc,
    )

    devices = {d.device_id: d for d in cfg.devices.devices}

    # 采集引擎：执行「读 → 处理 → 按 Task targets 派发」单次链路；采集回调
    # 接 Prometheus 计数器（domain 不依赖 infra，由组合根注入）。
    # read_timeout 是应用层对一次批量读的外层兜底（协议内部超时仍各自保留）。
    engine = AcquisitionEngine(
        protocols=protocols,
        pipeline=pipeline,
        points_by_device=points_by_device,
        on_points_collected=lambda n: metrics.points_collected_total.inc(n),
        on_points_bad=lambda n: metrics.points_bad_total.inc(n),
        read_timeout=cfg.system.runtime.read_timeout,
    )

    # Runtime：组件生命周期与状态编排核心，持有引擎/Task 定义/分发器；
    # 热重载重建组件用的工厂一并注入，使 Runtime 不依赖具体适配器。
    runtime = Runtime(
        devices=devices,
        protocols=protocols,
        sinks=sinks,
        engine=engine,
        dispatcher=dispatcher,
        config=cfg.system.runtime,
        tasks={t.task_id: t for t in cfg.tasks.tasks},
        points_by_device=points_by_device,
        protocol_factory=_create_protocol,
        sink_factory=make_sink,
        processor_factory=_create_processor,
        # 运行时事件指标（connect 失败/重连/collect 完成）接 Prometheus；
        # application 不 import infra.metrics，由组合根注入结构化实现。
        metrics_hook=metrics.PrometheusRuntimeMetrics(),
    )

    # ConfigUseCase 在构造时二次加载配置作为初始快照，用于后续热重载 diff；
    # 具体重构委托给 Runtime.reconfigure。
    config = ConfigUseCase(config_dir, runtime)
    # Task 管理经 Runtime；命令/查询用例委托 Dispatcher / Runtime。
    tasks = TaskUseCase(runtime)
    command = CommandUseCase(dispatcher)
    query = QueryUseCase(runtime)

    # IEC104 从站代理：reporting.yaml 存在时才装配（可选组件）。
    iec104_slave: IEC104SlaveServer | None = None
    if cfg.reporting is not None and cfg.reporting.reporting:
        iec104_slave = _build_iec104_slave(cfg.reporting, engine, dispatcher)

    return AssembledRuntime(
        boot_config=cfg,
        runtime=runtime,
        engine=engine,
        dispatcher=dispatcher,
        pipeline=pipeline,
        sinks=sinks,
        protocols=protocols,
        config=config,
        tasks=tasks,
        command=command,
        query=query,
        iec104_slave=iec104_slave,
    )


async def start_runtime(
    rt: AssembledRuntime,
    api_server: uvicorn.Server | None = None,
) -> asyncio.Task[None] | None:
    """启动运行时——先监听 API 端口，再启动 Runtime（决策 1）。

    顺序：
      1. 若提供了 ``api_server``，先以 ``asyncio.Task`` 启动
         ``uvicorn.Server.serve``——端口先行监听；即使随后所有设备连接
         都超时，``/health`` 也已可响应（此刻 ``runtime.running`` 为
         ``False``，健康端点如实报告 ``down``，与「API 可用但引擎尚未
         就绪」的语义区分）。
      2. 启动 Runtime（连接设备 + 打开 sink + 注册采集 Task
         Instance）——每台不可达设备都要等满 ``connect_timeout``，多台设备时
         可能耗时数十秒，这正是 API 必须先行的原因。设备连接失败仅记录
         日志并跳过。
      3. 若装配了 IEC104 从站代理，最后启动它（best-effort）。

    幂等：Runtime 已在运行时第 2 步为无操作。

    Args:
        rt: 装配完成的运行时。
        api_server: 可选的内嵌 uvicorn 服务器；``None`` 时本函数不触碰
            API（兼容测试与纯引擎用法）。

    Returns:
        API 服务任务（未提供 ``api_server`` 时为 ``None``）。停机由调用方
        置 ``server.should_exit = True`` 后等待该任务完成。
    """
    api_task: asyncio.Task[None] | None = None
    if api_server is not None:
        api_task = asyncio.create_task(api_server.serve())
    await rt.runtime.start()
    if rt.iec104_slave is not None:
        try:
            await rt.iec104_slave.start()
            logger.info("IEC104 从站代理已启动")
        except Exception:
            logger.warning("IEC104 从站代理启动失败——引擎继续运行", exc_info=True)
    return api_task


async def stop_runtime(rt: AssembledRuntime, timeout: float = 30.0) -> None:
    """优雅停机——先停从站代理，再停实例采集协程、清空队列、flush 并关闭
    sink。

    幂等：Runtime 未运行时直接返回。``timeout`` 是外层硬性上限，防止停机
    无限阻塞（Runtime 内部另受 ``system.yaml`` 的 ``runtime.shutdown_timeout``
    软约束）；若超时则抛 ``TimeoutError``，由调用方决定如何处理。

    Args:
        rt: 待停机的运行时。
        timeout: 停机整体硬超时（秒）。
    """
    if rt.iec104_slave is not None:
        try:
            await rt.iec104_slave.stop()
        except Exception:
            logger.warning("IEC104 从站代理停止失败", exc_info=True)
    await asyncio.wait_for(rt.runtime.stop(), timeout=timeout)


# ---------------------------------------------------------------------------
# 工厂——组合根按配置字符串创建具体实现
# ---------------------------------------------------------------------------


def _create_protocol(cfg: DeviceConfig) -> ProtocolPort:
    """按设备配置的 ``protocol`` 名从注册表创建驱动。"""
    return protocol_registry.create(cfg.protocol, cfg)


def _build_iec104_slave(
    reporting: ReportingConfig,
    engine: AcquisitionEngine,
    dispatcher: Dispatcher,
) -> IEC104SlaveServer:
    """装配 IEC104 从站代理对象图并把它挂到采集引擎的 observer 上。

    快照由 :class:`SlaveBridge.on_points_collected` 作为引擎 observer
    持续刷新；代理服务器只做纯同步的装配，网络监听发生在 ``start``。
    """
    snapshot = DataSnapshot()
    ioa_mapping = build_ioa_mapping(reporting.reporting)
    data_type_mapping = build_data_type_mapping(reporting.reporting)
    reverse_mapping = build_reverse_mapping(reporting.reporting)

    bridge = SlaveBridge(dispatcher, snapshot, ioa_mapping)
    engine.add_observer(bridge.on_points_collected)

    handlers = IEC104SlaveHandlers(
        snapshot=snapshot,
        data_type_mapping=data_type_mapping,
        reverse_mapping=reverse_mapping,
        bridge=bridge,
        common_address=reporting.common_address,
        batch_size=reporting.batch_size,
    )
    return IEC104SlaveServer(
        host=reporting.host,
        port=reporting.port,
        handlers=handlers,
        common_address=reporting.common_address,
    )


def _create_sink(cfg: SinkConfig) -> SinkPort:
    """按 sink 配置的 ``type`` 创建对应骨架实现。"""
    if cfg.type == "kafka":
        return KafkaSink(cfg)
    if cfg.type == "file":
        return FileSink(cfg)
    if cfg.type == "db":
        return DBSink(cfg)
    raise ConfigError(f"Unknown sink type '{cfg.type}' (available: kafka, file, db)")


def _create_processor(name: str, points_by_device: dict[str, list[PointConfig]]) -> ProcessorPort:
    """按处理器名从注册表创建，并注入点表配置（若处理器支持）。

    首次组装与 Runtime 热重载共享同一路径，确保热重载后处理器
    拿到新的点表（换算/过滤/校验参数），而非回退成空映射。
    """
    processor = processor_registry.create(name)
    if isinstance(processor, PointsConfigurable):
        processor.set_points_config(points_by_device)
    return processor
