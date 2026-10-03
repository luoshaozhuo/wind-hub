# Coding Agent 路由

本文件只决定当前任务读取哪些规则、是否进入产品代码 Gate，不重复其他规则内容。

## 1. 默认入口

所有仓库任务读取：

```text
CLAUDE.md
ai_shared/rules/routing.md
```

随后按任务最小读取。

## 2. 先分类变更范围

以当前任务实际修改文件为准：

| 范围 | 典型路径 | 验证 |
|---|---|---|
| 产品后端代码 | `src/`（非 admin）、`tests/`、`configs/`、`pyproject.toml`、`poetry.lock` | `local-fast-gate`；PR 前 `local-pr-gate` |
| 前端产品代码 | `src/wind-hub-admin/` | Fast/PR Gate 的前端部分 |
| CI / Agent 工具 | `scripts/`、`.github/`、Hook/adapter | 只验证修改工具自身；不自动跑产品测试 |
| 规则 / Skill / 文档 | `ai_shared/`、`CLAUDE.md`、`AGENTS.md`、`docs/`、`README.md` | 引用、格式、结构检查；不跑产品代码 Gate |

混合变更取并集：只有实际包含产品代码时才进入对应产品 Gate。

## 3. 编码任务

### 后端

读取 `coding.md`；涉及测试设计再读 `testing.md`；涉及注释再读 `comments.md`。

完成产品代码阶段后执行 `local-fast-gate`。

### 前端

读取：

```text
ai_shared/rules/coding.md
ai_shared/rules/frontend.md
```

涉及测试设计再读 `testing.md`。完成产品代码阶段后执行对应 Fast Gate。

### CI / Agent 工具

只读取与该工具直接相关的规则和真实文件。验证限于修改对象自身，例如：

- Python 工具：parse/compile + Ruff；
- workflow：GitHub YAML/表达式结构与调用入口；
- Hook：输入解析、允许/拒绝路径和最小行为检查。

除非工具修改同时改变产品代码，否则不自动执行 pytest、前端 build、Playwright、integration/system。

### 规则 / Skill / 文档

规则体系修改使用 `rule-update`。不执行产品代码 Gate；只检查路径、引用、重复规则、失效文件和结构一致性。

## 4. 生命周期节点

| 场景 | Skill |
|---|---|
| 产品代码阶段完成 | `local-fast-gate` |
| 含产品代码的 PR 准备合并 | `local-pr-gate` |
| Release / tag | `local-release-gate` |
| 硬件、性能、soak 资格验证 | `local-qualification` |
| GitHub CI 失败且与本次变更相关 | `ci-fix-loop` |
| rules / skills / hooks / agent 配置 | `rule-update` |

## 5. CI 失败相关性

CI 的运行状态与失败相关性是两个维度：

```text
状态：PASS / FAIL / RUNNING / QUEUED / NOT_RUN / NOT_EXECUTED
相关性：RELATED / UNRELATED / UNKNOWN
```

处理规则：

1. `RELATED`：本次变更涉及失败路径、依赖边界或执行入口；必须处理。
2. `UNRELATED`：可证明为既有失败，或失败范围与本次修改无依赖关系；记录证据后忽略，不阻断当前任务。
3. `UNKNOWN`：做最小定位；不能证明无关前不得擅自写成 `UNRELATED`。
4. 禁止为了让无关 CI 变绿而扩大任务范围。

## 6. 最小上下文原则

1. 不预读全部 rules、docs、tests 或源码。
2. Rule 管原则；Skill 管完整流程；Hook 管机械安全；CI 管独立环境验证。
3. 同一规则只有一个权威来源。
