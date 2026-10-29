"""R6：TDengine COMPOSITE KEY 真实能力与历史不可变幂等契约。

D5-01 复测要求区分三件事，不能只凭 DDL 成功就宣布 reliable 契约通过：

1. 运行中的真实服务端版本（``SERVER_VERSION()``，不是镜像标签）；
2. ``sample_id`` 在 ``DESCRIBE`` 中确为 COMPOSITE KEY 列（不是普通列）；
3. 复合键的去重语义是「拒绝/丢弃重复」还是「UPSERT 覆盖」——
   架构 v0_4 规定重复投递应「去重 / 丢弃」，目标侧幂等以 sample_id 为依据。
   若同一 (ts, sample_id) 的不同内容会覆盖**已确认**记录，则首写不可变
   契约不成立（重传消息内容相同时尚无害，但任何同身份不同值都会静默改写
   历史），此用例稳定失败交生产侧裁定，禁止修改期望值掩盖。

全部断言走真实 taosAdapter REST SQL。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from tests.fixtures.services.r5_stack import TDENGINE_DATABASES

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


def test_redelivery_with_different_value_must_not_overwrite_confirmed_record(
    r5_stack, r6_subtable
) -> None:
    """RELATED 契约项：同 (ts, sample_id) 不同值重写必须被去重/丢弃。

    v0_4 第 434/483 行：重复投递「去重 / 丢弃」，目标侧以 sample_id 幂等。
    真实 3.3.6.x Community 的行为是 UPSERT（第二次写 code=0 且覆盖原值），
    首写不可变性不成立。用例如实失败以固定证据，等待生产侧裁定（应用层
    去重/不可变写入，或明确将契约改为允许 UPSERT 并评估已确认记录被改写
    的风险）。
    """
    ts = int(time.time() * 1000) - 50_000
    first = _insert(r5_stack, ts, "r6-s3", "100.0")
    second = _insert(r5_stack, ts, "r6-s3", "200.0")
    assert first["code"] == 0
    rows = _rows(r5_stack, "WHERE sample_id = 'r6-s3'")
    assert len(rows) == 1, f"expected dedup to a single row, got {rows}"
    if second["code"] == 0 and rows[0][2] == "200.0":
        pytest.fail(
            "CONTRACT VIOLATION: (ts, sample_id) UPSERT overwrote the confirmed "
            "record 100.0 -> 200.0; TDengine COMPOSITE KEY is not first-write "
            "immutable on this server"
        )
    assert rows[0][2] == "100.0"


def test_same_sample_id_different_ts_keeps_history(r5_stack, r6_subtable) -> None:
    base = int(time.time() * 1000) - 40_000
    assert _insert(r5_stack, base, "r6-s4", "1.0")["code"] == 0
    assert _insert(r5_stack, base + 1_000, "r6-s4", "2.0")["code"] == 0
    rows = _rows(r5_stack, "WHERE sample_id = 'r6-s4' ORDER BY ts")
    # sample_id 不是全局唯一键；身份为 (ts, sample_id)，历史两行均保留。
    assert [row[2] for row in rows] == ["1.0", "2.0"]
