# Claude Code / Codex 执行入口

默认使用中文。Claude Code 与 Codex 是代码和仓库操作执行器，不主动扩展用户任务边界。

## 1. 环境

会话首次执行仓库命令前运行 `source .env.local`，确认 conda 环境为 `wind-hub`；否则停止。

## 2. 单一规则源与最小读取

公共规则只维护在：

```text
ai_shared/rules/
ai_shared/templates/
ai_shared/agent_config/skills/
ai_shared/agent_config/hooks/
```

`.claude/`、`.codex/`、`.agents/` 仅是工具适配层。默认只读
`ai_shared/rules/routing.md`、routing 指定规则、用户指定文件，以及当前任务涉及的真实源码、
测试、配置和 schema。`ai_shared/memory/project_tree.md` 只用于导航。

## 3. 独立执行阶段

| 阶段 | 触发 | 范围与写入边界 |
|---|---|---|
| 编码 | 普通修改请求 | 修改实现；不自动运行测试、lint、type-check 或回归 |
| 测试 | 用户显式 `/test` | staged diff 直接修改范围；可修改测试，原则上不改生产代码 |
| 验证 | 用户显式 `/validate` | 从 staged diff 扩展到真实影响范围；只读 |
| 全量测试 | 用户显式 `/test-all` | 当前完整工作区的常规检查；只读 |

Codex 对应使用 `$test`、`$validate`、`$test-all`。阶段互不自动串联，均由当前主会话直接执行。

`/test` 和 `/validate` 只以 `git diff --cached` 为变更起点：unstaged 与 untracked 不纳入范围；
mixed 文件必须标记 `MIXED_INDEX_FILE`。检查命令仍运行在当前完整工作区，因此结果可能受 unstaged
内容影响，报告必须披露。`/test-all` 不按 diff 裁剪；只有显式 `include-heavy` 才包含高成本项。

## 4. 不变量

1. public interface 应有清晰类型、签名或 schema；复杂边界应有解释原因和风险的必要注释。
2. 不允许无解释的类型/lint 抑制、裸 catch/except/rescue、静默吞异常或 fake OK。
3. 不通过降低断言、删除测试或扩大 skip 制造通过。
4. 检查结论必须来自真实命令、真实文件或明确 evidence。
5. skipped、mock、fake、health check、脚本存在、局部 passed、environment-pending 均不能写成真实通过。
6. 普通编码反馈不输出检查状态或例行检查命令。
7. 不把工具或实验模块引入生产路径。
8. 不自动执行 commit、push、reset、clean 或其他 Git/GitHub 写操作。
9. 规则、需求、报告、project_tree 和 heavy regression 只在用户明确要求时更新或执行。
