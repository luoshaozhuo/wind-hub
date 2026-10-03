---
name: local-pr-gate
description: Run the local counterpart of GitHub CI PR Gate before creating or updating a pull request for merge into main.
---

# Local PR Gate

## 执行

要求当前任务已通过 `local-fast-gate`，然后：

```bash
python3 scripts/dev.py env --frontend
python3 scripts/dev.py python scripts/ci_gate.py pr
```

规则：

1. 与 GitHub `ci-pr.yml` 共用 `scripts/ci_gate.py`。
2. 覆盖 integration、system smoke 和 frontend Playwright。
3. 真实软件服务不可用时为 `NOT_EXECUTED`，不得退化成 mock。
4. 失败不得进入准备合并状态。
5. 报告绑定当前 branch HEAD SHA。
