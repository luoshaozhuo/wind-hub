"""ADS 远程路由注册凭据：解析默认值、型号继承、设备覆盖与隔离。"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

from core.application.settings import ADSLocalConfig
from core.domain import ConnectionEndpoint
from core.infrastructure.config import YamlConfigAdapter
from core.infrastructure.config.effective import resolve_effective_device
from core.infrastructure.protocol.ads.config import parse_ads_config

TEMPLATE_DIR = Path(__file__).resolve().parents[4] / "configs" / "template"


def test_ads_credentials_defaults() -> None:
    config = parse_ads_config(ConnectionEndpoint(host="192.0.2.1"), {})
    assert config.username == "Administrator"
    assert config.password == ""
    # 密码不出现在 repr 中。
    assert "password" not in repr(config)


def test_ads_local_config_has_no_remote_credentials() -> None:
    names = {field.name for field in fields(ADSLocalConfig)}
    assert names == {"local_ams_net_id", "local_ip"}


def _effective_ads_options(snapshot, device_id: str) -> dict:
    from core.domain import DeviceId

    device = snapshot.site.devices[DeviceId(device_id)]
    model = snapshot.device_models[device.device_model_id]
    _, options = resolve_effective_device(
        device,
        model,
        "ads",
        snapshot.protocol_options_by_device[device.device_id],
    )
    return options


def test_ads_model_credentials_inherited_by_device() -> None:
    snapshot = YamlConfigAdapter(TEMPLATE_DIR).load()
    options = _effective_ads_options(snapshot, "ads-001")
    config = parse_ads_config(ConnectionEndpoint(host="127.0.0.21"), options)
    assert config.username == "Administrator"
    assert config.password == ""
    assert config.target_net_id == "127.0.0.21.1.1"


def test_ads_device_overrides_username() -> None:
    snapshot = YamlConfigAdapter(TEMPLATE_DIR).load()
    options = _effective_ads_options(snapshot, "ads-010")
    config = parse_ads_config(ConnectionEndpoint(host="127.0.0.30"), options)
    assert config.username == "RouteAdmin"
    # 未覆盖的密码继续继承型号默认值。
    assert config.password == ""


def test_ads_device_overrides_password() -> None:
    config = parse_ads_config(
        ConnectionEndpoint(host="192.0.2.1"),
        {"username": "user-a", "password": "device-secret"},
    )
    assert (config.username, config.password) == ("user-a", "device-secret")


def test_ads_devices_with_different_credentials_do_not_pollute() -> None:
    first = parse_ads_config(
        ConnectionEndpoint(host="192.0.2.1"),
        {"username": "user-a", "password": "secret-a"},
    )
    second = parse_ads_config(
        ConnectionEndpoint(host="192.0.2.2"),
        {"username": "user-b", "password": "secret-b"},
    )
    assert (first.username, first.password) == ("user-a", "secret-a")
    assert (second.username, second.password) == ("user-b", "secret-b")


def test_ads_empty_password_is_allowed() -> None:
    config = parse_ads_config(ConnectionEndpoint(host="192.0.2.1"), {"password": ""})
    assert config.password == ""
