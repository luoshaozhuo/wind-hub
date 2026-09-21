"""Integration test — Web API wired to a real assembled runtime.

验证对象：``build_api`` + ``AppContext`` + 真实 :class:`AssembledRuntime`
（来自 ``assembly.assemble``）组成的完整链路——``/metrics`` 渲染 Prometheus
文本、``/routes/explain`` 与 ``/config/reload`` 走真实服务、缺失服务/上下文时
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
            "scheduler": {"default_interval": 1.0},
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
                            "address": {"type": "holding_register", "register": 30001},
                            "data_type": "float32",
                        },
                    ],
                },
            },
        },
    )
    _write_yaml(
        base,
        "routing.yaml",
        {
            "rules": [
                {
                    "name": "default",
                    "match": {"all": True},
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
        set_context(
            AppContext(
                config=rt.config,
                route_query=rt.route_query,
                runtime=rt.runtime,
            )
        )
        with TestClient(build_api()) as client:
            resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        assert "wind_hub_devices_total 1.0" in resp.text
        assert "wind_hub_sinks_total 1.0" in resp.text
        assert "wind_hub_points_collected_total" in resp.text
    clear_context()


def test_route_explain_uses_real_router() -> None:
    with tempfile.TemporaryDirectory() as td:
        rt = _assemble(Path(td))
        set_context(AppContext(route_query=rt.route_query, runtime=rt.runtime))
        with TestClient(build_api()) as client:
            resp = client.get(
                "/routes/explain", params={"device_id": "d1", "point_id": "rotor.speed"}
            )
        assert resp.status_code == 200
        assert resp.json()["targets"] == ["archive"]
    clear_context()


def test_config_reload_runs_real_config_service() -> None:
    with tempfile.TemporaryDirectory() as td:
        rt = _assemble(Path(td))
        set_context(AppContext(config=rt.config, runtime=rt.runtime))
        with TestClient(build_api()) as client:
            resp = client.post("/config/reload")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
    clear_context()


def test_health_returns_503_when_query_service_missing() -> None:
    """查询服务未接入上下文时，``/health`` 应 503 而非崩溃。"""
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
