---
name: validate
description: Explicitly validate callers, dependents, contracts, and wiring affected by staged Git changes; read-only and narrower than a full-repository test.
disable-model-invocation: true
argument-hint: "[optional impact focus]"
---

# validate

本 skill 只能由用户显式调用。

1. 当前主会话以只读方式直接执行验证阶段，不启动或委派独立 agent。
2. 以 staged diff 为唯一变更起点，排除 unstaged 和 untracked 内容。
3. 从变更的符号和契约扩大到真实 import、调用方、实现类、上下游契约、配置/schema 消费方和装配路径。
4. 执行直接测试、受影响测试及影响范围内的 syntax/compile、lint、type-check 和必要专项门禁。
5. 对 staged 范围执行 `python3 ai_shared/agent_config/hooks/docstring-cn-gate.py --scope staged` 和 `ai_shared/agent_config/hooks/no-source-lab-import-gate.sh staged`。
6. 不修改源码、测试或文档，不自动扩大为全仓测试。
