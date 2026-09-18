---
name: test-all
description: Explicitly run the repository's full regular automated checks against the entire current workspace; add expensive suites only when include-heavy is requested.
disable-model-invocation: true
argument-hint: "[include-heavy]"
---

# test-all

本 skill 只能由用户显式调用。

1. 当前主会话以只读方式直接执行全量测试阶段，不启动或委派独立 agent；是否包含高成本项由 `include-heavy` 参数决定。
2. 不根据 diff 裁剪范围，对当前整个工作区执行仓库定义的全仓 syntax/compile、lint、type-check 和全部常规自动化测试。
3. 默认 `include_heavy: false`，不执行性能、长稳、真实外部依赖或完整部署演练。
4. 用户明确传入 `include-heavy` 时，先列出高成本矩阵、预计耗时和环境依赖，再在已授权范围内执行。
5. 对全仓范围执行 `python3 ai_shared/agent_config/hooks/docstring-cn-gate.py --scope all` 和 `ai_shared/agent_config/hooks/no-source-lab-import-gate.sh all`。
6. 不修改源码、测试或文档。
