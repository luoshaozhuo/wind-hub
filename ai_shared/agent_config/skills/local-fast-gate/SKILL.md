---
name: local-fast-gate
description: Run the local Fast Gate only when the current change set contains product code that maps to GitHub CI Fast.
---

# Local Fast Gate

先确认变更范围包含产品代码。纯 rules/docs/Agent/CI-tooling 变更不得调用本 Skill。

执行：

```bash
python3 scripts/dev.py env --frontend
python3 scripts/dev.py python scripts/ci_gate.py fast
```

可按实际范围只执行 backend/frontend part。任一相关必需项失败为 `FAIL`。

如果发现失败，先判断相关性：

- `RELATED`：修复并重跑；
- `UNRELATED`：记录基线证据后忽略；
- `UNKNOWN`：最小定位后再决定。

不自动升级到 PR/release/qualification。
