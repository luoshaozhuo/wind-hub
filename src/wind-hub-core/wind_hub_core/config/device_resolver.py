"""设备实例与设备型号的 resolved 配置合并。

把 DeviceInstanceConfig 与 DeviceModelConfig 合并成 Runtime 直接消费的
DeviceConfig；实例 endpoint/extensions 覆盖型号 connection_defaults。本模块
只做纯配置转换，不创建协议 Driver 或网络连接。
"""

from __future__ import annotations

from wind_hub_core.config.schema import (
    DeviceConfig,
    DeviceInstanceConfig,
    DeviceInstancesConfig,
    DeviceModelConfig,
    DeviceModelsConfig,
    DevicesConfig,
    InstanceEndpoint,
)
from wind_hub_core.model.device import Endpoint
from wind_hub_core.model.errors import ConfigError


def resolve_devices(
    instances: DeviceInstancesConfig,
    device_models: DeviceModelsConfig,
) -> DevicesConfig:
    """合并全部设备实例与型号定义，返回 resolved :class:`DevicesConfig`。

    ``device_id`` 唯一性已在 :class:`DeviceInstancesConfig` 解析时校验。

    Args:
        instances: 现场设备实例配置。
        device_models: 设备类型/型号定义集。

    Returns:
        Runtime 可直接消费的 resolved DevicesConfig。

    Raises:
        ConfigError: 实例引用未知型号，或端点合并后缺少 ``port``。
    """
    resolved = [
        _resolve_one(inst, _lookup_model(inst.device_id, inst.model, device_models))
        for inst in instances.devices
    ]
    return DevicesConfig(devices=resolved)


def _lookup_model(
    device_id: str, model_id: str, device_models: DeviceModelsConfig
) -> DeviceModelConfig:
    model = device_models.device_models.get(model_id)
    if model is None:
        raise ConfigError(
            f"Device '{device_id}' references unknown model '{model_id}' "
            f"(available: {sorted(device_models.device_models)})"
        )
    return model


def _resolve_one(inst: DeviceInstanceConfig, model: DeviceModelConfig) -> DeviceConfig:
    return DeviceConfig(
        device_id=inst.device_id,
        device_type=model.device_type,
        model=inst.model,
        protocol=model.protocol,
        point_table=model.point_table,
        read_mode=model.read_mode or "sum",
        endpoint=_merge_endpoint(inst.device_id, inst.endpoint, model.connection_defaults),
        device_group=inst.device_group,
        enabled=inst.enabled,
    )


def _merge_endpoint(
    device_id: str,
    endpoint: InstanceEndpoint,
    connection_defaults: dict[str, object],
) -> Endpoint:
    """合并型号连接默认值与实例端点（实例优先）。

    - ``port``：实例 ``endpoint.port`` 优先，否则取
      ``connection_defaults['port']``；两者都没有是配置错误；
    - 其余 ``connection_defaults`` 键并入 ``extensions``，实例
      ``endpoint.extensions`` 同名键覆盖。
    """
    defaults = dict(connection_defaults)
    default_port = defaults.pop("port", None)
    port = endpoint.port if endpoint.port is not None else default_port
    if port is None:
        raise ConfigError(
            f"Device '{device_id}': endpoint has no 'port' and the model provides "
            "no 'connection_defaults.port'"
        )
    extensions = {**defaults, **endpoint.extensions}
    return Endpoint(host=endpoint.host, port=int(port), extensions=extensions)
