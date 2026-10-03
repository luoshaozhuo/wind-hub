# Claude Code / Codex 执行入口

默认使用中文。Claude Code 与 Codex 作为 VS Code 插件或其他入口运行时，共用本仓库规则、Skill 和环境检查。

## 1. 环境

首次执行仓库任务前：

```bash
python3 scripts/dev.py env
```

前端产品任务再检查 frontend 工具链：

```bash
python3 scripts/dev.py env --frontend
```

本机工具路径配置在已入库的 `ai_shared/agent_config/local.json`；不假设 VS Code 插件继承 shell 或 conda 环境。

`.env.local` 只用于运行时、真实服务或测试参数，不负责 Python 环境；不得读取或输出敏感值。

## 2. 规则入口

公共治理只维护在：

```text
ai_shared/rules/
ai_shared/agent_config/skills/
ai_shared/agent_config/hooks/
```

`.claude/`、`.codex/`、`.agents/` 仅为适配层。

任务开始读取 `ai_shared/rules/routing.md`，随后按 routing 最小读取。

## 3. 开发生命周期

```text
origin/main
→ task branch
→ 修改
→ 按变更范围验证
→ commit(s)
→ push / PR
→ scope-aware GitHub CI
→ 仅处理 RELATED CI failure
→ merge
```

产品代码默认进入 Local Fast Gate；含产品代码的 PR 进入 Local PR Gate。

纯 CI/Agent 工具、rules、skills、docs、README 等非产品代码变更不运行产品代码 Gate，只做与修改对象直接相关的轻量验证。

## 4. CI 失败

CI 状态与失败相关性分开记录。可证明与本次变更无关的既有失败标记 `UNRELATED` 并忽略，不为了全绿扩大任务范围。证据不足时标记 `UNKNOWN`，只做最小诊断。

## 5. Git

不直接在 main 开发或 push；一个独立任务一个短分支；禁止 force push、覆盖用户修改和危险历史重写。
