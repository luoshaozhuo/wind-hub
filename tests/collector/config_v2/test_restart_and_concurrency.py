"""Service 重启边界与多写入方并发语义（任务书第二轮点名项）。"""

from __future__ import annotations

import asyncio

import pytest

from collector.application.config_service import CollectorConfigService
from collector.application.reload import ConfigDiff
from collector.infrastructure.config.loader import load_collector_config
from collector.infrastructure.config.transactions import (
    CollectorConfigTransactions,
    ConfigSelector,
    RevisionConflictError,
)
from core.infrastructure.config.collector_documents import CollectorDocumentLoader
from core.infrastructure.config.yaml import read_yaml_mapping
from tests.collector.config_v2.config_tree import ConfigTree  # noqa: E402

pytestmark = pytest.mark.unit


class _NoopRuntime:
    async def reconfigure(self, new_config, diff: ConfigDiff):  # noqa: ANN001
        return []


async def test_runtime_field_change_requires_restart_and_does_not_apply(
    config_tree: ConfigTree,
) -> None:
    source = CollectorDocumentLoader(config_tree.paths)
    svc = CollectorConfigService(
        _NoopRuntime(),  # type: ignore[arg-type]
        load_collector_config(source),
        load_config=lambda: load_collector_config(source),
        config_digest=lambda: source.load().revision,
        config_hash=source.load().revision,
    )
    old_value = svc.current_config.runtime.thread_pool_max_workers
    service = config_tree.read("services/collector.yaml")
    service["runtime"]["thread_pool"]["max_workers"] = old_value + 16
    config_tree.write("services/collector.yaml", service)

    result = await svc.reload()
    assert not result.success
    assert any("restart" in item for item in result.errors)
    # 被拒绝后生效配置仍是旧值。
    assert svc.current_config.runtime.thread_pool_max_workers == old_value


async def test_independent_writers_with_stale_revision_are_rejected(
    config_tree: ConfigTree, documents
) -> None:
    """独立事务对象（近似跨进程）：A 提交后，B 持旧版本串行提交仍被拒。

    replace() 在写锁内重新读取磁盘并比对哈希；进程内并发由 asyncio.Lock
    串行化。跨进程的 check-then-write 非原子窗口（TOCTOU）无文件锁保护，
    列为 D-CONFIG-4 假设项；常规顺序写不会丢更新。
    """
    writer_a = CollectorConfigTransactions(config_tree.paths, documents)
    writer_b = CollectorConfigTransactions(config_tree.paths, documents)
    selector = ConfigSelector("INSTANCE", "devices")
    base, revision = writer_a.read(selector)

    def candidate(port: int) -> dict:
        doc = dict(base)
        doc["devices"] = {k: dict(v) for k, v in doc["devices"].items()}
        doc["devices"]["WT01"]["endpoint"]["port"] = port
        return doc

    await writer_a.replace(selector, candidate(1801), expected_revision=revision, request_id="A")
    with pytest.raises(RevisionConflictError):
        await writer_b.replace(
            selector, candidate(1802), expected_revision=revision, request_id="B"
        )
    assert (
        read_yaml_mapping(config_tree.devices_yaml)["devices"]["WT01"]["endpoint"]["port"] == 1801
    )


async def test_concurrent_replacers_serialize_within_one_event_loop(
    config_tree: ConfigTree, documents
) -> None:
    """同进程内 N 个并发替换：恰好一个成功，其余全部版本冲突。"""
    store = CollectorConfigTransactions(config_tree.paths, documents)
    selector = ConfigSelector("INSTANCE", "devices")
    base, revision = store.read(selector)

    async def replace(port: int, request_id: str):
        doc = dict(base)
        doc["devices"] = {k: dict(v) for k, v in doc["devices"].items()}
        doc["devices"]["WT01"]["endpoint"]["port"] = port
        return await store.replace(selector, doc, expected_revision=revision, request_id=request_id)

    results = await asyncio.gather(
        *(replace(1900 + i, f"req-{i}") for i in range(8)),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, RevisionConflictError) for item in results) == 7
