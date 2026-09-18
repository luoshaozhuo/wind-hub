---
name: test
description: Explicitly test only code directly modified by staged Git changes; may add or update direct tests but must not expand to callers or the whole repository.
disable-model-invocation: true
argument-hint: "[optional direct scope]"
---

# test

本 skill 只能由用户显式调用。

1. 当前主会话直接执行测试阶段，不启动或委派独立 agent。
2. 以 Git index 为唯一范围基线，使用 staged diff 作为范围起点，排除 unstaged 和 untracked 内容。
3. 仅为 staged 内容直接修改的函数、类、模块、配置、schema 和行为新增或修改测试。
4. 只运行直接对应测试和支持它们的最小语法检查。
5. 不扩大到调用方、依赖方、跨模块或全仓范围，不自动触发 `validate` 或 `test-all`。
6. 发现生产实现问题时停止并报告需要返回编码阶段。
