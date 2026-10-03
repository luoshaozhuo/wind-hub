# 通用编码规则

## 1. 基本原则

1. 优先清晰、直接、可读；沿用当前模块架构和语言惯例。
2. 不为单一调用点增加无必要抽象，不为历史行为增加无需求兼容层。
3. 只修改当前任务所需文件，不做无关格式化和顺手重构。
4. public interface、schema、配置、RPC、CLI、消息格式均属于稳定契约，修改时同步处理调用方与测试。
5. 外部系统差异隔离在 adapter / driver / repository / gateway / client 等边界。
6. 异常不得静默吞掉；资源必须有关闭、释放或取消路径。
7. 并发、重试、超时、幂等、事务、回滚、lease/fencing 等运行时语义必须显式。
8. 不引入未经任务需要的新依赖，不把测试、诊断或实验模块接入生产路径。

## 2. Python 工程约束

1. 产品 Python 代码遵守 Ruff、mypy strict 与 import-linter。
2. public interface 使用明确类型；稳定多字段数据优先明确模型。
3. `async def` 中不得直接执行长期阻塞 I/O 或 CPU 密集操作。
4. 捕获 `asyncio.CancelledError` 时完成必要清理后继续抛出，除非契约明确消费取消。
5. 共享可变状态必须有明确并发保护策略。
6. 自动生成代码遵循生成器，不人工修补生成物风格。

## 3. 配置与接口

1. 不凭记忆推断配置、schema、ORM 或 RPC 字段，修改前读取真实定义。
2. 配置变化处理默认值、解析、兼容性和测试；危险写入能力不得默认开启。
3. public API 明确输入、输出、错误、side effect、幂等性和资源生命周期。
4. 进程间边界遵循现有 RPC 契约，不通过直接 import 绕过。

## 4. Git / PR

```text
origin/main
→ task branch
→ modify
→ 与变更范围匹配的本地验证
→ commit(s)
→ 含产品代码时 local-pr-gate
→ push
→ PR → main
→ GitHub scope-aware CI
→ RELATED CI failure 才进入 ci-fix-loop
→ merge
```

要求：

1. 不直接在 `main` 开发或 push。
2. 一个独立任务一个短生命周期分支。
3. commit/push 前确认当前分支与远端 HEAD；禁止 force push。
4. 不使用 reset --hard、clean 或覆盖用户修改的操作。
5. CI 证据绑定具体 SHA。
6. main 只通过 PR merge 更新。

## 5. 验证边界

1. **只有产品代码变更默认进入产品代码 Gate。**
2. CI/Agent 工具修改只验证工具自身，除非同时修改产品代码。
3. rules/skills/docs 等非代码治理修改不运行产品 pytest、build、Playwright、integration/system。
4. 混合变更按真实受影响范围执行对应 Gate。
5. CI FAIL 先判定 `RELATED / UNRELATED / UNKNOWN`；只有 `RELATED` 必须修复。
6. 可证明的 `UNRELATED` 既有失败只记录，不扩大本次任务。
7. 不通过删测试、放宽断言、扩大 skip/xfail 或关闭检查制造 PASS。

## 6. 文档与范围

1. 代码内 docstring / 注释按 `comments.md`。
2. README/docs 仅在任务明确要求或事实已因当前修改陈旧时更新。
3. 不维护不存在的 project tree、reporting 或 requirement-trace 体系。
