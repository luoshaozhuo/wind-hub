# 规则读取路由

## 1. 所有 agent 必读

```text
ai_shared/rules/routing.md
```

## 2. code-implementer

必须读取：

```text
ai_shared/rules/coding.md
```

必要时读取：

```text
ai_shared/rules/testing.md
ai_shared/rules/validation-routing.md
ai_shared/rules/quality-gate.md
ai_shared/rules/python-docstring-cn.md
ai_shared/rules/documentation.md
ai_shared/rules/reporting.md
```

升级为“必要时读取”的典型触发：
1. 修改 public interface、跨模块契约、schema、配置或环境变量契约。
2. 涉及权限、审计、事务、并发、重试、租约、回滚等运行时语义。
3. 用户显式要求执行 `/test`、`/validate`、`/test-all` 或质量检查。

说明：`python-docstring-cn.md` 是历史文件名，当前语义为“通用注释与文档注释规则”，不再是 Python 专用规则。

## 2a. 前端编码任务（涉及 Web UI 或设计系统修改）

当任务涉及以下任一项时，优先适用本规则集：
- 修改 `src/wind-hub-admin/` 下的代码；
- 新增或修改 Web UI 页面、组件、样式或设计系统；
- 修改 `.ts`、`.tsx`、`.vue`、`.css`、`.html` 等前端资源。

必须读取（按此顺序）：

```text
ai_shared/rules/frontend.md
ai_shared/rules/coding.md
```

必要时读取：

```text
ai_shared/rules/testing.md
ai_shared/rules/quality-gate.md
ai_shared/rules/python-docstring-cn.md
ai_shared/rules/validation-routing.md
ai_shared/rules/documentation.md
```

说明：`frontend.md` 置于 `coding.md` 之前，使设计系统和 UI 原则优先于通用编码规则生效。
前端任务若仅涉及文案、样式微调或局部 UI 排版，默认按轻量路径执行；命中上文“升级触发”再加载额外规则。

## 3. test-validator

必须读取：

```text
ai_shared/rules/testing.md
ai_shared/rules/validation-routing.md
ai_shared/rules/quality-gate.md
ai_shared/rules/python-docstring-cn.md
```

必要时读取：

```text
ai_shared/rules/coding.md
```

## 4. project-steward

必须读取：

```text
ai_shared/rules/documentation.md
ai_shared/rules/reporting.md
```

如涉及规则更新，必须读取：

```text
ai_shared/rules/coding.md
ai_shared/rules/python-docstring-cn.md
ai_shared/rules/quality-gate.md
ai_shared/rules/validation-routing.md
```

如涉及需求状态，必须结合 `requirement-trace` skill。
如涉及 project_tree 更新，必须结合 `project-tree-update` skill。
如涉及规则体系变化，必须结合 `rule-update` skill。

说明：

```text
1. project_tree 读取是普通导航规则，不再单独设置 project-tree-read skill。
2. 报告归档是 project-steward 常规职责，不再单独设置 report-archive skill。
3. 用户反馈归档归入 reporting.md，不再设置 feedback-archive skill。
```

## 5. 上下文节省

默认不读取全部项目说明、全部 reports、完整 project_tree 或全仓源码。

`project_tree.md` 只用于导航，不能替代二次读取真实源码、测试、配置和 schema。
