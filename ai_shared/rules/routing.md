# Coding Agent 路由

本文件只决定当前任务读取哪些规则、以及需要哪一级验证。

## 1. 默认入口

所有仓库任务读取 `CLAUDE.md` 和本文件，随后按任务最小读取。

## 2. 变更与验证等级

| 等级 | 典型变更 | 验证 |
|---|---|---|
| 非产品 | rules/Agent/CI tooling | 只验证修改对象自身 |
| L1 | 普通内部实现、普通前端页面/样式 | 按需相关测试 |
| L2 | protocol/RPC/sink 等边界 | 按需 Contract/Integration |
| L3 | acquisition/command/diagnostics/reload/startup/task-control/跨系统 E2E | 按需 Integration/System |
| Frontend E2E | 当前 Playwright 实际覆盖的 App shell / E2E 配置与测试 | 按需 Frontend/E2E |

原则：测试按任务需要运行；合并不执行自动门禁或强制测试。

## 3. 编码任务

后端读取 `coding.md`；测试设计读取 `testing.md`；注释读取 `comments.md`。

前端读取：

```text
coding.md
frontend.md
```

开发、Commit、Push、合并均不强制运行产品 Gate；按需执行直接相关测试。`//merge-branch` 只负责直接 Squash Merge 与安全清理。

## 4. Scope 规则

`scripts/ci_scope.py` 只负责分类，不执行测试：

- `backend_fast`
- `frontend_fast`
- `backend_pr_targets`
- `frontend_e2e`
- `tooling`
- `governance`

后端 PR target 可为：

```text
integration-protocol
integration-rpc
integration-sinks
system-acquisition
system-command
system-e2e
```

没有 target 的 L1 产品变更不执行额外 integration/system/E2E。

## 5. 生命周期

| 场景 | Skill |
|---|---|
| 开发期间 | 按需运行定向检查，不强制 Gate |
| 直接合并 | `merge-branch`：无 PR、无合并门禁 |
| Release / tag | `local-release-gate` |
| hardware/performance/soak | `local-qualification` |
| 当前 SHA 的相关 CI 失败 | `ci-fix-loop` |
| rules/skills/hooks/Agent 配置 | `rule-update` |

## 6. CI 失败

状态和相关性分开：

```text
PASS / FAIL / RUNNING / QUEUED / NOT_RUN / NOT_EXECUTED
RELATED / UNRELATED / UNKNOWN
```

仅 `RELATED` 必须修复；`UNRELATED` 有证据后忽略；`UNKNOWN` 只做最小定位。
