---
name: local-pr-gate
description: Run only targeted integration/system/E2E checks selected by the current product-code risk scope before PR merge.
---

# Local PR Gate

普通 L1 产品代码不需要额外重型测试；Fast Gate 即可。

先基于 `origin/main...HEAD` 获取变更文件并运行：

```bash
python3 scripts/ci_scope.py <changed-files...>
```

只在输出存在以下内容时执行：

- `backend_pr_targets` 非空：
  ```bash
  python3 scripts/dev.py python scripts/ci_gate.py pr --targets "<targets>"
  ```
- `frontend_e2e=true`：
  ```bash
  python3 scripts/dev.py python scripts/ci_gate.py frontend-e2e
  ```

规则：

1. Integration 仅运行 protocol / rpc / sinks 中命中的边界。
2. System 仅运行 acquisition / command / diagnostics / reload / startup / task-control / e2e 中命中的领域。
3. Playwright 只在当前 E2E 实际覆盖的应用外壳、Playwright 配置或 E2E 测试自身变化时运行。
4. 没有 PR target 时直接 `NOT_APPLICABLE`；不得为了“PR 必须测点什么”而运行无关测试。
5. 失败继续按 `RELATED / UNRELATED / UNKNOWN` 处理。
