"""系统恢复测试专用：构造 v1_21 权威 Sink Service 单 Service 配置根。

新架构下 ``wind-hub-sink-service`` 只接受 ``--config-root`` 与
``--service-file``：gRPC 监听、Bearer 鉴权、NATS、Sink 连接与 Delivery 过滤
全部来自配置根内的 canonical 文档树。本模块复用 Sink Service 自己的
阶段一夹具（:mod:`tests.sink_service._stage1_fixtures` 与
Bearer 环境令牌），保证系统测试不会漂移出生产
校验器接受的布局；只把连接地址、身份与过滤范围参数化为恢复测试所需。
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from tests.sink_service._stage1_fixtures import (
    DELIVERIES_INSTANCE,
    SERVICE_FILE,
    SINKS_INSTANCE,
    service_document,
    template_doc,
    template_path,
    write_tree,
)

#: 恢复测试的固定 Sink/Delivery 身份。
SINK_ID = "redis_sink"
DELIVERY_ID = "realtime_power"
#: 系统测试缺省 Redis 命名空间（等价旧扁平配置的 key_prefix 角色）。
DEFAULT_NAMESPACE = "wind-hub"
#: 与 ``write_functional_config`` 缺省站点及 ``start_collector`` 缺省身份对齐，
#: Delivery source 过滤器必须与此一致，否则批次零匹配被过滤 ACK（不出数）。
DEFAULT_WIND_FARM_ID = "test"
DEFAULT_COLLECTOR_ID = "system-test"
TEST_SINK_CLI_TOKEN = secrets.token_hex(32)
MAINTENANCE_CALLER = {
    "caller_id": "maintenance",
    "token_env": "env:WIND_HUB_SINK_CLI_TOKEN",
    "permissions": ["query", "control", "config"],
}


def latest_value_key(
    namespace: str,
    *,
    wind_farm_id: str = DEFAULT_WIND_FARM_ID,
    collector_id: str = DEFAULT_COLLECTOR_ID,
    device_id: str,
    point_id: str,
) -> str:
    """新架构 Redis 最新值键：``{namespace}:{farm}:{collector}:{device}:{point}``。

    对应 ``sink_service.infrastructure.adapters.outbound.realtime_adapters``
    的键契约；替代已退役的长度前缀键（``tests.support.redis_client.point_key``）。
    """
    return ":".join((namespace, wind_farm_id, collector_id, device_id, point_id))


def write_sink_root(
    root: Path,
    *,
    nats_url: str,
    redis_address: str,
    wind_farm_id: str,
    collector_id: str,
    grpc_port: int,
    namespace: str,
    deployment_id: str,
    devices: list[str] | None = None,
    points: list[str] | None = None,
) -> Path:
    """在 ``root`` 写出一套可直接启动的 canonical Sink Service 配置根。

    只装配 realtime + Redis 链路（无 reliable/Kafka 交付）；空 devices/
    points 在生产引擎中表示该身份下全收（``realtime.py`` accepts 语义）。
    """
    root.mkdir(parents=True, exist_ok=True)
    redis_host, _, redis_port = redis_address.rpartition(":")

    service = service_document(
        port=grpc_port,
        config_root=str(root),
        callers=[dict(MAINTENANCE_CALLER)],
    )
    service["wind_farm_id"] = wind_farm_id
    service["deployment_id"] = deployment_id
    service["nats"]["servers"] = [nats_url]

    redis_template = template_doc("redis_realtime")
    redis_template["connection"] = {"host": redis_host, "port": int(redis_port)}
    redis_template["output"]["namespace"] = namespace

    docs: dict[str, dict[str, Any]] = {
        SERVICE_FILE: service,
        template_path("redis_realtime"): redis_template,
        template_path("realtime_power"): template_doc("realtime_power"),
        SINKS_INSTANCE: {
            "schema_version": 1,
            "file_version": "1.0.0",
            "revision": "system-sinks-r1",
            "sinks": {
                SINK_ID: {
                    "template_id": "redis_realtime",
                    "template_version": "1.0.0",
                    "connection": {"host": redis_host, "port": int(redis_port)},
                    "overrides": {},
                }
            },
        },
        DELIVERIES_INSTANCE: {
            "schema_version": 1,
            "file_version": "1.0.0",
            "revision": "system-deliveries-r1",
            "deliveries": {
                DELIVERY_ID: {
                    "template_id": "realtime_power",
                    "template_version": "1.0.0",
                    "sink_id": SINK_ID,
                    "desired_state": "START",
                    "source": {
                        "wind_farm_id": wind_farm_id,
                        "collector_id": collector_id,
                        "devices": devices or [],
                        "points": points or [],
                    },
                    "overrides": {},
                }
            },
        },
    }
    write_tree(root, docs)
    return root
