# Agent 反馈与报告归档规则

## 1. 规则定位

本规则定义 agent 反馈、主会话反馈和报告归档格式。测试分类、生命周期阶段和 NOT_RUN 原因以测试规则为准；本规则只规定反馈必须呈现哪些信息。

默认使用项目主要语言反馈；用户明确指定其他语言时除外。

## 2. Agent result 格式

所有 subagent 使用统一格式：

```text
Agent result:
- agent:
- files read:
- files changed:
- rules used:
- skills used:
- commands run:
- passed:
- failed:
- not-run:
- evidence:
- risk:
- next handoff suggestion:
```

字段要求：

1. `passed` 记录已执行且通过的检查或测试。
2. `failed` 记录已执行且失败的检查或测试。
3. `not-run` 记录未执行项，并说明原因。
4. 不使用未定状态字段。
5. 测试框架 skip 不单独作为结果字段；未执行时归入 `not-run`。

`next handoff suggestion` 可按任务需要使用简短标签，例如 coding、test、validation、fix、cleanup、documentation、requirement、architecture、security、performance、deployment、operation、regression。标签不是封闭枚举；重点是说明后续动作、触发条件和风险。

## 3. 主会话反馈

未生成报告文件时，主会话反馈使用以下结构：

```text
修改文件:
- ...

行为变化:
- ...

Agent 使用:
- code-implementer:
- test-validator:
- project-steward:

验证范围:
- 阶段:
- 来源:
- 命令:

检查与测试:
- passed:
- failed:
- not-run:

project_tree:
- 已更新 / 无需更新 / 未更新原因:

报告:
- 无 / 路径:

是否收口:
- 是/否:
- 理由:

剩余风险:
- ...

下一步:
- ...
```

已生成报告文件时，主会话只需给出：

```text
报告:
- <报告路径>

是否收口:
- 是/否:

关键结果:
- passed:
- failed:
- not-run:

下一步:
- ...
```

窗口反馈不得与报告结论不一致，不应重复报告全文。

## 4. 报告输出位置

任务报告默认输出到：

```text
ai_shared/reports/
```

如果项目另有报告目录，以任务 handoff 或仓库规则为准。报告必须使用 `.md`，不得把任务报告写入长期规则、需求文档或目录树。

## 5. 报告命名

报告文件名使用小写英文、数字、下划线，推荐格式：

```text
<scope>_<topic>_<round_or_date>.md
```

避免使用：

```text
report.md
final.md
new.md
临时报告.md
未命名.md
```

## 6. 报告正文结构

报告应包含以下信息，可按任务裁剪：

```markdown
# <标题>

> 日期:
> 范围:
> 状态:
> 验证范围:

## 1. 总览

| 项 | 结果 |
|---|---|

## 2. 修改文件

| 文件 | 操作 | 说明 |
|---|---|---|

## 3. 行为变化

- ...

## 4. 检查与测试

| 命令/检查 | 结果 | 未执行原因 | 说明 |
|---|---|---|---|

## 5. 验证覆盖

| 对象 | 阶段 | 来源 | 结果 | 说明 |
|---|---|---|---|---|

## 6. project_tree / 规则

- project_tree:
- rules:

## 7. 剩余风险

- ...

## 8. 下一步建议

- ...
```

## 7. 命令结果写法

命令和检查结果使用真实状态：

```text
PASS
FAIL
NOT_RUN: <原因>: <详情>
```

未执行命令必须写 `NOT_RUN`，不得写成 PASS。

## 8. 禁止事项

1. 不粘贴大段日志。
2. 不把未执行命令写成通过。
3. 不把 mock、fake、stub、health check、脚本存在、单文件通过写成真实闭环。
4. 不重复粘贴完整规则、完整 diff 或完整测试日志。
5. 不创建与本规则冲突的第二套报告格式。
