# Coding Agent 路由

本文件只决定当前任务读取哪些规则、以及需要哪一级验证。

## 1. 默认入口

所有仓库任务读取 `CLAUDE.md` 和本文件，随后按任务最小读取。

## 2. 变更与验证等级

| 等级 | 典型变更 | 验证 |
|---|---|---|
| 非产品 | rules/docs/Agent/CI tooling | 只验证修改对象自身 |
| L1 | 普通内部实现、普通前端页面/样式 | Fast Gate |
| L2 | protocol/RPC/sink 等边界 | Fast + targeted Integration |
| L3 | acquisition/command/diagnostics/reload/startup/task-control/跨系统 E2E | Fast + targeted Integration/System |
| Frontend E2E | 当前 Playwright 实际覆盖的 App shell / E2E 配置与测试 | Fast + Playwright |

原则：**PR Gate 不是 PR 固定全量测试，而是 Fast 无法覆盖的风险增量验证。**

## 3. 编码任务

后端读取 `coding.md`；测试设计读取 `testing.md`；注释读取 `comments.md`。

前端读取：

```text
coding.md
frontend.md
```

产品代码完成后运行与范围匹配的 Fast Gate。只有 `scripts/ci_scope.py` 判断存在 PR target 时才运行 `local-pr-gate`。

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
system-diagnostics
system-reload
system-startup
system-task-control
system-e2e
```

没有 target 的 L1 产品变更不执行额外 integration/system/E2E。

## 5. 生命周期

| 场景 | Skill |
|---|---|
| 产品代码阶段完成 | `local-fast-gate` |
| scope 存在 PR target | `local-pr-gate` |
| Release / tag | `local-release-gate` |
| hardware/performance/soak | `local-qualification` |
| 当前 SHA 的相关 CI 失败 | `ci-fix-loop` |
| 任务分支经 PR 合并回 main | `merge-branch` |
| rules/skills/hooks/Agent 配置 | `rule-update` |

## 6. CI 失败

状态和相关性分开：

```text
PASS / FAIL / RUNNING / QUEUED / NOT_RUN / NOT_EXECUTED
RELATED / UNRELATED / UNKNOWN
```

仅 `RELATED` 必须修复；`UNRELATED` 有证据后忽略；`UNKNOWN` 只做最小定位。
