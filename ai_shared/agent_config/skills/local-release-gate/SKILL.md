---
name: local-release-gate
description: Run the local counterpart of the GitHub Release Gate before a release candidate or version tag is prepared.
---

# Local Release Gate

## 执行

```bash
python3 scripts/dev.py env --frontend
python3 scripts/dev.py python scripts/ci_gate.py release
```

规则：

1. 与 GitHub `ci-release.yml` 共用 `scripts/ci_gate.py`。
2. Backend 执行静态检查和常规全量分层测试。
3. Frontend 执行 build、Vitest、Playwright。
4. 排除 hardware/performance/soak；这些属于 qualification。
5. 失败不得生成 Release 候选结论，结果绑定当前 Git SHA。
