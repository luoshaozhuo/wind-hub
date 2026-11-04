"""Data Generator v1 独立验收性能/资源实测脚本（非 pytest 用例）。

真实组件：assemble_gateway 正式 IndustrialGatewayService（7 RPC）+
独立 data_generator 子进程 + 真实 gRPC 客户端；不使用 Mock/替身。

用法（仓库根目录）：
    PYTHONPATH=src python -m tests.data_generator.performance.measure_acceptance

输出各节环境、负载、样本量与 P50/P95/P99，供验收报告直接引用。
"""

# 一次性验收实测脚本；静态门检范围为 src/ 与 tests/，不含本脚本。
# mypy: ignore-errors

from __future__ import annotations

import asyncio
import math
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import conftest as cfg  # noqa: E402
import yaml  # noqa: E402

from contracts import common_pb2 as common  # noqa: E402
from contracts import industrial_gateway_pb2 as gw  # noqa: E402
from contracts import industrial_gateway_pb2_grpc as gw_rpc  # noqa: E402

ONE_FLOAT = {"GW01": [("pitch_angle", "float32", "feedback")]}
VARIABLE = "GW01.pitch_angle"
REPO_ROOT = cfg.REPO_ROOT


def percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return math.nan
    rank = max(1, math.ceil(q * len(sorted_values)))
    return sorted_values[rank - 1]


def report_latency(name: str, samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    result = {
        "samples": float(len(ordered)),
        "p50_ms": percentile(ordered, 0.50) * 1000,
        "p95_ms": percentile(ordered, 0.95) * 1000,
        "p99_ms": percentile(ordered, 0.99) * 1000,
        "max_ms": ordered[-1] * 1000,
    }
    print(
        f"[{name}] n={len(ordered)} "
        f"P50={result['p50_ms']:.3f}ms P95={result['p95_ms']:.3f}ms "
        f"P99={result['p99_ms']:.3f}ms MAX={result['max_ms']:.3f}ms"
    )
    return result


def environment() -> dict[str, Any]:
    info = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "grpcio": __import__("grpc").__version__,
    }
    try:
        meminfo = {
            parts[0].rstrip(":"): parts[1]
            for line in Path("/proc/meminfo").read_text().splitlines()
            if len(parts := line.split()) >= 2
        }
        info["mem_total"] = meminfo.get("MemTotal", "n/a")
    except OSError:
        info["mem_total"] = "n/a"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    info["head"] = head.stdout.strip()
    print("[env]", info)
    return info


def rss_kb(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return -1


def fd_count(pid: int) -> int:
    return len(os.listdir(f"/proc/{pid}/fd"))


LOG_DIR = REPO_ROOT / "tmp-acceptance-logs"
_LOG_SEQ = 0


def start_dg(service_path: Path) -> tuple[str, subprocess.Popen[bytes]]:
    global _LOG_SEQ
    bind = yaml.safe_load(service_path.read_text())["service"]["grpc_bind"]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(cfg.SRC_PATH) + os.pathsep + env.get("PYTHONPATH", "")
    LOG_DIR.mkdir(exist_ok=True)
    _LOG_SEQ += 1
    log_handle = (LOG_DIR / f"dg-{_LOG_SEQ}.log").open("wb")
    # 不能用 PIPE：长跑时日志写满管道会阻塞服务事件循环（测试装置伪影）。
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "data_generator.main",
            "--config",
            str(service_path),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    return bind, process


async def measure_direct_rtt(runs: int = 2000, warmup: int = 200) -> dict[str, float]:
    """A. 客户端→真实 Industrial Gateway UpdateVariables 原生 RTT。"""
    bind = f"127.0.0.1:{cfg.reserve_port()}"
    config = cfg.build_data_gateway_config(endpoint_id="gw-a", data_bind=bind, devices=ONE_FLOAT)
    application = cfg.assemble_gateway(config)
    await application.start()
    samples: list[float] = []
    try:
        import grpc

        async with grpc.aio.insecure_channel(bind) as channel:
            await channel.channel_ready()
            stub = gw_rpc.IndustrialGatewayServiceStub(channel)
            # v1_10：写入需先 ClaimTargets；60s 租约覆盖整个短测量窗口。
            lease = await stub.ClaimTargets(
                gw.ClaimTargetsRequest(
                    gateway_id="gw-a",
                    owner_id="perf-direct-rtt",
                    variable_ids=[VARIABLE],
                    action="ACQUIRE",
                    lease_ms=60_000,
                )
            )
            assert lease.accepted, lease.error_code
            for i in range(warmup + runs):
                update = gw.VariableUpdate(
                    variable_id=VARIABLE,
                    value=common.ScalarValue(double_value=3.0 + (i % 100) * 0.01),
                )
                began = time.perf_counter()
                await stub.UpdateVariables(
                    gw.UpdateVariablesRequest(
                        gateway_id="gw-a",
                        lease_token=lease.lease_token,
                        values=[update],
                    )
                )
                elapsed = time.perf_counter() - began
                if i >= warmup:
                    samples.append(elapsed)
    finally:
        await application.stop()
    return report_latency("direct UpdateVariables RTT", samples)


def build_configs(root: Path, scenarios: dict[str, Any], **kwargs: Any) -> Path:
    cfg._real_tree(root)  # noqa: SLF001
    cfg.write_scenarios(root, scenarios)
    return cfg.write_service_yaml(root, bind=f"127.0.0.1:{cfg.reserve_port()}", **kwargs)


async def measure_dg_pipeline(runs_seconds: float = 12.0) -> dict[str, Any]:
    """B. DG 真实生成→正式更新链路 P99（生产指标，nearest-rank）。"""
    bind = f"127.0.0.1:{cfg.reserve_port()}"
    config = cfg.build_data_gateway_config(endpoint_id="gw-a", data_bind=bind, devices=ONE_FLOAT)
    application = cfg.assemble_gateway(config)
    await application.start()
    root = REPO_ROOT / "tmp-acceptance-perf"
    root.mkdir(exist_ok=True)
    service_path = build_configs(
        root / "pipeline",
        cfg.ship_scenarios_document({"gw-a": bind}, interval=0.02, mappings=[("gw-a", VARIABLE)]),
    )
    dg_bind, process = start_dg(service_path)
    try:
        await cfg.wait_for_port(dg_bind)
        import grpc

        from contracts import common_pb2 as common
        from contracts import common_pb2_grpc as common_rpc
        from contracts import data_generator_pb2 as dg
        from contracts import data_generator_pb2_grpc as dg_rpc

        async with grpc.aio.insecure_channel(dg_bind) as channel:
            await channel.channel_ready()
            common_stub = common_rpc.CommonServiceStub(channel)
            stub = dg_rpc.DataGeneratorServiceStub(channel)
            revision = await common_stub.GetConfigSnapshot(
                common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_RUNTIME)
            )
            started = await stub.StartGeneration(
                dg.StartGenerationRequest(
                    scenario_id="demo-random", expected_revision=revision.revision
                )
            )
            assert started.accepted, started.message
            await asyncio.sleep(runs_seconds)
            await stub.StopGeneration(
                dg.StopGenerationRequest(
                    generation_id=started.generation_id, expected_revision=revision.revision
                )
            )
            await asyncio.sleep(0.3)
            metrics = cfg.metric_map(
                await common_stub.GetMetrics(common.GetMetricsRequest(window_ms=60_000))
            )
        result = {
            "samples": metrics["gateway.gw-a.success_total"],
            "p50_s": metrics.get("gateway.gw-a.rpc_duration_p50", 0.0),
            "p95_s": metrics.get("gateway.gw-a.rpc_duration_p95", 0.0),
            "p99_s": metrics.get("gateway.gw-a.rpc_duration_p99", 0.0),
            "generated": metrics["data_generator.generated_total"],
            "confirmed": metrics["data_generator.confirmed_total"],
            "unknown": metrics["data_generator.unknown_total"],
        }
        print(
            f"[DG pipeline] n={result['samples']:.0f} "
            f"P50={result['p50_s'] * 1000:.3f}ms P95={result['p95_s'] * 1000:.3f}ms "
            f"P99={result['p99_s'] * 1000:.3f}ms confirmed={result['confirmed']:.0f}"
        )
        return result
    finally:
        process.terminate()
        process.wait(timeout=20)
        await application.stop()


def write_large_csv(root: Path, rows: int) -> Path:
    path = root / "history" / "large.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        handle.write("device_id,point_id,source_timestamp,value,quality,record_id\n")
        for i in range(rows):
            clock = i % 86_400
            ts = f"2026-01-01T{clock // 3600:02d}:{clock % 3600 // 60:02d}:{clock % 60:02d}Z"
            handle.write(f"GW02,a02_dm_pitchangle1,{ts},{3.0 + (i % 100) * 0.01:.2f},GOOD,r{i}\n")
    return path


async def measure_index_cost() -> dict[str, Any]:
    """C. 重放整源 SQLite 索引/解码耗时：首批发出前必须完成。"""

    async def run(rows: int) -> tuple[float, int, Path]:
        import tempfile

        gate = asyncio.Event()
        fault_cfg = {"gw-a": [("GW02.pitch", "float32", True)]}
        server, service, bind_fault = await _fault_server(fault_cfg, {"gw-a": {"gate": gate}})
        gate.set()  # 索引测量不挂起；服务即时应答
        tmp_root = Path(tempfile.gettempdir())
        baseline_tmp = {p.name for p in tmp_root.glob("windhub-replay-*")}
        try:
            root = REPO_ROOT / f"tmp-acceptance-index-{rows}"
            cfg._real_tree(root)  # noqa: SLF001
            csv_path = write_large_csv(root, rows)
            doc = cfg.replay_document(
                {"gw-a": bind_fault},
                file=f"history/{csv_path.name}",
                speed=1.0,
                start="2026-01-01T00:00:00Z",
                end="2026-01-02T00:00:00Z",
                mappings=[("gw-a", "GW02.pitch")],
            )
            cfg.write_scenarios(root, doc)
            service_path = cfg.write_service_yaml(
                root, bind=f"127.0.0.1:{cfg.reserve_port()}", output_timeout=2.0
            )
            began = time.perf_counter()
            dg_bind, process = start_dg(service_path)
            try:
                await cfg.wait_for_port(dg_bind)
                await _start_default(dg_bind, "demo-replay")
                deadline = asyncio.get_running_loop().time() + 60
                while not service.update_calls["gw-a"]:
                    if asyncio.get_running_loop().time() > deadline:
                        raise TimeoutError("first replay batch never sent")
                    if process.poll() is not None:
                        logs = sorted(LOG_DIR.glob("dg-*.log"))
                        detail = logs[-1].read_text()[-2000:] if logs else "(no log)"
                        raise RuntimeError(f"dg exited before first batch\n{detail}")
                    await asyncio.sleep(0.02)
                elapsed = time.perf_counter() - began
                # 索引库位于系统临时目录 windhub-replay-*（见 replay.py 的 TemporaryDirectory）。
                index_dirs = [
                    p for p in tmp_root.glob("windhub-replay-*") if p.name not in baseline_tmp
                ]
                index_bytes = sum(
                    f.stat().st_size for d in index_dirs for f in d.rglob("*") if f.is_file()
                )
                return elapsed, index_bytes, csv_path
            finally:
                process.terminate()
                process.wait(timeout=20)
        finally:
            await server.stop(grace=0)

    small = await run(1_000)
    print(f"[replay index] rows=1000 startup_to_first_batch={small[0]:.3f}s")
    large = await run(200_000)
    print(
        f"[replay index] rows=200000 startup_to_first_batch={large[0]:.3f}s "
        f"csv={large[2].stat().st_size / 1e6:.1f}MB sqlite={large[1] / 1e6:.1f}MB"
    )
    return {
        "rows_1k": {"startup_to_first_batch_s": small[0]},
        "rows_200k": {
            "startup_to_first_batch_s": large[0],
            "csv_mb": large[2].stat().st_size / 1e6,
            "sqlite_mb": large[1] / 1e6,
        },
    }


async def _fault_server(catalog: Any, behaviors: Any) -> Any:
    import grpc

    server = grpc.aio.server()
    service = cfg.FaultGatewayService(catalog, behaviors)
    gw_rpc.add_IndustrialGatewayServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return server, service, f"127.0.0.1:{port}"


async def _start_default(bind: str, scenario_id: str) -> str:
    import grpc

    from contracts import common_pb2 as common
    from contracts import common_pb2_grpc as common_rpc
    from contracts import data_generator_pb2 as dg
    from contracts import data_generator_pb2_grpc as dg_rpc

    async with grpc.aio.insecure_channel(bind) as channel:
        await channel.channel_ready()
        common_stub = common_rpc.CommonServiceStub(channel)
        revision = await common_stub.GetConfigSnapshot(
            common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_RUNTIME)
        )
        stub = dg_rpc.DataGeneratorServiceStub(channel)
        started = await stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id=scenario_id, expected_revision=revision.revision)
        )
        assert started.accepted, started.message
        return started.generation_id


async def measure_soak(seconds: float = 30.0) -> dict[str, Any]:
    """D. 高频长时运行资源增长：RSS/FD 前后对比，指标有界。"""
    bind = f"127.0.0.1:{cfg.reserve_port()}"
    config = cfg.build_data_gateway_config(endpoint_id="gw-a", data_bind=bind, devices=ONE_FLOAT)
    application = cfg.assemble_gateway(config)
    await application.start()
    root = REPO_ROOT / "tmp-acceptance-soak"
    service_path = build_configs(
        root,
        cfg.ship_scenarios_document({"gw-a": bind}, interval=0.02, mappings=[("gw-a", VARIABLE)]),
    )
    dg_bind, process = start_dg(service_path)
    try:
        await cfg.wait_for_port(dg_bind)
        await _start_default(dg_bind, "demo-random")
        await asyncio.sleep(5.0)
        rss_early, fd_early = rss_kb(process.pid), fd_count(process.pid)
        await asyncio.sleep(seconds - 5.0)
        rss_late, fd_late = rss_kb(process.pid), fd_count(process.pid)
        import grpc

        from contracts import common_pb2 as common
        from contracts import common_pb2_grpc as common_rpc

        async with grpc.aio.insecure_channel(dg_bind) as channel:
            await channel.channel_ready()
            common_stub = common_rpc.CommonServiceStub(channel)
            metrics = cfg.metric_map(
                await common_stub.GetMetrics(common.GetMetricsRequest(window_ms=60_000))
            )
        delta_mb = (rss_late - rss_early) / 1024
        result = {
            "seconds": seconds,
            "rss_early_mb": rss_early / 1024,
            "rss_late_mb": rss_late / 1024,
            "rss_delta_mb": delta_mb,
            "fd_early": fd_early,
            "fd_late": fd_late,
            "generated": metrics["data_generator.generated_total"],
            "confirmed": metrics["data_generator.confirmed_total"],
        }
        print(
            f"[soak {seconds:.0f}s] RSS {rss_early / 1024:.1f}→{rss_late / 1024:.1f}MB "
            f"(Δ{delta_mb:+.1f}MB) FD {fd_early}→{fd_late} "
            f"confirmed={metrics['data_generator.confirmed_total']:.0f}"
        )
        return result
    finally:
        process.terminate()
        process.wait(timeout=20)
        await application.stop()


async def main() -> None:
    print("=== Data Generator v1 acceptance performance evidence ===")
    environment()
    await measure_direct_rtt()
    await measure_dg_pipeline()
    await measure_index_cost()
    await measure_soak()


if __name__ == "__main__":
    asyncio.run(main())
