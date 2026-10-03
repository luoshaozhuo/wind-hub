# 通用编码规则

## 1. 基本原则

1. 优先清晰、直接、可读；沿用当前模块的架构和语言惯例。
2. 不为单一调用点增加无必要抽象，不为历史行为增加无需求兼容层。
3. 只修改完成当前任务所需的文件；不做无关格式化和顺手重构。
4. public interface、schema、配置、RPC、CLI、消息格式均属于稳定契约，修改时同步处理调用方与测试。
5. 外部系统差异隔离在 adapter / driver / repository / gateway / client 等边界。
6. 异常不得静默吞掉；转换异常时保留异常链，资源必须有关闭、释放或取消路径。
7. 并发、重试、超时、幂等、事务、回滚、lease/fencing 等关键运行时语义必须显式。
8. 不引入未经任务需要的新依赖，不把测试、诊断或实验模块接入生产路径。

## 2. Python 工程约束

1. Python 代码遵守项目 Ruff、mypy strict 与 import-linter 配置。
2. public function / method / class / Protocol 等使用明确类型；稳定多字段数据优先使用明确模型而非松散 dict。
3. `async def` 中不得直接执行长期阻塞 I/O 或 CPU 密集操作。
4. 捕获 `asyncio.CancelledError` 时完成必要清理后继续抛出，除非契约明确消费取消。
5. 共享可变状态必须有明确并发保护策略。
6. 自动生成代码遵循生成器，不进行人工风格修补。

## 3. 配置与接口

1. 不凭记忆推断配置、schema、ORM 或 RPC 字段，修改前读取真实定义。
2. 配置变化必须处理默认值、解析、兼容性和测试；危险写入能力不得默认开启。
3. public API 必须明确输入、输出、错误、side effect、幂等性和资源生命周期。
4. 进程间边界遵循现有 RPC 契约，不通过直接 import 绕过架构边界。

## 4. Git / PR 开发流程

一个独立任务使用一个短生命周期分支；一个分支可以包含多个 commit。

标准流程：

```text
最新 origin/main
→ 创建任务分支
→ 编码
→ local-fast-gate
→ commit（可多次）
→ local-pr-gate
→ push
→ PR → main
→ GitHub ci-fast + ci-pr
→ CI 失败时 ci-fix-loop
→ PASS 后 merge
→ 删除任务分支
```

要求：

1. 禁止直接在 `main` 开发或直接 push `main`。
2. 创建任务分支前确认远端 `main` 最新 HEAD。
3. commit / push 前确认当前分支和远端 HEAD，禁止 force push。
4. 不使用 `reset --hard`、`clean`、任意覆盖用户未提交修改的操作。
5. GitHub CI 结果必须绑定具体 commit SHA；旧 SHA 的 PASS 不能证明新 SHA。
6. merge 由 PR 完成，`main` 表示已通过合并门禁的基线。

## 5. 验证原则

1. 每轮完整编码阶段结束后默认执行 `local-fast-gate`，不是每修改一个文件就执行。
2. PR 前执行 `local-pr-gate`；Release 前执行 `local-release-gate`。
3. hardware / performance / soak 只在明确需要且环境满足时执行 `local-qualification`。
4. 不通过删测试、放宽断言、扩大 skip/xfail 或关闭静态检查制造 PASS。
5. 未执行、mock/fake、局部通过均不得表述成真实环境 PASS。

## 6. 文档与范围

1. 代码内 docstring / 注释按 `comments.md` 同步维护。
2. README、docs、设计报告等独立文档仅在任务明确要求或当前修改使其事实陈旧时修改。
3. 不维护不存在的 project tree、reporting 或 requirement-trace 体系。
