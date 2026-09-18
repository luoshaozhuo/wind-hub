# 规则读取路由

## 1. 所有 agent 必读

```text
ai_shared/rules/routing.md
```

## 2. code-implementer

必须读取：

```text
ai_shared/rules/coding.md
ai_shared/rules/testing.md
ai_shared/rules/validation-routing.md
ai_shared/rules/quality-gate.md
ai_shared/rules/python-docstring-cn.md
```

必要时读取：

```text
ai_shared/rules/documentation.md
ai_shared/rules/reporting.md
```

说明：`python-docstring-cn.md` 是历史文件名，当前语义为“通用注释与文档注释规则”，不再是 Python 专用规则。

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
