"""R5-2：JetStream reliable → Sink Service → TDengine 真实持久化链路。

所有断言通过 taosAdapter REST SQL 直查 TDengine 子表，不以 ACK 返回代替
落库证据。覆盖：

- PubAck/显式 ACK 后行可见，值/质量/sample_id 与发布一致；
- 同一 (ts, sample_id) 重复投递在 TDengine COMPOSITE KEY 上幂等；
- 目标故障期写失败 → NAK 退避重投，恢复后自动补齐，未 ACK 不丢；
- sink_service 进程终止后以相同 deployment（同名 durable）重启，
  JetStream 重投在途消息；
- StopSink 期间积压在 StartSink 后补齐；
- 两个 durable 各自独立投递，ClearSink 只跳过本 durable 的积压。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.infrastructure.config import YamlConfigAdapter
from sink_service.infrastructure.adapters import TDengineSink
from sink_service.infrastructure.config import _resolve_sinks
from tests.fixtures.services.r5_stack import TDENGINE_DATABASES
from tests.support.process import CollectorProcess, free_port
from tests.support.wait import wait_until
from tests.system.r5.conftest import make_sample, mgmt, publish, wait_state
from tests.system.r5.r5_config import TASK_ID, write_r5_config

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker]

POINT = "rotor_speed"


def _rsample(*args: Any, **kwargs: Any):
    """reliable 信封：生产契约要求省略 max_staleness_ms（真实 Collector 同此）。"""
    kwargs.setdefault("max_staleness_ms", None)
    return make_sample(*args, **kwargs)


STABLE = "point_values"
DB_A = TDENGINE_DATABASES[0]
DB_B = TDENGINE_DATABASES[1]


def _td_sink(*, sink_id: str, database: str, host: str, port: int) -> dict[str, Any]:
    return {
        sink_id: {
            "type": "tdengine",
            "connection": {
                "host": host,
                "port": port,
                "database": database,
                "username": "root",
                "password": "taosdata",
                "timeout": 5.0,
            },
            "outputs": {TASK_ID: {}},
        }
    }


def _table_name(device_id: str, point_id: str) -> str:
    """与生产 TDengineSink._table_name 完全相同的子表命名。"""
    identity = json.dumps([device_id, point_id], ensure_ascii=False).encode("utf-8")
    return "p_" + hashlib.sha256(identity).hexdigest()[:32]


def _query(stack, database: str, where: str = "") -> list[list[Any]]:
    sql = f"SELECT ts, sample_id, value_json, quality FROM {STABLE} {where}".strip()
    body = stack.tdengine_sql(sql, database=database)
    return list(body.get("data") or [])


def _count(stack, database: str, like: str) -> int:
    body = stack.tdengine_sql(
        f"SELECT COUNT(*) FROM {STABLE} WHERE sample_id LIKE '{like}'",
        database=database,
    )
    rows = body.get("data") or []
    return int(rows[0][0]) if rows else 0


async def _wait_count(
    stack, database: str, like: str, expected: int, *, timeout: float = 60.0
) -> int:
    async def _probe() -> int | None:
        count = _count(stack, database, like)
        return count if count >= expected else None

    return await wait_until(
        _probe, timeout=timeout, interval=1.0, description=f"{database} rows {like}"
    )


async def _wait_tdengine_ready(stack, *, timeout: float = 60.0) -> None:
    async def _probe() -> bool | None:
        try:
            stack.tdengine_sql("SELECT SERVER_VERSION()")
        except Exception:
            return None
        return True

    await wait_until(_probe, timeout=timeout, interval=1.0, description="taosAdapter queryable")


async def _start_reliable(
    r5_sink_factory, config_dir: Path, *, deployment_id: str
) -> CollectorProcess:
    return await r5_sink_factory(
        config_dir,
        delivery="reliable",
        reliable_sinks=("td_sink",),
        deployment_id=deployment_id,
    )


def _write_config(tmp_path: Path, stack, database: str) -> Path:
    host, _, port = stack.tdengine_rest.rpartition(":")
    return write_r5_config(
        tmp_path / f"cfg-{database}",
        free_port(),
        stack.nats,
        _td_sink(sink_id="td_sink", database=database, host=host, port=int(port)),
        delivery="reliable",
    )


async def test_ddl_composite_key_accepted_by_real_tdengine(r5_stack, tmp_path: Path) -> None:
    """RELATED 生产缺陷（阻断 R5-2 全部行为用例）。

    TDengineSink.open() 的建表 DDL 在真实 TDengine Community 3.3.5.0 上
    被拒（code 9728: syntax error near "key, ..."）：``VARCHAR(192)
    COMPOSITE KEY`` 不是社区版支持的语法。Sink 因此永远 FAILED，reliable
    链路无法订阅 durable，历史持久化整体不可用。

    复现：src/sink_service/infrastructure/adapters/tdengine.py:102-108
    直接对真实 taosAdapter 执行 open()。修复前本测试稳定失败；不得用
    skip/xfail 掩盖。
    """
    config_dir = _write_config(tmp_path, r5_stack, DB_A)
    snapshot = YamlConfigAdapter(config_dir).load()
    resolved = _resolve_sinks(snapshot)["td_sink"]
    adapter = TDengineSink(resolved)
    await adapter.open()
    # open 成功后超级表必须真实存在（SELECT 而非仅 DDL 返回）。
    r5_stack.tdengine_sql(f"SELECT sample_id FROM {STABLE} LIMIT 0", database=DB_A)
    await adapter.close()


async def test_reliable_writes_persist_with_sql_evidence(
    r5_sink_factory, r5_stack, tmp_path: Path
) -> None:
    config_dir = _write_config(tmp_path, r5_stack, DB_A)
    sink = await _start_reliable(r5_sink_factory, config_dir, deployment_id="r5-basic")
    await wait_state(sink, "td_sink", "running")

    base = datetime.now(UTC) - timedelta(seconds=3)
    samples = [
        _rsample(POINT, 1.5, sample_id="r5basic-1", sample_time=base),
        _rsample(POINT, 2.5, sample_id="r5basic-2", sample_time=base + timedelta(seconds=1)),
        _rsample(POINT, 3.5, sample_id="r5basic-3", sample_time=base + timedelta(seconds=2)),
    ]
    for sample in samples:
        await publish(r5_stack, sample, "reliable")

    await _wait_count(r5_stack, DB_A, "r5basic-%", 3)
    rows = list(_query(r5_stack, DB_A, "WHERE sample_id LIKE 'r5basic-%'"))
    assert [row[1] for row in rows] == ["r5basic-1", "r5basic-2", "r5basic-3"]
    assert [json.loads(row[2]) for row in rows] == [1.5, 2.5, 3.5]
    assert all(row[3] == "good" for row in rows)

    status = await mgmt(sink, "GetSinkStatus", "td_sink")
    assert status["pending"] == 0

    # 子表按 (device, point) 哈希落到生产命名规则的表。
    body = r5_stack.tdengine_sql(
        f"SELECT COUNT(*) FROM {_table_name('modbus-1', POINT)} WHERE sample_id LIKE 'r5basic-%'",
        database=DB_A,
    )
    assert int((body.get("data") or [[0]])[0][0]) == 3


async def test_duplicate_redelivery_is_idempotent(
    r5_sink_factory, r5_stack, tmp_path: Path
) -> None:
    config_dir = _write_config(tmp_path, r5_stack, DB_A)
    sink = await _start_reliable(r5_sink_factory, config_dir, deployment_id="r5-dup")
    await wait_state(sink, "td_sink", "running")

    sample = _rsample(POINT, 7.7, sample_id="r5dup-1")
    # 两次独立 JetStream 发布（不同 stream seq），同一 (ts, sample_id)。
    await publish(r5_stack, sample, "reliable")
    await publish(r5_stack, sample, "reliable")

    async def _both_acked() -> bool | None:
        status = await mgmt(sink, "GetSinkStatus", "td_sink")
        if status["pending"] == 0 and _count(r5_stack, DB_A, "r5dup-1") >= 1:
            return True
        return None

    await wait_until(_both_acked, timeout=30.0, interval=0.5, description="both messages ACKed")
    # 两条都显式 ACK（无重投挂起），但 TDengine 只有一行——幂等在目标侧成立。
    assert _count(r5_stack, DB_A, "r5dup-1") == 1


async def test_tdengine_outage_nak_then_redelivered_after_recovery(
    r5_sink_factory, r5_stack, tmp_path: Path
) -> None:
    config_dir = _write_config(tmp_path, r5_stack, DB_A)
    sink = await _start_reliable(r5_sink_factory, config_dir, deployment_id="r5-outage")
    await wait_state(sink, "td_sink", "running")

    r5_stack.stop_container("tdengine")
    try:
        await publish(r5_stack, _rsample(POINT, 9.1, sample_id="r5out-1"), "reliable")

        async def _failed() -> dict[str, Any] | None:
            status = await mgmt(sink, "GetSinkStatus", "td_sink")
            if status["state"] == "failed" and status["failure"]:
                return status
            return None

        failed = await wait_until(_failed, timeout=15.0, interval=0.5, description="sink failed")
        assert "TDengine" in str(failed["failure"]) or "write" in str(failed["failure"]).lower()
    finally:
        r5_stack.start_container("tdengine")
        await _wait_tdengine_ready(r5_stack)

    # supervisor 每 5s 重启 FAILED Sink；NAK 退避（1/2/4s）后重投成功。
    await wait_state(sink, "td_sink", "running", timeout=90.0)
    await _wait_count(r5_stack, DB_A, "r5out-1", 1, timeout=90.0)
    status = await mgmt(sink, "GetSinkStatus", "td_sink")
    assert status["pending"] == 0
    assert status["failure"] is None


async def test_process_restart_same_durable_redelivers_unacked(
    r5_sink_factory, r5_stack, tmp_path: Path
) -> None:
    deployment = "r5-kill-restart"
    config_dir = _write_config(tmp_path, r5_stack, DB_A)
    sink_a = await _start_reliable(r5_sink_factory, config_dir, deployment_id=deployment)
    await wait_state(sink_a, "td_sink", "running")

    r5_stack.stop_container("tdengine")
    try:
        await publish(r5_stack, _rsample(POINT, 4.4, sample_id="r5kill-1"), "reliable")

        async def _failed() -> bool | None:
            status = await mgmt(sink_a, "GetSinkStatus", "td_sink")
            return True if status["state"] == "failed" else None

        await wait_until(_failed, timeout=15.0, interval=0.5, description="sink A failed")
    finally:
        sink_a.terminate()
        await asyncio.sleep(1.0)
        assert not sink_a.is_running()
        r5_stack.start_container("tdengine")
        await _wait_tdengine_ready(r5_stack)

    # 全新进程 + 相同 deployment_id → 绑定同名 durable，JetStream 重投未 ACK 消息。
    # --reliable-sink 即启动意图：新进程（全新 state-dir）绑定同名 durable 后
    # 直接进入 running；JetStream 向同 durable 重投未 ACK 的 r5kill-1。
    sink_b = await _start_reliable(r5_sink_factory, config_dir, deployment_id=deployment)
    await wait_state(sink_b, "td_sink", "running", timeout=30.0)
    await _wait_count(r5_stack, DB_A, "r5kill-1", 1, timeout=90.0)
    status = await mgmt(sink_b, "GetSinkStatus", "td_sink")
    assert status["pending"] == 0


async def test_backlog_while_stopped_delivered_on_start(
    r5_sink_factory, r5_stack, tmp_path: Path
) -> None:
    config_dir = _write_config(tmp_path, r5_stack, DB_A)
    sink = await _start_reliable(r5_sink_factory, config_dir, deployment_id="r5-stop-gap")
    await wait_state(sink, "td_sink", "running")

    stopped = await mgmt(sink, "StopSink", "td_sink")
    assert stopped["state"] == "stopped"
    for index in range(3):
        await publish(
            r5_stack,
            _rsample(POINT, 10.0 + index, sample_id=f"r5gap-{index}"),
            "reliable",
        )
    # 消费者已关闭，消息只进 JetStream；给服务端一点记账时间。
    await asyncio.sleep(1.0)
    assert _count(r5_stack, DB_A, "r5gap-%") == 0

    await mgmt(sink, "StartSink", "td_sink")
    await wait_state(sink, "td_sink", "running")
    await _wait_count(r5_stack, DB_A, "r5gap-%", 3, timeout=30.0)
    status = await mgmt(sink, "GetSinkStatus", "td_sink")
    assert status["pending"] == 0


async def test_two_durables_isolated_and_clear_skips_own_backlog(
    r5_sink_factory, r5_stack, tmp_path: Path
) -> None:
    cfg_a = _write_config(tmp_path / "a", r5_stack, DB_A)
    cfg_b = _write_config(tmp_path / "b", r5_stack, DB_B)
    sink_a = await _start_reliable(r5_sink_factory, cfg_a, deployment_id="r5-multi-a")
    sink_b = await _start_reliable(r5_sink_factory, cfg_b, deployment_id="r5-multi-b")
    await wait_state(sink_a, "td_sink", "running")
    await wait_state(sink_b, "td_sink", "running")

    # 两个 durable 各自独立收到全量 subject 并分别落不同库。
    await publish(r5_stack, _rsample(POINT, 5.5, sample_id="r5live-1"), "reliable")
    await _wait_count(r5_stack, DB_A, "r5live-1", 1)
    await _wait_count(r5_stack, DB_B, "r5live-1", 1)

    await mgmt(sink_a, "StopSink", "td_sink")
    await mgmt(sink_b, "StopSink", "td_sink")
    await wait_state(sink_a, "td_sink", "stopped")
    await wait_state(sink_b, "td_sink", "stopped")
    for index in range(2):
        await publish(
            r5_stack,
            _rsample(POINT, 20.0 + index, sample_id=f"r5clear-{index}"),
            "reliable",
        )
    await asyncio.sleep(1.5)

    # ClearSink 只重置 A 自己的 durable 进度（B 的积压原封不动）。
    cleared = await mgmt(sink_a, "ClearSink", "td_sink")
    assert int(cleared["skipped"]) >= 2

    await mgmt(sink_a, "StartSink", "td_sink")
    await mgmt(sink_b, "StartSink", "td_sink")
    await wait_state(sink_a, "td_sink", "running")
    await wait_state(sink_b, "td_sink", "running")

    await _wait_count(r5_stack, DB_B, "r5clear-%", 2, timeout=30.0)
    # A 的积压被显式跳过；留足重投窗口后仍为 0，证明不是"落得慢"。
    await asyncio.sleep(5.0)
    assert _count(r5_stack, DB_A, "r5clear-%") == 0
    # ClearSink 不删除目标侧既有数据。
    assert _count(r5_stack, DB_A, "r5live-1") == 1
    assert _count(r5_stack, DB_B, "r5live-1") == 1
