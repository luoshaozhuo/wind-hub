"""Fault injection 测试共享 fixture——复用 system 层 subprocess harness。"""

from __future__ import annotations

# 直接复用 system conftest 的 fixture 定义（import 即注册，不复制实现）。
from tests.system.conftest import collector_factory, modbus_server  # noqa: F401
