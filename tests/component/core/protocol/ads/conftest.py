"""ADS 组件测试的进程级 driver 全局态隔离。

重构后本地 AMS 生命周期状态（``_LOCAL_CLOSED`` 等）保存在 driver 模块
全局变量中（生产进程只有一个事件循环）。pytest-asyncio 每个用例使用独立
事件循环，旧实现的路由注册表按循环弱引用持有，天然逐用例隔离；全局态
版本必须显式复位，否则先执行的 close_local 用例闩锁会让后续用例的
connect 全部快速失败。
"""

from __future__ import annotations

import asyncio

import pytest

import core.infrastructure.protocol.ads.driver as driver_module


@pytest.fixture(autouse=True)
def _reset_ads_driver_globals() -> None:
    driver_module._LOCAL_CLOSED = False
    driver_module._LOCAL_OPERATIONS = 0
    driver_module._LOCAL_AMS = None
    driver_module._LOCAL_PORT_OPEN = False
    driver_module._ACTIVE_DRIVERS.clear()
    # 模块级 asyncio.Lock 可能已被前一用例的事件循环绑定，逐用例重建。
    driver_module._LOCAL_LIFECYCLE = asyncio.Lock()
    yield
    driver_module._LOCAL_CLOSED = False
    driver_module._LOCAL_OPERATIONS = 0
    driver_module._LOCAL_AMS = None
    driver_module._LOCAL_PORT_OPEN = False
    driver_module._ACTIVE_DRIVERS.clear()
    driver_module._LOCAL_LIFECYCLE = asyncio.Lock()
