# Coding Agent 路由

本文件只决定当前任务需要读取哪些规则和执行哪个 Skill，不重复其他规则内容。

## 1. 默认入口

所有仓库任务先读取：

```text
CLAUDE.md
ai_shared/rules/routing.md
```

随后按任务最小读取。

## 2. 编码任务

### 后端 / Python / 配置 / RPC

读取：

```text
ai_shared/rules/coding.md
```

如果需要新增、修改或判断测试，再读取：

```text
ai_shared/rules/testing.md
```

涉及新增或修改 docstring / 注释时读取：

```text
ai_shared/rules/comments.md
```

编码阶段完成后执行 `local-fast-gate`。

### 前端

读取：

```text
ai_shared/rules/coding.md
ai_shared/rules/frontend.md
```

涉及测试设计时再读取 `testing.md`。

编码阶段完成后执行 `local-fast-gate`。

## 3. 生命周期节点

| 场景 | Skill |
|---|---|
| 编码阶段完成 | `local-fast-gate` |
| 准备创建或更新 PR | `local-pr-gate` |
| 准备 Release / tag | `local-release-gate` |
| 真实硬件、性能、soak 资格验证 | `local-qualification` |
| GitHub CI 失败诊断与修复 | `ci-fix-loop` |
| 修改 rules / skills / hooks / agent 配置 | `rule-update` |

## 4. 最小上下文原则

1. 不预读全部 rules、docs、tests 或源码。
2. 不存在的规则、模板、project tree 不得作为隐式依赖。
3. Rule 管原则；Skill 管完整工作流；Hook 管机械安全限制；CI 管独立环境验证。
4. 同一规则只保留一个权威来源，其他位置只引用，不复制。
