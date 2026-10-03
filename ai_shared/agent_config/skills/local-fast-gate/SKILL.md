---
name: local-fast-gate
description: Run the local counterpart of GitHub CI Fast after a coding stage is complete. Use by default after code changes before considering the coding stage complete.
---

# Local Fast Gate

## 执行

本地固定使用 Agent 本机配置的工具链：

```bash
python3 scripts/dev.py env --frontend
python3 scripts/dev.py python scripts/ci_gate.py fast
```

规则：

1. 与 GitHub `ci-fast.yml` 共用 `scripts/ci_gate.py` 中的命令定义。
2. 任一必需项失败则为 `FAIL`，修复后重新执行。
3. 不自动升级到 integration/system/Playwright/hardware/performance/soak。
4. 只报告 `PASS`、`FAIL` 或明确的 `NOT_EXECUTED`；局部通过不得代替整个 Gate。
