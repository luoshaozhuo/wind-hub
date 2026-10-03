# Claude Code / Codex 执行入口

默认使用中文。Claude Code 与 Codex 作为 VS Code 插件或其他入口运行时，共用本仓库的规则、Skill 和环境检查。

## 1. 环境

首次执行仓库任务前：

```bash
python3 scripts/dev.py env
```

前端任务：

```bash
python3 scripts/dev.py env --frontend
```

本机工具路径可配置在不入库的 `.agent/local.json`，模板为
`.agent/local.example.json`。不假设 VS Code 插件继承某个交互式 shell 或 conda 环境。

`.env.local` 只用于运行时、真实服务或测试参数，不负责选择 Python 解释器；不得读取或输出其中的敏感值。

## 2. 规则入口

公共治理只维护在：

```text
ai_shared/rules/
ai_shared/agent_config/skills/
ai_shared/agent_config/hooks/
```

`.claude/`、`.codex/`、`.agents/` 只是工具适配层。

任务开始读取：

```text
ai_shared/rules/routing.md
```

随后仅按 routing 读取需要的规则、真实源码、测试、配置和 schema。

## 3. 开发生命周期

独立编码任务默认遵循：

```text
origin/main
→ task branch
→ code
→ local-fast-gate
→ commit(s)
→ local-pr-gate
→ push
→ PR → main
→ GitHub ci-fast + ci-pr
→ ci-fix-loop（失败时）
→ merge
```

要求：

1. 不直接在 `main` 开发或 push。
2. 一个独立任务一个短生命周期分支，一个分支可多个 commit。
3. `local-fast-gate` 在每轮完整编码阶段结束后默认执行。
4. Release 与真实资格验证只在对应阶段执行。
5. GitHub CI 结果必须绑定当前 branch HEAD SHA。
6. 不 force push，不覆盖用户已有修改，不用测试放宽制造 PASS。

## 4. 证据

mock/fake、skip、health check、脚本存在、局部通过、旧 SHA 的 CI 结果均不能冒充当前真实 Gate PASS。
