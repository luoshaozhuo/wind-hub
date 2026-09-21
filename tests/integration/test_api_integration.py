"""Integration test — Web API wired to a real assembled runtime.

验证对象：``build_api`` + ``AppContext`` + 真实 :class:`AssembledRuntime`
（来自 ``assembly.assemble``）组成的完整链路——``/metrics`` 渲染 Prometheus
文本、``/tasks`` 系列端点与 ``/config/reload`` 走真实服务、缺失服务/上下文时
的 503 兜底。不真实监听端口，用 ``fastapi.testclient`` 走 in-process 请求。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from wind_hub.adapter.inbound.webapi.app import build_api
from wind_hub.application.app_context import AppContext, clear_context, set_context
from wind_hub.assembly import assemble


@pytest.fixture(autouse=True)
def _clean_context() -> None:
    clear_context()
    yield
    clear_context()


def _write_yaml(base: Path, name: str, data: dict) -> None:
    (base / name).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _write_minimal_config(base: Path) -> None:
    _write_yaml(
        base,
        "system.yaml",
        {
            "runtime": {},
            "pipeline": {"processors": ["unit_convert"]},
            "sinks": [
                {"name": "archive", "type": "file", "params": {"path": "/tmp/x.csv"}},
            ],
        },
    )
    _write_yaml(
        base,
        "devices.yaml",
        {
            "devices": [
                {
                    "device_id": "d1",
                    "protocol": "modbus",
                    "point_table": "wtg",
                    "endpoint": {"host": "10.0.0.1", "port": 502, "extensions": {"unit_id": 1}},
                },
            ],
        },
    )
    _write_yaml(
        base,
        "points.yaml",
        {
            "point_tables": {
                "wtg": {
                    "points": [
                        {
                            "point_id": "rotor.speed",
                            "point_groups": ["telemetry"],
                            "address": {"register_type": "holding", "address": 100},
                            "data_type": "float32",
                        },
                    ],
                },
            },
        },
    )
    _write_yaml(
        base,
        "tasks.yaml",
        {
            "tasks": [
                {
                    "task_id": "d1-telemetry",
                    "device": "d1",
                    "point_group": "telemetry",
                    "interval": 1.0,
                    "targets": [{"sink": "archive"}],
                },
            ],
        },
    )


def _assemble(tmp: Path):
    _write_minimal_config(tmp)
    return assemble(tmp)


def test_metrics_renders_prometheus_text() -> None:
    with tempfile.TemporaryDirectory() as td:
        rt = _assemble(Path(td))
        set_context(AppContext(config=rt.config, runtime=rt.runtime))
        with TestClient(build_api()) as client:
            resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        assert "wind_hub_devices_total 1.0" in resp.text
        assert "wind_hub_sinks_total 1.0" in resp.text
        assert "wind_hub_points_collected_total" in resp.text
    clear_context()


def test_tasks_endpoints_use_real_task_usecase() -> None:
    """``/tasks`` 系列端点走真实 TaskUseCase：定义、实例展开与 start/stop。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        rt = _assemble(base)
        set_context(AppContext(config=rt.config, tasks=rt.tasks, runtime=rt.runtime))
        with TestClient(build_api()) as client:
            resp = client.get("/tasks")
            assert resp.status_code == 200
            tasks = resp.json()
            assert [t["task_id"] for t in tasks] == ["d1-telemetry"]
            assert tasks[0]["targets"] == ["archive"]

            # 实例只在 Runtime.start / 热重载时展开——纯 assemble 后尚无实例。
            resp = client.get("/tasks/instances")
            assert resp.status_code == 200
            assert resp.json() == []

            # 修改 Task interval 制造真实 diff，经 /config/reload 触发实例展开
            # （无 diff 的 reload 会短路，不触碰运行时）。
            tasks_path = base / "tasks.yaml"
            task_data = yaml.safe_load(tasks_path.read_text(encoding="utf-8"))
            task_data["tasks"][0]["interval"] = 2.0
            tasks_path.write_text(yaml.safe_dump(task_data, sort_keys=False), encoding="utf-8")
            resp = client.post("/config/reload")
            assert resp.status_code == 200
            assert resp.json()["success"] is True
            assert resp.json()["tasks_updated"] == ["d1-telemetry"]

            resp = client.get("/tasks/instances")
            assert resp.status_code == 200
            instances = resp.json()
            assert [i["instance_id"] for i in instances] == ["d1-telemetry:d1"]
            assert instances[0]["state"] == "stopped"

            resp = client.get("/tasks/instances/d1-telemetry:d1")
            assert resp.status_code == 200
            assert resp.json()["device_id"] == "d1"

            # 未 start_runtime 的装配上 start/stop 只翻转生命周期状态（幂等）。
            resp = client.post("/tasks/instances/d1-telemetry:d1/start")
            assert resp.status_code == 200
            assert resp.json()["state"] == "running"
            resp = client.post("/tasks/instances/d1-telemetry:d1/stop")
            assert resp.status_code == 200
            assert resp.json()["state"] == "stopped"

            resp = client.post("/tasks/start-all")
            assert resp.status_code == 200
            assert resp.json() == {"total": 1, "changed": 1, "unchanged": 0}

            # 收尾：停掉实例，避免 polling 协程泄漏到测试之外。
            resp = client.post("/tasks/stop-all")
            assert resp.status_code == 200
            assert resp.json() == {"total": 1, "changed": 1, "unchanged": 0}

            resp = client.get("/tasks/instances/unknown:d9")
            assert resp.status_code == 404
    clear_context()


def test_config_reload_runs_real_config_usecase() -> None:
    """无配置变化时 reload 成功，diff 各字段为空（新响应无 routing 字段）。"""
    with tempfile.TemporaryDirectory() as td:
        rt = _assemble(Path(td))
        set_context(AppContext(config=rt.config, runtime=rt.runtime))
        with TestClient(build_api()) as client:
            resp = client.post("/config/reload")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["tasks_added"] == []
        assert body["tasks_removed"] == []
        assert body["tasks_updated"] == []
        assert "routing_rebuilt" not in body
    clear_context()


def test_config_reload_reports_tasks_diff() -> None:
    """新增 Task 后 reload 在 ``tasks_added`` 中如实上报。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        rt = _assemble(base)
        set_context(AppContext(config=rt.config, runtime=rt.runtime))

        tasks_path = base / "tasks.yaml"
        tasks = yaml.safe_load(tasks_path.read_text(encoding="utf-8"))
        tasks["tasks"].append(
            {
                "task_id": "d1-telemetry-fast",
                "device": "d1",
                "point_group": "telemetry",
                "interval": 0.5,
                "targets": [{"sink": "archive"}],
            }
        )
        tasks_path.write_text(yaml.safe_dump(tasks, sort_keys=False), encoding="utf-8")

        with TestClient(build_api()) as client:
            resp = client.post("/config/reload")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["tasks_added"] == ["d1-telemetry-fast"]
    clear_context()


def test_health_returns_503_when_query_usecase_missing() -> None:
    """查询用例未接入上下文时，``/health`` 应 503 而非崩溃。"""
    with tempfile.TemporaryDirectory() as td:
        rt = _assemble(Path(td))
        set_context(AppContext(runtime=rt.runtime))
        with TestClient(build_api()) as client:
            resp = client.get("/health")
        assert resp.status_code == 503
    clear_context()


def test_metrics_returns_503_without_context() -> None:
    clear_context()
    # 不用 `with TestClient(...)`：lifespan 会在启动时因上下文缺失而抛错。
    client = TestClient(build_api())
    resp = client.get("/metrics")
    assert resp.status_code == 503
