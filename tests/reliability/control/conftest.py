"""Control 故障测试共享 fixture。

复用 system 层的 subprocess harness；``proxied_stack`` 在 Commander 与
Modbus 从站之间插入 :class:`WriteResponseDropProxy`——设备 endpoint 指向
代理端口，采集/命令流量全部经过代理，测试用 ``drop_write_responses``
开关注入「设备已执行但响应丢失」。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests.fixtures.servers.modbus_proxy import WriteResponseDropProxy
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.functional_config import write_functional_config
from tests.support.process import (
    CollectorProcess,
    free_port,
    start_server,
)
from tests.support.wait import wait_http_ready

# 直接复用 system conftest 的 fixture 定义（import 即注册，不复制实现）。
from tests.system.conftest import (  # noqa: F401
    collector_factory,
    commander_factory,
    full_stack,
    modbus_server,
)

PROXIED_COLLECTOR_ID = "collector-1"


@dataclass
class ProxiedStack:
    """Commander/Collector 经代理访问从站的完整系统现场。"""

    modbus: ModbusMockServer
    proxy: WriteResponseDropProxy
    collector: CollectorProcess
    commander: CollectorProcess
    server: CollectorProcess
    http: httpx.AsyncClient
    config_dir: Path
    sink_path: Path


@pytest.fixture
async def proxied_stack(
    modbus_server: ModbusMockServer,  # noqa: F811 - fixture 参数注入即对导入 fixture 的消费
    collector_factory,  # noqa: F811
    commander_factory,  # noqa: F811
    tmp_path: Path,
) -> AsyncIterator[ProxiedStack]:
    proxy = WriteResponseDropProxy("127.0.0.1", modbus_server.port, free_port())
    await proxy.start()

    sink_path = tmp_path / "out" / "telemetry.csv"
    config_dir = write_functional_config(
        tmp_path / "cfg",
        proxy.port,
        sinks=[
            {
                "name": "file_sink",
                "type": "file",
                "connection": {
                    "path": str(sink_path),
                },
            }
        ],
        tasks=[
            {
                "task_id": "modbus-telemetry",
                "device": "modbus-1",
                "point_group": "telemetry",
                "interval": 0.2,
                "targets": [{"sink": "file_sink"}],
            }
        ],
    )
    collector: CollectorProcess = await collector_factory(
        config_dir, collector_id=PROXIED_COLLECTOR_ID
    )
    commander: CollectorProcess = await commander_factory(config_dir)
    server = start_server(
        config_dir,
        collectors=[f"{PROXIED_COLLECTOR_ID}={collector.grpc_target}"],
        commander=commander.grpc_target,
        log_dir=tmp_path,
    )
    try:
        await wait_http_ready(server.grpc_target)
    except BaseException:
        server.kill_tree()
        server.close_log()
        await proxy.close()
        raise

    http = httpx.AsyncClient(base_url=f"http://{server.grpc_target}", timeout=20.0)
    try:
        yield ProxiedStack(
            modbus=modbus_server,
            proxy=proxy,
            collector=collector,
            commander=commander,
            server=server,
            http=http,
            config_dir=config_dir,
            sink_path=sink_path,
        )
    finally:
        await http.aclose()
        try:
            if server.is_running():
                server.terminate()
        except BaseException:
            server.kill_tree()
        finally:
            server.close_log()
        await proxy.close()
