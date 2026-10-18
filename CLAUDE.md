# Claude Code / Codex 执行入口

默认使用中文。Claude Code 与 Codex 作为 VS Code 插件或其他入口运行时，共用本仓库规则、Skill 和环境检查。

## 1. 环境

首次执行仓库任务前（包括计划、回答问题、编码等等）：

```bash
python3 scripts/dev.py env
```

前端产品任务再检查 frontend 工具链：

```bash
python3 scripts/dev.py env --frontend
```

`ai_shared/agent_config/activate-env.sh` 统一激活本机 `~/miniconda3` 下的 `wind-hub` 环境。
Claude Code 通过 SessionStart / `CLAUDE_ENV_FILE` 加载；Codex 通过 `.codex/config.toml` 的 `BASH_ENV` 加载。
不假设 VS Code 插件继承终端环境；`scripts/dev.py env` 检查当前解释器，不切换到其他 Python。

`.env.local` 只用于运行时、真实服务或测试参数，不负责 Python 环境；不得读取或输出敏感值。

如果检查当前环境不是 wind-hub 则报错，不执行后续工作。

## 2. 规则入口

公共治理只维护在：

```text
ai_shared/rules/
ai_shared/agent_config/skills/
ai_shared/agent_config/hooks/
```

`.claude/`、`.codex/`、`.agents/` 仅为适配层。

任务开始读取 `ai_shared/rules/routing.md`，随后按 routing 最小读取。普通编码任务的独立文档边界由 `ai_shared/rules/coding.md` 约束。

## 3. 开发生命周期

```text
origin/main → task branch → 开发 / 按需定向测试 → commit / push / PR
→ //merge-branch（一次受影响范围本地验证）→ squash merge main
```

开发、Commit、Push 和 PR 创建均不强制完整测试。GitHub Fast/PR 仅执行轻量治理检查；Release 与硬件、性能、长稳测试仍使用独立门禁。

## 4. CI 失败

CI 状态与失败相关性分开记录。可证明与本次变更无关的既有失败标记 `UNRELATED` 并忽略，不为了全绿扩大任务范围。证据不足时标记 `UNKNOWN`，只做最小诊断。

## 5. Git

不直接在 main 开发或 push；一个独立任务一个短分支；禁止 force push、覆盖用户修改和危险历史重写。
