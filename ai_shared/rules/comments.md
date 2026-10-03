# 注释与 Docstring 规则

## 1. 原则

1. 注释解释职责、原因、边界、假设、风险和非显然行为，不复述代码。
2. Python 采用 PEP 257 基础约定和 Google Style Docstring。
3. 业务注释和 docstring 默认使用中文；第三方协议固定术语、代码标识符保留原文。
4. 测试文件不要求机械补齐每个测试函数 docstring；复杂 fixture、测试基础设施和非显然行为仍应说明。

## 2. 必须说明的对象

1. 生产模块应有简洁 module docstring，说明模块职责和关键边界。
2. public class / Protocol / dataclass / enum / function / method / API/CLI 入口在语义不自明时必须有 docstring。
3. 涉及协议、并发、事务、重试、回滚、资源生命周期、异常转换的 private helper 应说明关键约束。
4. 参数、返回值或异常语义不自明时使用 `Args` / `Returns` / `Raises`。

## 3. 禁止

1. 空泛文件头，如 “Utilities”“Helpers”“Tests for ...”。
2. 对简单赋值或显然控制流逐行解释。
3. 无解释的 `type: ignore`、`noqa` 等抑制。
4. 用注释掩盖错误设计或未实现行为。
5. 把 mock/fake 行为描述成真实生产能力。
