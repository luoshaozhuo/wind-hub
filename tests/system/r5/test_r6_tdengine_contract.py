"""R6/R7：TDengine COMPOSITE KEY 真实能力与历史存储幂等契约。

验收要点（不能只凭 DDL 成功就宣布 reliable 契约通过）：

1. 运行中的真实服务端版本（``SERVER_VERSION()``，不是镜像标签）；
2. ``sample_id`` 在 ``DESCRIBE`` 中确为 COMPOSITE KEY 列（不是普通列）；
3. 幂等语义（R7 已收敛，见 v0_4 6.2）：
   - 相同消息（同 ``(ts, sample_id)`` 同内容）重投最终一条且内容一致；
   - 同 ``(ts, sample_id)`` 不同内容为数据库 UPSERT 覆盖——Sink Service
     不承诺首写不可变，风险由上游「同 sample_id 内容不可变」规避；
   - 文档必须明确披露 UPSERT 行为，生产代码不得残留错误版本声明。
4. 不支持 COMPOSITE KEY 的旧版 TDengine 必须在 open() 阶段 fail-closed。

全部断言走真实 taosAdapter REST SQL 与真实容器。
"""

from __future__ import annotations

import base64
import json
import secrets
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sink_service.application.error import SinkError
from sink_service.infrastructure.adapters import TDengineSink
from tests.fixtures.services.r5_stack import TDENGINE_DATABASES, docker_available

REPO_ROOT = Path(__file__).resolve().parents[3]
ARCH_DOC = REPO_ROOT / "docs" / "wind-hub_sink_architecture_v0_4.md"
TDENGINE_ADAPTER = (
    REPO_ROOT / "src" / "sink_service" / "infrastructure" / "adapters" / "tdengine.py"
)

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker]

STABLE = "point_values"
DB_A = TDENGINE_DATABASES[0]
SUB = "r6_contract_sub"


def _sql(stack, stmt: str, *, database: str | None = DB_A) -> dict[str, Any]:
    return stack.tdengine_sql(stmt, database=database)


def _rows(stack, where: str) -> list[list[Any]]:
    body = _sql(stack, f"SELECT ts, sample_id, value_json FROM {STABLE} {where}")
    return list(body.get("data") or [])


def _insert(stack, ts_ms: int, sample_id: str, value: str) -> dict[str, Any]:
    return _sql(
        stack,
        f"INSERT INTO {DB_A}.{SUB} USING {DB_A}.{STABLE} "
        "TAGS ('r6-dev', 'r6-point') VALUES "
        f"({ts_ms}, '{sample_id}', '{value}', 'good', NULL, NULL, 'r6-probe')",
    )


@pytest.fixture
def r6_subtable(r5_stack) -> str:
    """生产 DDL 已由 SinkRuntime/open 在同库建立（r5_stack session 共享）。

    若超级表尚不存在（单独运行本文件），用与生产相同的 DDL 先建表。
    """
    _sql(
        r5_stack,
        f"CREATE STABLE IF NOT EXISTS {DB_A}.{STABLE} ("
        "ts TIMESTAMP, sample_id VARCHAR(192) COMPOSITE KEY, "
        "value_json NCHAR(2048), quality NCHAR(32), "
        "receive_time TIMESTAMP, device_time TIMESTAMP, source NCHAR(64)) "
        "TAGS (device_id NCHAR(192), point_id NCHAR(192))",
        database=DB_A,
    )
    _sql(r5_stack, f"DROP TABLE IF EXISTS {DB_A}.{SUB}")
    return SUB


def test_server_version_is_reported(r5_stack) -> None:
    body = _sql(r5_stack, "SELECT SERVER_VERSION()", database=None)
    version = (body.get("data") or [["unknown"]])[0][0]
    # 记录真实运行版本；断言它至少是 3.3.6.x（首个公开支持该语法的发行线）。
    assert isinstance(version, str) and version.count(".") == 3
    major, minor, patch, *_ = (int(part) for part in version.split("."))
    assert (major, minor, patch) >= (3, 3, 6), f"composite key unsupported on {version}"


def test_sample_id_is_real_composite_key_column(r5_stack, r6_subtable) -> None:
    body = _sql(r5_stack, f"DESCRIBE {STABLE}")
    notes = {row[0]: row[3] for row in body.get("data") or []}
    assert notes.get("sample_id") == "COMPOSITE KEY"
    # 超级表与子表均可查询。
    assert _sql(r5_stack, f"SELECT sample_id FROM {STABLE} LIMIT 0")["code"] == 0


def test_same_ts_different_sample_id_keeps_both(r5_stack, r6_subtable) -> None:
    ts = int(time.time() * 1000) - 60_000
    assert _insert(r5_stack, ts, "r6-s1-a", "1.5")["code"] == 0
    assert _insert(r5_stack, ts, "r6-s1-b", "2.5")["code"] == 0
    rows = _rows(r5_stack, "WHERE sample_id IN ('r6-s1-a','r6-s1-b') ORDER BY sample_id")
    assert len(rows) == 2
    assert [row[1] for row in rows] == ["r6-s1-a", "r6-s1-b"]


def test_exact_duplicate_collapses_to_single_identical_row(r5_stack, r6_subtable) -> None:
    ts = int(time.time() * 1000) - 55_000
    assert _insert(r5_stack, ts, "r6-s2", "10.0")["code"] == 0
    assert _insert(r5_stack, ts, "r6-s2", "10.0")["code"] == 0
    rows = _rows(r5_stack, "WHERE sample_id = 'r6-s2'")
    assert len(rows) == 1
    assert rows[0][2] == "10.0"


def test_conflicting_value_redelivery_follows_documented_upsert_contract(
    r5_stack, r6_subtable
) -> None:
    """R7 收敛后的契约（v0_4 6.2 第 3 项）：同 (ts, sample_id) 异值为 UPSERT。

    Sink Service 只承诺「相同消息重投最终一条且内容一致」，不承诺同主键
    冲突内容首写不可变；TDengine 实测第二次写 code=0 且覆盖旧值。本用例
    把真实 UPSERT 行为钉死为期望，防止未来数据库行为静默变化；冲突覆盖
    风险由上游「同 sample_id 内容不可变」要求规避，披露一致性由
    ``test_architecture_doc_discloses_upsert_contract`` 守护。
    """
    ts = int(time.time() * 1000) - 50_000
    first = _insert(r5_stack, ts, "r6-s3", "100.0")
    second = _insert(r5_stack, ts, "r6-s3", "200.0")
    assert first["code"] == 0
    # 不是拒绝/报错，而是接受并覆盖——这是契约允许且必须如实披露的行为。
    assert second["code"] == 0, f"expected documented UPSERT, got {second}"
    rows = _rows(r5_stack, "WHERE sample_id = 'r6-s3'")
    assert len(rows) == 1, f"expected single surviving row, got {rows}"
    assert rows[0][2] == "200.0", f"expected UPSERT overwrite, got {rows}"


def test_same_sample_id_different_ts_keeps_history(r5_stack, r6_subtable) -> None:
    base = int(time.time() * 1000) - 40_000
    assert _insert(r5_stack, base, "r6-s4", "1.0")["code"] == 0
    assert _insert(r5_stack, base + 1_000, "r6-s4", "2.0")["code"] == 0
    rows = _rows(r5_stack, "WHERE sample_id = 'r6-s4' ORDER BY ts")
    # sample_id 不是全局唯一键；身份为 (ts, sample_id)，历史两行均保留。
    assert [row[2] for row in rows] == ["1.0", "2.0"]


# ---------------------------------------------------------------------------
# R7：文档/生产注释/错误信息一致性 + 旧版 fail-closed
# ---------------------------------------------------------------------------


def test_architecture_doc_discloses_upsert_contract() -> None:
    """v0_4 必须显式披露最终幂等键、UPSERT 覆盖风险与上游不可变要求。"""
    text = ARCH_DOC.read_text(encoding="utf-8")
    required = [
        "(ts, sample_id)",
        "UPSERT",
        "首写不可变",
        "内容必须保持不变",
        "ACK 只能在适配器确认目标数据库写入成功之后发出",
        "3.3.6.13",
        "COMPOSITE KEY",
    ]
    missing = [token for token in required if token not in text]
    assert not missing, f"architecture doc missing contract disclosure: {missing}"


def test_production_code_has_no_incorrect_version_claim() -> None:
    """D6-01：生产代码不得再出现未经核实的具体最低版本（如 3.3.8.x）。"""
    text = TDENGINE_ADAPTER.read_text(encoding="utf-8")
    assert "3.3.8" not in text
    assert "COMPOSITE KEY" in text
    # fail-closed 提示必须包含兼容性指引而非静默吞错。
    assert "verify TDengine server compatibility" in text


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _rest_sql(port: int, sql_text: str, *, database: str | None = None) -> dict[str, Any]:
    url = f"http://127.0.0.1:{port}/rest/sql" + (f"/{database}" if database else "")
    token = base64.b64encode(b"root:taosdata").decode("ascii")
    request = urllib.request.Request(
        url,
        data=sql_text.encode(),
        headers={"Authorization": f"Basic {token}", "Content-Type": "text/plain"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def _wait_rest_ready(port: int, *, deadline_s: float = 120.0) -> str:
    deadline = time.monotonic() + deadline_s
    last = "not ready"
    while time.monotonic() < deadline:
        try:
            body = _rest_sql(port, "SELECT SERVER_VERSION()")
            if body.get("code") == 0:
                return str(body["data"][0][0])
        except Exception as exc:  # taosAdapter 起来到可查询之间有窗口
            last = repr(exc)
        time.sleep(2.0)
    raise RuntimeError(f"legacy TDengine REST not ready: {last}")


@pytest.fixture
def legacy_tdengine_3350() -> Any:
    """独立的 TDengine 3.3.5.0 容器（loopback 动态端口），与 R5 栈完全隔离。"""
    if not docker_available():
        pytest.skip("docker unavailable")
    port = _free_port()
    name = f"r6legacy-{secrets.token_hex(4)}"
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:6041",
            "tdengine/tdengine:3.3.5.0",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if run.returncode != 0:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
        pytest.skip(f"legacy image unavailable: {run.stderr.strip()}")
    try:
        version = _wait_rest_ready(port)
        yield port, version
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60, check=False)


async def test_legacy_tdengine_open_fails_closed(legacy_tdengine_3350) -> None:
    """不支持 VARCHAR COMPOSITE KEY 的 3.3.5.0：生产 open() 必须明确报错。"""
    port, version = legacy_tdengine_3350
    assert version.startswith("3.3.5"), f"fixture sanity: unexpected {version}"
    _rest_sql(port, "CREATE DATABASE IF NOT EXISTS r6legacy")
    adapter = TDengineSink(
        SimpleNamespace(
            connection={
                "host": "127.0.0.1",
                "port": port,
                "database": "r6legacy",
                "username": "root",
                "password": "taosdata",
                "timeout": 5.0,
            },
            task_sources=None,
        )
    )
    try:
        with pytest.raises(SinkError) as excinfo:
            await adapter.open()
    finally:
        await adapter.close()
    message = str(excinfo.value)
    assert "COMPOSITE KEY" in message
    # 不得残留错误的具体版本下限声明。
    assert "3.3.8" not in message
