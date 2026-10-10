"""风电场 Site 领域约束。"""

from dataclasses import FrozenInstanceError

import pytest

from core.domain import ConnectionEndpoint, Device, DeviceGroupId, DeviceId, DeviceModelId, Site


def test_site_owns_devices() -> None:
    device = Device(
        device_id=DeviceId("wtg-001"),
        device_model_id=DeviceModelId("wtg"),
        endpoint=ConnectionEndpoint("127.0.0.1", 502),
        device_group_ids=(DeviceGroupId("north"),),
    )
    site = Site("wind-farm", "北区风电场", {device.device_id: device})
    assert site.name == "北区风电场"
    assert site.devices[device.device_id] is device
    with pytest.raises(TypeError):
        site.devices[DeviceId("other")] = device


def test_site_rejects_invalid_identity_and_device_index() -> None:
    with pytest.raises(ValueError, match="site_id"):
        Site("", "wind-farm", {})
    with pytest.raises(ValueError, match="site name"):
        Site("wind-farm", " ", {})
    device = Device(
        DeviceId("d"), DeviceModelId("m"),
        ConnectionEndpoint("127.0.0.1"),
        device_group_ids=(DeviceGroupId("g"),),
    )
    with pytest.raises(ValueError, match="device key"):
        Site("s", "site", {DeviceId("wrong"): device})
