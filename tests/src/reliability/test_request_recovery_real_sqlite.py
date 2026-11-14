"""崩溃恢复保守裁决测试（Commander v1_22 §9.5）——真实 SQLite，禁纯 Mock。

模拟进程崩溃的方式是丢弃全部内存态（BufferedJournal/CommandControl/
QueuedExecutor 不再可达），在同一数据库文件上新建 EventRepository——
文件锁释放、WAL 重放、连接重开均为真实行为。恢复路径不构造任何现场
会话，因此结构上不可能产生重发。

覆盖任务书语义项：
- ⑨ 缺持久 T2/T3 证据的 ACCEPTED 命令（含仅 ACCEPTED 与有 T2 无 T3
  两种形态）重开后一律保守 UNKNOWN/UNKNOWN，绝不误判 NOT_SENT；
- 恢复幂等：二次 recover() 为空；
- 已持久 COMPLETED 的命令不受恢复影响，终态保持；
- 持久 request_id 索引在重开后仍可按 (caller, request_id) 聚合回查。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from commander.domain.command import (
    CommandOutcome,
    CommandSpec,
    CommandState,
)
from commander.infrastructure.persistence.event_repository import EventRepository
from tests.commander.seed_events import seed_accept, seed_transition

pytestmark = [pytest.mark.reliability, pytest.mark.contract]


def _open_repo(writer: ThreadPoolExecutor, path: Path) -> EventRepository:
    return writer.submit(lambda: EventRepository(path, retention_seconds=1800)).result()


def test_unfinished_commands_recover_conservatively_after_reopen(tmp_path: Path) -> None:
    """⑨：仅 ACCEPTED 与 EXECUTING 无 T3 两种形态，重开后均 UNKNOWN/UNKNOWN。"""
    db_path = tmp_path / "recovery.db"
    writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="src-recover")
    try:
        repo = _open_repo(writer, db_path)
        spec = CommandSpec("WT01", "setpoint_power", 12.5, "ref-v1")
        # 形态一：仅 T1 ACCEPTED，无任何 T2/T3 证据（崩溃于落盘后、调度前）。
        accepted_only = writer.submit(
            lambda: seed_accept(
                repo,
                [spec],
                caller_id="ops-console",
                request_id=f"req-rec-accept-{uuid4().hex}",
            )
        ).result()
        # 形态二：有 T2 EXECUTING 但无 COMPLETED（崩溃于现场交互中途）。
        executing = writer.submit(
            lambda: seed_accept(
                repo,
                [CommandSpec("WT01", "setpoint_power", 7.5, "ref-v1")],
                caller_id="ops-console",
                request_id=f"req-rec-exec-{uuid4().hex}",
            )
        ).result()
        writer.submit(
            lambda: seed_transition(repo, executing[0].command_id, CommandState.EXECUTING)
        ).result()
        # 对照组：完整 T2/T3 的命令必须保持原终态，不被恢复触碰。
        finished = writer.submit(
            lambda: seed_accept(
                repo,
                [CommandSpec("WT01", "setpoint_power", 3.0, "ref-v1")],
                caller_id="ops-console",
                request_id=f"req-rec-done-{uuid4().hex}",
            )
        ).result()
        writer.submit(
            lambda: seed_transition(repo, finished[0].command_id, CommandState.EXECUTING)
        ).result()
        writer.submit(
            lambda: seed_transition(
                repo,
                finished[0].command_id,
                CommandState.SUCCEEDED,
                outcome=CommandOutcome.CONFIRMED_SUCCESS,
            )
        ).result()
        writer.submit(repo.close).result()

        # “进程重启”：全新仓储对象重开同一数据库，全部内存态已丢失。
        reopened = _open_repo(writer, db_path)
        try:
            recovered = set(writer.submit(reopened.recover).result())
            assert recovered == {
                accepted_only[0].command_id,
                executing[0].command_id,
            }
            for command_id in recovered:
                result = writer.submit(reopened.get, command_id).result()
                assert result is not None
                # 缺证据一律 UNKNOWN：保守裁决禁止把“不知道”虚构成 NOT_SENT。
                assert result.state is CommandState.UNKNOWN
                assert result.outcome is CommandOutcome.UNKNOWN
            intact = writer.submit(reopened.get, finished[0].command_id).result()
            assert intact is not None
            assert intact.state is CommandState.SUCCEEDED
            assert intact.outcome is CommandOutcome.CONFIRMED_SUCCESS

            # 恢复幂等：第二次重放恢复不产生新裁决。
            assert writer.submit(reopened.recover).result() == ()

            # 持久 request_id 索引在重开后仍支持聚合回查（语义⑧的持久基础）。
            view = writer.submit(
                reopened.get_request,
                accepted_only[0].request_id,
                "ops-console",
            ).result()
            assert [item.command.command_id for item in view] == [accepted_only[0].command_id]
            assert view[0].state is CommandState.UNKNOWN
        finally:
            writer.submit(reopened.close).result()
    finally:
        writer.shutdown(wait=True)
