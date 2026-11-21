"""Recovery：短周期故障/重载循环的资源稳定性——不得单调泄漏。

与 soak 的长时资源采样的区别：这里针对的是故障注入路径本身
（reconnect 循环、配置收敛循环、Delivery 消费协程重建）——这些路径涉及
连接重建、协议实例替换、acquisition handle 重启、sink consumer 重建，
是句柄泄漏的高发区。循环次数有限（10 次），断言的是「不单调增长」
而非长时平稳。

v2 拓扑：Collector 与 Sink Service 是两个独立进程，因此 RES-02 分别
在两个进程边界采样 FD——Collector 侧通过新版本 task/model 模板 +
devices 文档替换驱动采集句柄重建，Sink 侧通过正式 Delivery 生命周期
（StopDelivery/StartDelivery，mTLS 管理面）驱动 consumer 协程重建
（旧 Sink 级 RestartSink 管理面已随 v0 运行时移除，且不存在安全
RPC，不得回退）。

观测点（全部在进程边界）：
- ``/proc/<pid>/fd`` 采样（含 socket/文件/管道）；
- v2 ListTasks 的设备状态表（每设备唯一 Task，无重复 handle）；
- GetMetrics 的 reconnects 计数（确认故障循环真实发生）。
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import grpc
import pytest
import yaml

from contracts import common_pb2 as common_pbx
from contracts import sink_service_pb2_grpc
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.collector_config_tree import build_collector_tree_from_site
from tests.support.control import (
    get_disk_yaml,
    instance_states,
    metrics_snapshot,
    replace_config,
    replace_template,
    start_task_instance,
)
from tests.support.functional_config import write_functional_config
from tests.support.gateway import reserve_port
from tests.support.process import CollectorProcess, start_sink_service_from_root
from tests.support.recovery_helpers import (
    DEVICE_ID,
    recovery_collector_tree,
    wait_status,
)
from tests.support.sink_harness import DELIVERY_ID, TEST_SINK_CLI_TOKEN, write_sink_root
from tests.support.wait import wait_redis_value, wait_tcp_port, wait_until

pytestmark = [pytest.mark.modbus, pytest.mark.redis, pytest.mark.real_service]

TASK_ID = DEVICE_ID
INSTANCE_ID = DEVICE_ID

CYCLES = 10
#: FD 采样容忍的瞬时波动（日志 flush、metrics 连接等）；泄漏是单调增长，
#: 10 次循环后仍落在基线 +3 以内即不成立。
FD_SLACK = 3
#: 内存采样容忍的瞬时波动（Python 分配器高位页与 GC 窗口）；持续增长
#: 是泄漏，10 轮后仍落在基线 + 每轮 1MB 内即不成立。
MEMORY_SLACK_KB = 10 * 1024
#: 逐轮 CPU 增长因子与绝对余量：后半程均值不得显著高于前半程。
CPU_GROWTH_FACTOR = 1.5
CPU_ABS_SLACK_S = 0.5

#: 身份契约：v2 站点 site_id / 默认 collector_id 必须与 Sink Delivery 的
#: source 过滤器一致，否则批次按零匹配被过滤 ACK（无法出数）。
SITE_ID = "test"
COLLECTOR_ID = "system-test"
#: 新架构 Redis 键契约：{namespace}:{farm}:{collector}:{device}:{point}。
SINK_NAMESPACE = "wind-hub-resources"
REDIS_KEY = ":".join((SINK_NAMESPACE, SITE_ID, COLLECTOR_ID, DEVICE_ID, "rotor_speed"))


def _resource_evidence(root: Path, case_id: str, **payload: Any) -> None:
    repo = Path(__file__).resolve().parents[3]
    entry = {
        "case_id": case_id,
        "status": "PASS",
        "cycles": CYCLES,
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
        "worktree_diff_sha256": hashlib.sha256(
            subprocess.check_output(["git", "diff", "--binary"], cwd=repo)
        ).hexdigest(),
        **payload,
    }
    path = root / f"{case_id}-resource.json"
    path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"RESOURCE_EVIDENCE={path}")


@pytest.fixture
async def sink_factory(tmp_path: Path, nats_service: str, redis_service: str):
    """canonical Sink Service 子进程（--config-root + mTLS Delivery 管理面）。"""
    started: list[CollectorProcess] = []

    async def _start() -> tuple[Path, CollectorProcess]:
        grpc_port = reserve_port()
        root = write_sink_root(
            tmp_path / f"sink-{grpc_port}",
            nats_url=nats_service,
            redis_address=redis_service,
            wind_farm_id=SITE_ID,
            collector_id=COLLECTOR_ID,
            grpc_port=grpc_port,
            namespace=SINK_NAMESPACE,
            deployment_id=f"res-{grpc_port}",
        )
        proc = start_sink_service_from_root(
            root,
            grpc_port=grpc_port,
            log_dir=tmp_path,
            env_extra={"WIND_HUB_SINK_CLI_TOKEN": TEST_SINK_CLI_TOKEN},
        )
        started.append(proc)
        try:
            # 管理面无就绪 RPC；gRPC 端口可连即装配完成，端到端就绪由
            # 后续 Redis 出数轮询保证。
            await wait_tcp_port("127.0.0.1", grpc_port)
            if not proc.is_running():
                raise RuntimeError(f"sink_service exited early; see {proc.log_path}")
        except BaseException:
            proc.kill_tree()
            proc.close_log()
            started.remove(proc)
            raise
        return root, proc

    yield _start

    for proc in started:
        try:
            if proc.is_running():
                proc.terminate()
        except BaseException:
            proc.kill_tree()
        finally:
            proc.close_log()


def _flat_config(base: Path, device_port: int, redis_address: str, nats_url: str) -> Path:
    return write_functional_config(
        base,
        device_port,
        nats_url=nats_url,
        tasks={
            TASK_ID: {
                "device_group_id": DEVICE_ID,
                "point_group": "telemetry",
                "interval": 0.2,
            }
        },
    )


async def _wait_data(address: str, *, after: str | None = None) -> str:
    value = await wait_redis_value(
        address,
        REDIS_KEY,
        match=(lambda payload: str(payload["sample_time"]) > after) if after else None,
    )
    return str(value["sample_time"])


async def _single_running_instance(proc: CollectorProcess) -> None:
    """断言本用例设备唯一且处于 RUNNING 生命周期状态（轮询等待恢复收敛）。"""

    async def _probe() -> bool | None:
        states = await instance_states(proc.grpc_target)
        if states is None:
            return None
        running = states.get(f"{TASK_ID}:{INSTANCE_ID}") == "RUNNING"
        if running and len(states) == 1:
            return True
        return None

    await wait_until(_probe, timeout=20.0, description="single RUNNING instance after recovery")


async def _delivery_restart_cycle(root: Path, grpc_port: int) -> None:
    """正式 Delivery 生命周期重建 consumer：Stop → Start（Bearer 管理面）。

    旧 Sink 级 RestartSink 管理面不存在安全 RPC；S-7 裁定只能以
    StopDelivery/StartDelivery 组合表达同等 consumer 协程重建。
    """
    metadata = (("authorization", f"Bearer {TEST_SINK_CLI_TOKEN}"),)
    async with grpc.aio.insecure_channel(f"127.0.0.1:{grpc_port}") as channel:
        stub = sink_service_pb2_grpc.SinkServiceStub(channel)
        detail = await stub.GetDelivery(common_pbx.ObjectId(id=DELIVERY_ID), metadata=metadata)
        stopped = await stub.StopDelivery(
            common_pbx.OperationRequest(id=DELIVERY_ID, expected_revision=detail.delivery.revision),
            metadata=metadata,
        )
        assert stopped.accepted, stopped.message
        detail = await stub.GetDelivery(common_pbx.ObjectId(id=DELIVERY_ID))
        restarted = await stub.StartDelivery(
            common_pbx.OperationRequest(id=DELIVERY_ID, expected_revision=detail.delivery.revision)
        )
        assert restarted.accepted, restarted.message


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _model_ref(v2_tree: Path) -> tuple[str, str, str, str]:
    """返回 (model_id, model_version, task_id, task_version) 当前引用链。

    版本化模板为扁平命名：``templates/<category>/<prefix>_<id>_v<ver>.yaml``。
    """
    devices = _load(v2_tree / "instances" / "devices.yaml")
    model = devices["devices"][DEVICE_ID]["model"]
    model_path = (
        v2_tree
        / "templates"
        / "device_models"
        / f"device_model_{model['id']}_v{model['version']}.yaml"
    )
    model_doc = _load(model_path)
    return model["id"], model["version"], model_doc["task"]["id"], model_doc["task"]["version"]


async def _bump_task_interval(
    proc: CollectorProcess,
    v2_tree: Path,
    *,
    cycle: int,
    interval: float,
) -> None:
    """新版本 task + model 模板，再把 devices 文档引用切到新版本。

    v2 模板不可变：节拍变更必须发布新版本，不能覆盖旧版本文件。
    """
    model_id, _, task_template_id, task_version = _model_ref(v2_tree)
    task_path = v2_tree / "templates" / "tasks" / f"task_{task_template_id}_v{task_version}.yaml"
    task_doc = _load(task_path)
    new_task_version = f"1.0.{cycle + 1}"
    new_task_doc = copy.deepcopy(task_doc)
    new_task_doc["template_version"] = new_task_version
    new_task_doc["file_version"] = new_task_version
    new_task_doc["interval"] = interval
    await replace_template(
        proc.grpc_target, "tasks", task_template_id, new_task_version, new_task_doc
    )

    model_version = _model_ref(v2_tree)[1]
    model_path = (
        v2_tree / "templates" / "device_models" / f"device_model_{model_id}_v{model_version}.yaml"
    )
    model_doc = _load(model_path)
    new_model_version = f"1.0.{cycle + 1}"
    new_model_doc = copy.deepcopy(model_doc)
    new_model_doc["template_version"] = new_model_version
    new_model_doc["file_version"] = new_model_version
    new_model_doc["task"] = {"id": task_template_id, "version": new_task_version}
    await replace_template(
        proc.grpc_target, "device_models", model_id, new_model_version, new_model_doc
    )

    # Use the current committed snapshot: startup YAML predates the durable START
    # intent written by StartTask, and replacing it would silently reset to STOP.
    devices_doc = await get_disk_yaml(proc.grpc_target, "instances/devices.yaml")
    assert devices_doc["devices"][DEVICE_ID]["overrides"]["task"]["desired_state"] == "START"
    devices_doc["devices"][DEVICE_ID]["model"]["version"] = new_model_version
    result = await replace_config(
        proc.grpc_target,
        yaml.safe_dump(devices_doc, allow_unicode=True, sort_keys=False),
    )
    assert result.success, f"cycle {cycle} devices replace failed: {list(result.errors)}"


class TestReconnectFlapStability:
    async def test_ten_disconnect_cycles_do_not_leak(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        """RES-01：10 次断连/恢复循环后实例唯一、FD 不增长。"""
        await sink_factory()
        flat_dir = _flat_config(tmp_path / "cfg", modbus_server.port, redis_service, nats_service)
        v2_tree = build_collector_tree_from_site(tmp_path / "v2", flat_dir, nats_url=nats_service)
        proc: CollectorProcess = await collector_factory(v2_tree)
        await start_task_instance(proc.grpc_target, instance_id=INSTANCE_ID)
        await _wait_data(redis_service)

        baseline_reconnects = int(
            (await metrics_snapshot(proc.grpc_target))["device_reconnects"].get(DEVICE_ID, 0)
        )
        baseline_fd = proc.fd_count()
        baseline_rss = proc.memory_rss_kb()
        collector_cpu = proc.cpu_seconds()
        per_cycle_cpu: list[float] = []

        for cycle in range(CYCLES):
            await modbus_server.stop()
            await wait_status(
                proc,
                lambda p: p["device_connected"] == 0,
                description=f"cycle {cycle}: disconnected",
            )
            await modbus_server.start()
            await wait_status(
                proc,
                lambda p: p["device_connected"] == 1,
                description=f"cycle {cycle}: reconnected",
            )
            await _single_running_instance(proc)
            current_cpu = proc.cpu_seconds()
            per_cycle_cpu.append(current_cpu - collector_cpu)
            collector_cpu = current_cpu

        # 数据面在最后一次恢复后继续出数。
        previous = await _wait_data(redis_service)
        await _wait_data(redis_service, after=previous)

        reconnects = int(
            (await metrics_snapshot(proc.grpc_target))["device_reconnects"].get(DEVICE_ID, 0)
        )
        assert (
            reconnects - baseline_reconnects >= CYCLES
        ), f"expected >= {CYCLES} reconnects, got {reconnects - baseline_reconnects}"
        final_fd = proc.fd_count()
        assert (
            final_fd <= baseline_fd + FD_SLACK
        ), f"fd count grew from {baseline_fd} to {final_fd} over {CYCLES} flap cycles"
        final_rss = proc.memory_rss_kb()
        assert (
            final_rss <= baseline_rss + MEMORY_SLACK_KB
        ), f"rss grew from {baseline_rss}KB to {final_rss}KB over {CYCLES} flap cycles"
        half = CYCLES // 2
        early = sum(per_cycle_cpu[:half]) / half
        late = sum(per_cycle_cpu[half:]) / (CYCLES - half)
        assert late <= early * CPU_GROWTH_FACTOR + CPU_ABS_SLACK_S, (
            f"per-cycle reconnect CPU grew: first-half {early:.2f}s/cycle vs "
            f"second-half {late:.2f}s/cycle ({per_cycle_cpu})"
        )
        _resource_evidence(
            tmp_path,
            "RES-01",
            reconnects=reconnects - baseline_reconnects,
            collector={
                "baseline_fd": baseline_fd,
                "final_fd": final_fd,
                "fd_slack": FD_SLACK,
                "baseline_rss_kb": baseline_rss,
                "final_rss_kb": final_rss,
                "rss_slack_kb": MEMORY_SLACK_KB,
                "per_cycle_cpu_seconds": per_cycle_cpu,
                "early_cpu_mean_seconds": early,
                "late_cpu_mean_seconds": late,
                "cpu_growth_factor_limit": CPU_GROWTH_FACTOR,
                "cpu_absolute_slack_seconds": CPU_ABS_SLACK_S,
            },
        )


class TestReloadStability:
    async def test_ten_reloads_do_not_leak(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        """RES-02：10 轮配置收敛（Collector 采集句柄重建）+ Delivery
        生命周期重建（Sink consumer 协程重建）后，两侧实例/句柄唯一、FD 不增长。"""
        sink_root, sink = await sink_factory()
        sink_port = int(sink.grpc_target.rpartition(":")[2])
        flat_dir = _flat_config(tmp_path / "cfg", modbus_server.port, redis_service, nats_service)
        v2_tree = recovery_collector_tree(flat_dir, tmp_path / "v2", nats_url=nats_service)
        # 模板新版本发布须显式开启发布闸门（生产 fail-closed 开关）。
        proc: CollectorProcess = await collector_factory(
            v2_tree, env_extra={"WIND_HUB_COLLECTOR_TEMPLATE_PUBLISH": "enabled"}
        )
        await start_task_instance(proc.grpc_target, instance_id=INSTANCE_ID)
        await _wait_data(redis_service)
        collector_fd = proc.fd_count()
        sink_fd = sink.fd_count()
        collector_rss = proc.memory_rss_kb()
        sink_rss = sink.memory_rss_kb()
        per_cycle_cpu: list[float] = []
        collector_cpu = proc.cpu_seconds()
        per_cycle_sink_cpu: list[float] = []
        sink_cpu = sink.cpu_seconds()

        for cycle in range(CYCLES):
            interval = 0.2 if cycle % 2 == 0 else 0.3

            # Collector 侧：新版本 task/model + devices 引用切换，
            # 强制采集句柄按新节拍重建。
            await _bump_task_interval(proc, v2_tree, cycle=cycle, interval=interval)

            # Sink 侧：正式 Delivery Stop→Start 重建 consumer 协程整体替换。
            await _delivery_restart_cycle(sink_root, sink_port)

            await _single_running_instance(proc)
            per_cycle_cpu.append(proc.cpu_seconds() - collector_cpu)
            collector_cpu = proc.cpu_seconds()
            current_sink_cpu = sink.cpu_seconds()
            per_cycle_sink_cpu.append(current_sink_cpu - sink_cpu)
            sink_cpu = current_sink_cpu

        # 采集在最后一轮配置下继续出数。
        previous = await _wait_data(redis_service)
        await _wait_data(redis_service, after=previous)

        final_collector_fd = proc.fd_count()
        assert final_collector_fd <= collector_fd + FD_SLACK, (
            f"collector fd grew from {collector_fd} to {final_collector_fd} "
            f"over {CYCLES} reloads"
        )
        final_sink_fd = sink.fd_count()
        assert (
            final_sink_fd <= sink_fd + FD_SLACK
        ), f"sink fd grew from {sink_fd} to {final_sink_fd} over {CYCLES} delivery cycles"

        final_collector_rss = proc.memory_rss_kb()
        assert final_collector_rss <= collector_rss + MEMORY_SLACK_KB, (
            f"collector rss grew from {collector_rss}KB to {final_collector_rss}KB "
            f"over {CYCLES} reloads"
        )
        final_sink_rss = sink.memory_rss_kb()
        assert (
            final_sink_rss <= sink_rss + MEMORY_SLACK_KB
        ), f"sink rss grew from {sink_rss}KB to {final_sink_rss}KB over {CYCLES} delivery cycles"

        # CPU：每轮收敛为有界重复工作——前半程均值与后半程均值对比，
        # 后半程显著高于前半程即重复工作在逐轮放大（持续增长即异常）；
        # 单轮收敛瞬时接近单核属正常（快照哈希 + 句柄重建 + 首拍补采）。
        half = CYCLES // 2
        early = sum(per_cycle_cpu[:half]) / half
        late = sum(per_cycle_cpu[half:]) / (CYCLES - half)
        assert late <= early * CPU_GROWTH_FACTOR + CPU_ABS_SLACK_S, (
            f"per-cycle collector CPU grew: first-half {early:.2f}s/cycle vs "
            f"second-half {late:.2f}s/cycle ({per_cycle_cpu})"
        )
        _resource_evidence(
            tmp_path,
            "RES-02",
            collector={
                "baseline_fd": collector_fd,
                "final_fd": final_collector_fd,
                "fd_slack": FD_SLACK,
                "baseline_rss_kb": collector_rss,
                "final_rss_kb": final_collector_rss,
                "rss_slack_kb": MEMORY_SLACK_KB,
                "per_cycle_cpu_seconds": per_cycle_cpu,
                "early_cpu_mean_seconds": early,
                "late_cpu_mean_seconds": late,
                "cpu_growth_factor_limit": CPU_GROWTH_FACTOR,
                "cpu_absolute_slack_seconds": CPU_ABS_SLACK_S,
            },
            sink={
                "baseline_fd": sink_fd,
                "final_fd": final_sink_fd,
                "fd_slack": FD_SLACK,
                "baseline_rss_kb": sink_rss,
                "final_rss_kb": final_sink_rss,
                "rss_slack_kb": MEMORY_SLACK_KB,
                "per_cycle_cpu_seconds": per_cycle_sink_cpu,
            },
        )
