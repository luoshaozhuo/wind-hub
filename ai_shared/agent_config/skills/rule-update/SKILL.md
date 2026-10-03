---
name: rule-update
description: Use when rules, skills, hooks or agent adapters must be changed while preserving one authoritative source and minimal governance surface.
---

# Rule Update

## 目的

保持 Coding Agent 治理结构最小、无重复、无死引用。

## 原则

1. Rule 管长期原则；Skill 管完整可执行流程；Hook 管确定性的机械安全限制；CI 管独立环境验证。
2. 同一规则只有一个权威来源，其他位置只引用。
3. 无独立闭环的 Skill 应合并或删除。
4. 不创建不存在的 templates、memory/project_tree、reporting 或 requirement tracking 依赖。
5. 工具适配层 `.claude/`、`.codex/`、`.agents/` 不复制业务规则。
6. 删除或重命名文件前搜索全部引用并同步更新。
7. 不为历史兼容保留无使用者的 wrapper。

## 输出

说明新增、删除、重命名和迁移风险即可，不维护额外治理报告。
