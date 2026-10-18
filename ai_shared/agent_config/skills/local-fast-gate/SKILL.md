---
name: local-fast-gate
description: Legacy optional focused checks; ordinary development does not require a Fast Gate.
---

# Local Fast Gate（可选诊断，不自动调用）

日常开发、Commit、Push 和 PR 创建不强制执行本 Skill。用户调用 `//merge-branch` 后才统一执行合并验收。

仅在用户明确要求定向诊断时调用，不是开发、提交或创建 PR 的强制步骤。

先获取 `origin/main...HEAD` 的变更文件并运行：

```bash
python3 scripts/ci_scope.py <changed-files...>
```

按输出执行：

- `backend_static=true`
  ```bash
  python3 scripts/dev.py python scripts/ci_gate.py fast --part backend-static
  ```
- `backend_fast_targets` 非空
  ```bash
  python3 scripts/dev.py python scripts/ci_gate.py fast --part backend-tests --targets "<targets>"
  ```
- `frontend_fast=true`
  ```bash
  python3 scripts/dev.py python scripts/ci_gate.py fast --part frontend
  ```
- 仅 CI/Agent Python tooling：只做 compile/Ruff，不进入产品测试。

典型裁剪：

- 后端生产源码 / 依赖：static + unit + component + contract；
- 只改 unit test：static + unit；
- 只改 component test：static + component；
- 只改 contract test：static + contract；
- 只改 configs：contract；
- 只改 Playwright E2E：不跑 Vitest/build，交给 PR E2E target；
- 普通前端源码：Vitest + build。

只运行与当前变更相关的 Fast 检查。
