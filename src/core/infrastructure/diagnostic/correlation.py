"""Request-ID 跨进程关联：HTTP/gRPC 入口 → contextvar → 日志。

- Web Server 侧另有 ASGI 中间件与出站 gRPC 拦截器（复用本模块的
  contextvar）；Collector/Commander 侧由入站 gRPC 拦截器绑定。
- 仅用于可观测性，不携带任何鉴权语义。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

REQUEST_ID_HEADER = "x-request-id"

#: 协程/线程安全的当前 Request-ID；无请求上下文时为空串。
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

#: 截断上限，防止外部输入的 ID 撑爆日志行。
_MAX_LENGTH = 128


def get_request_id() -> str:
    """返回当前上下文的 Request-ID；未绑定时为空串。"""
    return request_id_var.get()


def new_request_id() -> str:
    """生成新的 Request-ID（UUID4 hex）。"""
    return uuid.uuid4().hex


def normalize_request_id(value: str | None) -> str:
    """规整入口收到的 Request-ID：去空白、截断；非法时返回空串。"""
    if not value:
        return ""
    cleaned = value.strip()[:_MAX_LENGTH]
    # 头内容不得注入控制字符/换行，避免日志伪造。
    if not cleaned or any(ord(char) < 0x20 for char in cleaned):
        return ""
    return cleaned


@contextmanager
def bind_request_id(request_id: str) -> Iterator[None]:
    """在 with 块内绑定 Request-ID，退出时恢复上层上下文。"""
    token = request_id_var.set(request_id)
    try:
        yield
    finally:
        request_id_var.reset(token)


class RequestIdFilter(logging.Filter):
    """给每条日志记录注入当前 Request-ID；无上下文时为 ``-``。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        return True
