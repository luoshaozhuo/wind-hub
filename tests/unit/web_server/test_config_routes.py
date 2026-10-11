"""所有副作用 API 的默认关闭和配置事务透传检查。"""

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from web_server.app import create_app, get_gateway


def test_all_mutating_routes_reject_unconfigured_credentials() -> None:
    app = create_app()
    with TestClient(app) as client:
        for route in app.routes:
            methods = getattr(route, "methods", set())
            path = getattr(route, "path", "")
            if (
                path.startswith("/api/v1/")
                and "POST" in methods
                and not path.endswith("/read-many")
            ):
                response = client.post(path.replace("{task_id}", "t1")
                    .replace("{instance_id}", "i1").replace("{name}", "csv")
                    .replace("{device_id}", "d1"), json={})
                assert response.status_code == 503, path


def test_prepare_is_target_specific_not_false_atomic_commit() -> None:
    gateway = AsyncMock()
    gateway.collector_prepare_config.return_value = {
        "success": True, "revision_id": "r1"
    }
    app = create_app(write_token="secret")
    app.dependency_overrides[get_gateway] = lambda: gateway
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/collector/config/prepare",
            headers={"X-API-Key": "secret"},
            json={"revision_id": "r1", "config_hash": "sha"},
        )
    assert response.status_code == 200
    gateway.collector_prepare_config.assert_awaited_once_with("r1", "sha")
    gateway.commander_prepare_config.assert_not_awaited()
