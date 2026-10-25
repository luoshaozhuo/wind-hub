"""Settings 页面只能修改 Core 接受的 system.yaml 字段。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from web_server.application.config.settings import SettingsService, SettingsUpdate


def _service() -> tuple[SettingsService, AsyncMock]:
    config = SimpleNamespace(
        current_config=SimpleNamespace(
            system=SimpleNamespace(ads=None),
            site=SimpleNamespace(site_id="site-1", name="Wind Farm"),
        )
    )
    admin = SimpleNamespace(mutate_yaml_files=AsyncMock())
    return SettingsService(
        config, admin, api_host="127.0.0.1", api_port=18080
    ), admin.mutate_yaml_files


def _request(**overrides: object) -> SettingsUpdate:
    fields = {
        "site_id": "site-2",
        "site_name": "New Farm",
        "api_enabled": True,
        "api_host": "127.0.0.1",
        "api_port": 18080,
        "ads_local_ip": None,
        "ads_local_ams_net_id": None,
    }
    fields.update(overrides)
    return SettingsUpdate.model_validate(fields)


def test_settings_reports_runtime_listener() -> None:
    service, _ = _service()
    snapshot = service.get()
    assert snapshot.api_host == "127.0.0.1"
    assert snapshot.api_port == 18080


@pytest.mark.asyncio
async def test_settings_never_writes_interfaces_api() -> None:
    service, apply = _service()
    await service.update(_request())
    documents = {"system.yaml": {"site": {"site_id": "old"}, "runtime": {}}}
    mutator = apply.call_args.args[1]
    mutator(documents)
    assert "interfaces" not in documents["system.yaml"]
    assert documents["system.yaml"]["site"]["site_id"] == "site-2"
    assert documents["system.yaml"]["runtime"] == {}


@pytest.mark.asyncio
async def test_settings_rejects_listener_mutation() -> None:
    service, apply = _service()
    with pytest.raises(ValueError, match="process deployment"):
        await service.update(_request(api_port=8081))
    apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_settings_identity_save_preserves_ads_timeout_fields() -> None:
    service, apply = _service()
    await service.update(_request(ads_local_ip="127.0.0.1", ads_local_ams_net_id="127.0.0.1.1.1"))
    documents = {
        "system.yaml": {
            "site": {"site_id": "old"},
            "ads": {
                "local_ams_net_id": "127.0.0.1.1.1",
                "local_ip": "127.0.0.1",
                "request_timeout_ms": 333,
                "connect_timeout_ms": 444,
                "close_timeout_ms": 555,
            },
        }
    }
    apply.call_args.args[1](documents)
    ads = documents["system.yaml"]["ads"]
    assert ads["local_ip"] == "127.0.0.1"
    assert ads["local_ams_net_id"] == "127.0.0.1.1.1"
    # Web Settings 不认识超时字段，但保存身份时必须原样保留。
    assert (
        ads["request_timeout_ms"],
        ads["connect_timeout_ms"],
        ads["close_timeout_ms"],
    ) == (333, 444, 555)


@pytest.mark.asyncio
async def test_settings_rejects_incomplete_ads_identity() -> None:
    service, apply = _service()
    await service.update(_request(ads_local_ip="127.0.0.1"))
    documents = {"system.yaml": {"site": {}}}
    with pytest.raises(ValueError, match="both IP and AMS Net ID"):
        apply.call_args.args[1](documents)
