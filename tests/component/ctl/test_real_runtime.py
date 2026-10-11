"""CTL 直接操作真实 Collector / Commander Runtime，不经过 Server/Admin。"""

from __future__ import annotations

import grpc
import pytest

from collector.assembly import assemble_collector
from collector.infrastructure.grpc import build_grpc_server as build_collector_server
from commander.assembly import assemble_commander
from commander.infrastructure.grpc import build_grpc_server as build_commander_server
from ctl.client import CollectorClient, CommanderClient
from tests.support.new_collector import write_collector_config_tree
from tests.support.new_commander import write_minimal_config_tree


async def test_collector_task_start_stop_via_ctl(tmp_path) -> None:
    """验证 CTL 可独立查询并启停已配置采集任务。"""
    config_dir = write_collector_config_tree(
        tmp_path,
        devices=[
            {
                "device_id": "dev1",
                "model": "mod",
                "endpoint": {"host": "127.0.0.1", "port": 1},
            }
        ],
        runtime={"connect_timeout": 0.5},
    )
    app = assemble_collector(config_dir, collector_id="ctl-standalone")
    server = build_collector_server(
        app,
        app.identity,
        host="127.0.0.1",
        port=0,
    )
    await app.start()
    await server.start()
    try:
        async with CollectorClient(server.endpoint, timeout=3.0) as client:
            identity = await client.call("info")
            assert identity["collector_id"] == "ctl-standalone"

            tasks = await client.call("tasks")
            assert len(tasks["items"]) == 1
            task_id = tasks["items"][0]["task_id"]

            # 未知任务必须映射为稳定的 NOT_FOUND 错误。
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await client.call("start-task", task_id="ghost")
            assert error.value.code() == grpc.StatusCode.NOT_FOUND

            started = await client.call("start-task", task_id=task_id)
            assert started["runtime_state"] == "running"

            stopped = await client.call("stop-task", task_id=task_id)
            assert stopped["runtime_state"] == "stopped"
    finally:
        await server.stop(0)
        await app.stop()


async def test_commander_live_rpc_via_ctl(tmp_path) -> None:
    """验证 CTL 无 Server/Admin 时可读取真实 Commander 状态与诊断结果。"""
    config_dir = write_minimal_config_tree(
        tmp_path,
        devices=[
            {
                "device_id": "dev1",
                "model": "mod",
                "endpoint": {"host": "127.0.0.1", "port": 1},
            }
        ],
        runtime={"connect_timeout": 0.5, "read_timeout": 0.5},
    )
    app = assemble_commander(config_dir)
    server = build_commander_server(app, host="127.0.0.1", port=0)
    await app.start()
    await server.start()
    try:
        async with CommanderClient(server.endpoint, timeout=3.0) as client:
            status = await client.call("status")
            devices = await client.call("devices")
            diagnostic = await client.call(
                "verify-device",
                device_id="dev1",
                io_timeout=0.3,
            )

            assert status["running"] is True
            assert devices["devices"][0]["device_id"] == "dev1"
            assert diagnostic["device_id"] == "dev1"
            assert diagnostic["ok"] is False
    finally:
        await server.stop(0)
        await app.stop()
