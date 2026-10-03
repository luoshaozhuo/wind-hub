---
name: local-pr-gate
description: Run the local PR Gate only for pull requests that contain product code requiring integration, system, or frontend E2E validation.
---

# Local PR Gate

纯 rules/docs/Agent/CI-tooling PR 不执行本 Skill。

产品代码 PR 在对应 Fast Gate 通过后执行：

```bash
python3 scripts/dev.py env --frontend
python3 scripts/dev.py python scripts/ci_gate.py pr
```

按实际 backend/frontend 范围裁剪。真实服务不可用时为 `NOT_EXECUTED`，不得退化为 mock。

发现 FAIL 时先分类 `RELATED / UNRELATED / UNKNOWN`。只有 RELATED 失败阻断本次变更；可证明的既有无关失败记录后忽略。
