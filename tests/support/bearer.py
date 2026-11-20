"""测试客户端 Bearer 环境与真实 gRPC metadata 适配。"""

from __future__ import annotations

import hashlib
import os
import re
from collections import namedtuple
from typing import Any

import grpc

from core.infrastructure.grpc_bearer import bearer_metadata

_CallDetails = namedtuple(
    "_CallDetails",
    ("method", "timeout", "metadata", "credentials", "wait_for_ready"),
)


def ensure_bearer(principal: str) -> str:
    """为测试主体 provision 唯一占位令牌，返回 env:NAME 引用。"""
    variable = "WIND_HUB_TEST_" + re.sub(r"[^A-Za-z0-9]+", "_", principal).strip("_").upper()
    digest = hashlib.sha256(principal.encode()).hexdigest()
    os.environ[variable] = f"test-only-{digest}"
    return f"env:{variable}"


class BearerMetadataInterceptor(grpc.aio.UnaryUnaryClientInterceptor):
    """在未显式提供 authorization 的测试调用上附加指定主体。"""

    def __init__(self, token_ref: str) -> None:
        self._token_ref = token_ref

    async def intercept_unary_unary(
        self,
        continuation: Any,
        client_call_details: grpc.aio.ClientCallDetails,
        request: Any,
    ) -> Any:
        metadata = list(client_call_details.metadata or ())
        if not any(key.lower() == "authorization" for key, _value in metadata):
            metadata.extend(bearer_metadata(self._token_ref))
        details = _CallDetails(
            client_call_details.method,
            client_call_details.timeout,
            metadata,
            client_call_details.credentials,
            client_call_details.wait_for_ready,
        )
        return await continuation(details, request)


def bearer_channel(endpoint: str, principal: str) -> grpc.aio.Channel:
    """创建使用指定测试主体 Bearer 的真实内网明文 channel。"""
    token_ref = ensure_bearer(principal)
    return grpc.aio.insecure_channel(
        endpoint,
        interceptors=(BearerMetadataInterceptor(token_ref),),
    )
