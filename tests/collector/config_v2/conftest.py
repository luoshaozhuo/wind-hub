"""配置体系验收夹具：三路径临时配置树、已加载文档与真实 grpc.aio 栈。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import grpc
import pytest

from collector.assembly import assemble_collector
from collector.infrastructure.grpc.v2_server import (
    CollectorConfigServiceV2,
    CollectorMetricsServiceV2,
    CollectorRuntimeServiceV2,
    CollectorTaskServiceV2,
)
from contracts import collector_v2_pb2_grpc as pb_grpc
from tests.collector.config_v2.config_tree import ConfigTree, make_config_tree

ADMIN_TOKEN = "acceptance-token"
ADMIN_METADATA = (("x-windhub-admin-token", ADMIN_TOKEN),)


@pytest.fixture
def config_tree(tmp_path: Path) -> ConfigTree:
    return make_config_tree(tmp_path)


@pytest.fixture
def documents(config_tree: ConfigTree):
    from core.infrastructure.config.collector_documents import CollectorDocumentLoader

    return CollectorDocumentLoader(config_tree.paths).load()


@pytest.fixture
async def grpc_stack(
    config_tree: ConfigTree, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[dict]:
    """真实 assemble_collector + grpc.aio in-process server，四个服务全注册。

    不调用 app.start()：持久化、校验、事务与错误映射全部真实；运行时收敛
    成功路径由第三/六轮真实链路覆盖（未启动时 reload 确定性失败
    "Collector runtime is not running"）。
    """
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ID", "acceptance-collector")
    app = assemble_collector(
        config_tree.service,
        config_tree.instances,
        config_tree.templates,
        collector_id="acceptance-collector",
    )
    server = grpc.aio.server()
    pb_grpc.add_CollectorRuntimeServiceServicer_to_server(CollectorRuntimeServiceV2(app), server)
    pb_grpc.add_CollectorTaskServiceServicer_to_server(CollectorTaskServiceV2(app), server)
    pb_grpc.add_CollectorConfigServiceServicer_to_server(CollectorConfigServiceV2(app), server)
    pb_grpc.add_CollectorMetricsServiceServicer_to_server(CollectorMetricsServiceV2(app), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield {
            "app": app,
            "tree": config_tree,
            "config": pb_grpc.CollectorConfigServiceStub(channel),
            "runtime": pb_grpc.CollectorRuntimeServiceStub(channel),
            "tasks": pb_grpc.CollectorTaskServiceStub(channel),
        }
    finally:
        await channel.close()
        await server.stop(1)
