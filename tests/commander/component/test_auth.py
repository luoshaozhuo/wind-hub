"""§14.3 test_auth.py（component/security）：证书 SAN 身份→caller_id 映射与授权。

直接驱动 gRPC 边界的 ``_Authorized``：TLS 握手由集成层 test_grpc.py 的真实
mTLS 覆盖，本层用可控的假 auth_context 穷举身份/权限矩阵与异常→状态码映射。
"""

from __future__ import annotations

from typing import Any

import grpc
import pytest

from commander.infrastructure.grpc.server import _Authorized, _page, _scalar
from contracts import common_pb2
from core.application.errors import ConfigError

pytestmark = [
    pytest.mark.component,
    pytest.mark.security,
]


class FakeContext:
    """记录 abort 的 ServicerContext 替身；auth_context 可控。"""

    def __init__(self, identities: list[bytes] | None) -> None:
        self._identities = identities or []
        self.aborted: tuple[grpc.StatusCode, str] | None = None

    def auth_context(self) -> dict[str, list[bytes]]:
        return {"x509_subject_alternative_name": list(self._identities)}

    async def abort(self, code: grpc.StatusCode, details: str) -> None:
        self.aborted = (code, details)
        raise _AbortError()


class _AbortError(Exception):
    """模拟 grpc context.abort 抛出（真实实现抛 AioRpcError 路径外的内部异常）。"""


_PERMISSIONS = {
    "viewer@windhub.test": ("viewer-console", frozenset({"query"})),
    "ops@windhub.test": ("ops-console", frozenset({"query", "control"})),
    "deployer@windhub.test": ("reload-deployer", frozenset({"query", "reload"})),
}


def _boundary() -> _Authorized:
    return _Authorized(service=object(), permissions=_PERMISSIONS)  # type: ignore[arg-type]


async def _require(
    identities: list[str] | None, scope: str
) -> tuple[str | None, grpc.StatusCode | None]:
    context = FakeContext([item.encode() for item in identities] if identities else [])
    try:
        caller = await _boundary().require(context, scope)
    except _AbortError:
        assert context.aborted is not None
        return None, context.aborted[0]
    return caller, None


@pytest.mark.normal
async def test_known_identity_returns_caller_id_when_scope_granted() -> None:
    caller, code = await _require(["ops@windhub.test"], "control")
    assert caller == "ops-console" and code is None
    caller, code = await _require(["deployer@windhub.test"], "reload")
    assert caller == "reload-deployer" and code is None
    caller, code = await _require(["viewer@windhub.test"], "query")
    assert caller == "viewer-console" and code is None


@pytest.mark.exception
async def test_missing_client_identity_is_unauthenticated() -> None:
    caller, code = await _require(None, "query")
    assert caller is None and code is grpc.StatusCode.UNAUTHENTICATED
    caller, code = await _require([], "query")
    assert caller is None and code is grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.exception
async def test_unregistered_identity_is_permission_denied() -> None:
    caller, code = await _require(["stranger@windhub.test"], "query")
    assert caller is None and code is grpc.StatusCode.PERMISSION_DENIED


@pytest.mark.exception
async def test_authenticated_but_scope_missing_is_permission_denied() -> None:
    # viewer 只有 query，不能 control/reload。
    _caller, code = await _require(["viewer@windhub.test"], "control")
    assert code is grpc.StatusCode.PERMISSION_DENIED
    _caller, code = await _require(["viewer@windhub.test"], "reload")
    assert code is grpc.StatusCode.PERMISSION_DENIED
    # ops 有 control/query，没有 reload。
    _caller, code = await _require(["ops@windhub.test"], "reload")
    assert code is grpc.StatusCode.PERMISSION_DENIED
    # deployer 有 reload/query，没有 control。
    _caller, code = await _require(["deployer@windhub.test"], "control")
    assert code is grpc.StatusCode.PERMISSION_DENIED


@pytest.mark.exception
async def test_certificate_matching_two_callers_is_permission_denied() -> None:
    _caller, code = await _require(["viewer@windhub.test", "ops@windhub.test"], "query")
    assert code is grpc.StatusCode.PERMISSION_DENIED


@pytest.mark.exception
@pytest.mark.parametrize(
    "exc, expected",
    [
        (OverflowError("capacity"), grpc.StatusCode.RESOURCE_EXHAUSTED),
        (ValueError("bad field"), grpc.StatusCode.INVALID_ARGUMENT),
        (TypeError("bad type"), grpc.StatusCode.INVALID_ARGUMENT),
        (ConfigError("bad config"), grpc.StatusCode.INVALID_ARGUMENT),
        (KeyError("missing-command"), grpc.StatusCode.NOT_FOUND),
        (FileNotFoundError("x"), grpc.StatusCode.NOT_FOUND),
        (PermissionError("not authorized"), grpc.StatusCode.PERMISSION_DENIED),
        (RuntimeError("configuration revision conflict"), grpc.StatusCode.ABORTED),
        (RuntimeError("expected_revision does not match disk revision"), grpc.StatusCode.ABORTED),
        (RuntimeError("dispatcher unavailable"), grpc.StatusCode.FAILED_PRECONDITION),
    ],
)
async def test_exception_to_grpc_status_mapping(exc: Exception, expected: grpc.StatusCode) -> None:
    context = FakeContext(["unused"])
    with pytest.raises(_AbortError):
        await _boundary().invalid(context, exc)
    assert context.aborted is not None
    assert context.aborted[0] is expected
    assert str(exc) in context.aborted[1]


@pytest.mark.exception
async def test_unknown_exception_maps_to_internal() -> None:
    context = FakeContext(["unused"])
    with pytest.raises(_AbortError):
        await _boundary().invalid(context, OSError("boom"))
    # OSError 不在映射表内（快照层在调用前自行转 ValidationIssue）；边界层
    # 对未知异常给 INTERNAL，避免吞错。
    assert context.aborted is not None
    assert context.aborted[0] is grpc.StatusCode.INTERNAL


# ---------------------------------------------------------------------------
# 分页与 ScalarValue 适配纯逻辑（错误码契约的单元级固化）
# ---------------------------------------------------------------------------


@pytest.mark.normal
@pytest.mark.parametrize(
    "size,token,count",
    [(0, "", 5), (2, "", 5), (2, "2", 5), (1000, "", 3), (5, "5", 5)],
)
def test_page_canonical_windows(size: int, token: str, count: int) -> None:
    start, end, next_token = _page(size, token, count)
    assert 0 <= start <= end <= count
    assert start == int(token or 0)
    assert next_token == "" or str(end) == next_token


@pytest.mark.exception
@pytest.mark.parametrize("size,token", [(1001, ""), (1, "abc"), (1, "01"), (1, "-1"), (1, "99")])
def test_page_rejects_boundary_violations(size: int, token: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - 映射 INVALID_ARGUMENT 的源异常
        _page(size, token, 10)


@pytest.mark.normal
@pytest.mark.parametrize(
    "setter,expected",
    [
        ("bool_value", True),
        ("sint64_value", -9),
        ("uint64_value", 9),
        ("double_value", 2.5),
        ("string_value", "x"),
    ],
)
def test_scalar_value_five_kinds_decode(setter: str, expected: Any) -> None:
    scalar = common_pb2.ScalarValue()
    setattr(scalar, setter, expected)
    assert _scalar(scalar) == expected


@pytest.mark.exception
def test_scalar_value_unset_and_bytes_rejected() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        _scalar(common_pb2.ScalarValue())
    with pytest.raises(ValueError):  # noqa: PT011
        _scalar(common_pb2.ScalarValue(bytes_value=b"\x00"))
