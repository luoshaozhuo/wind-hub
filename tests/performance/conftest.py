"""tests/perf 的 pytest fixtures：veth 创建、netem 应用、server 启动。

本目录默认没有任何 ``test_*.py``（压测走 ``scripts/run_benchmark.py``，
决策 9）；这些 fixtures 为将来需要 pytest 驱动的压测用例备好环境
（例如 ``@pytest.mark.performance`` 标记的场景化测试）。无 root 权限时一律
skip 而非 fail（决策 10：不自动 sudo）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.performance.netem import NetemController
from tests.performance.veth import VethManager, VethPair


@pytest.fixture
def veth_manager() -> VethManager:
    """默认参数的 veth 管理器（不创建，仅提供句柄）。"""
    return VethManager()


@pytest.fixture
async def veth_pair(veth_manager: VethManager) -> AsyncIterator[VethPair]:
    """创建好的 veth pair；无 root 权限时 skip。"""
    if not veth_manager.check_permission():
        pytest.skip("veth 创建需要 root 权限（决策 10：不自动 sudo）")
    async with veth_manager.active() as pair:
        yield pair


@pytest.fixture
def netem_controllers(veth_pair: VethPair) -> list[NetemController]:
    """veth 两端各一个 netem 控制器（双方向损伤注入）。"""
    return [
        NetemController(veth_pair.name_client),
        NetemController(veth_pair.name_server),
    ]
