"""configs/ 历史归档中已被后继修复的畸形候选。

`configs/` 是不可变历史归档，其中个别早期候选包含重复 YAML 键（严格解析器
按安全策略拒绝重复键）。这些候选从未发布（台账
``docs/audit/config_version_release_ledger_20261123.csv`` 标为
UNPUBLISHED_CANDIDATE），并由干净后继 ``..._v1.1.2`` 取代；活动根
``deploy/config_roots/*`` 只选择后继版本。因此它们不参与"当前仓库可发布文件"
的整目录扫描，其拒绝行为由 ``test_yaml_acceptance`` 单独断言，避免让畸形历史
文件阻塞整目录发布验收。
"""

from __future__ import annotations

SUPERSEDED_MALFORMED = frozenset(
    {
        "services/service_gateway-e2e-source_v1.1.0.yaml",
        "services/service_gateway-e2e-source_v1.1.1.yaml",
    }
)


def is_superseded_malformed(relative_path: str) -> bool:
    """判断相对路径是否属于默认排除的畸形历史候选。"""
    return relative_path in SUPERSEDED_MALFORMED
