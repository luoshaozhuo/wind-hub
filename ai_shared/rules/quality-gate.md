# 质量门禁规则

## 1. 变更范围门禁

每个 agent 开始工作前必须获取真实变更范围，不得只依赖上一个 agent 的口头说明。

## 2. 通用门禁

修改代码时，必须优先执行或说明未执行原因：

```text
1. 语法或编译检查；
2. lint / static analysis；
3. type-check（如果该语言或仓库启用）；
4. affected tests；
5. 与边界、安全、审计、配置、schema 相关的专项检查。
```

## 3. 语言门禁参考

实际命令以仓库配置为准：

```text
Python: py_compile/compileall, ruff/pylint, mypy/pyright, pytest
TypeScript/JavaScript: tsc --noEmit, eslint, npm/pnpm/yarn test
Go: go test ./..., go vet ./..., golangci-lint
Java/Kotlin: mvn/gradle test, checkstyle/spotbugs/PMD/detekt
C/C++: cmake --build, ctest, clang-tidy/cppcheck, compiler warnings
Rust: cargo check, cargo clippy, cargo test
Shell: bash -n, shellcheck
SQL/migration: migration dry-run, schema compatibility tests
```

## 4. 文档门禁

新增、删除、移动、重命名文件，或文件职责变化时，必须触发目录树或索引更新判断。

## 5. 结果分类

检查或测试结果使用：

```text
PASS
FAIL
NOT_RUN
```

失败原因建议分类为：

```text
本次变更引入
既有失败
环境失败
flaky
依赖缺失
验证命令错误
```

未执行项写为 NOT_RUN，并使用测试规则定义的原因。

## 6. 收口规则

1. 存在本次变更引入的 FAIL 时不得收口。
2. 必需验证项为 NOT_RUN 时不得收口，除非任务范围明确排除。
3. 未执行项不得写成 PASS。
4. 局部验证不得写成全量验证。

## 7. 禁止事项

1. 不允许无说明的类型、lint 或静态分析抑制指令。
2. 不允许裸 catch / except / rescue。
3. 不允许静默吞异常。
4. 不允许把未执行的工具写成通过。
5. 不允许把 skipped、mock、fake、health check、脚本存在、局部通过写成真实闭环。
