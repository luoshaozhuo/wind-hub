# Python 中文注释与 Google-style Docstring 规则

本规则补充 `coding.md`，适用于 Python 生产代码、工具代码、测试代码、脚本代码和迁移脚本。

## 1. 总原则

1. Python 文件应同时接受文件级、类级、函数级和测试说明检查。
2. 新增或修改的业务注释、module docstring、class docstring、function docstring 正文默认使用中文。
3. 第三方 API、协议字段、标准名、日志 key、异常类名、CLI 参数、环境变量、配置键、数据库字段、错误码、命令名、库名可保留英文。
4. 文档注释解释职责、边界、假设、风险和语义，不重复代码表面行为。
5. Docstring 不能替代类型标注。类型检查能发现的问题应通过准确类型或显式边界类型修复。

## 2. 文件头 / Module Docstring

每个 `.py` 文件应有 module docstring。简单文件说明用途；复杂文件说明职责边界。

生产代码文件头按涉及程度说明：

```text
1. 架构位置；
2. 主要职责；
3. 不负责什么；
4. 关键外部依赖或边界；
5. 关键 side effect；
6. 资源生命周期、事务、并发、超时、重试、回滚语义；
7. 安全、权限、审计、脱敏要求。
```

测试文件头按涉及程度说明：

```text
1. 被验证对象；
2. 所属测试阶段；
3. 使用的 fake/mock/stub/simulator 与真实依赖的差异；
4. 外部依赖；
5. 不能证明什么；
6. NOT_RUN 条件。
```

禁止只有 `Utilities`、`Tests for xxx`、`CRUD routes`、`Helpers` 这类空泛说明。

## 3. 必须写 Docstring 的对象

生产代码：

```text
1. public class / Protocol / ABC / dataclass / enum / TypedDict / Pydantic model；
2. public function / method；
3. API route handler / CLI command / entrypoint / composition root；
4. use case / service / repository / adapter / provider / gateway / runner / driver；
5. runtime / scheduler / worker / daemon / job handler；
6. 协议解析、序列化、反序列化、socket/subprocess 边界；
7. 权限、认证、审计、指标、脱敏、密钥、证书、TLS、安全策略；
8. lease、fencing、事务、幂等、乐观并发、回滚、重试、超时、backpressure；
9. ORM/API/DTO/schema/protocol 转换函数。
```

复杂 private helper 涉及以下内容时也应有 docstring 或关键注释：

```text
1. 返回多字段结构且字段语义非显然；
2. 修改外部状态；
3. 捕获通用异常后继续执行；
4. 创建、释放或持有资源；
5. 涉及调度、协议解析、安全边界、审计逻辑；
6. 实现超时、重试、回滚、fallback；
7. API route 中参与权限、session、审计、事务、响应模型转换、分页或错误转换。
```

测试代码：

```text
1. 测试文件说明被验证对象、阶段、替身差异、外部依赖、不能证明什么、NOT_RUN 条件；
2. 复杂 fixture、factory、fake、mock、stub 说明边界；
3. 生命周期或执行条件判定函数说明门禁条件和误判风险；
4. 并发、故障注入、超时、权限、审计、回滚、lease/fencing、协议、native runner、subprocess、socket、数据库、消息队列相关测试说明环境边界。
```

## 4. Function / Method Docstring 内容

复杂 public interface 的 docstring 应覆盖相关项：

```text
职责边界；不负责什么；关键参数；返回值或输出语义；稳定异常、错误码或错误分类；side effect；资源生命周期；并发、幂等、事务、超时、取消、重试、回滚语义；安全、权限、审计、脱敏。
```

允许保留 Google-style 标题：

```text
Args:
Returns:
Raises:
Yields:
Attributes:
Examples:
Notes:
```

标题下正文默认中文。

以下对象存在参数时写 `Args`，返回非 `None` 时写 `Returns`，调用方需要理解异常传播时写 `Raises` 或正文说明：

```text
1. public function / method；
2. API route handler；
3. CLI command / entrypoint / composition root；
4. use case / service / repository / adapter / provider / gateway / runner / driver；
5. runtime / scheduler / worker / daemon / job handler；
6. ORM/API/DTO/schema/protocol 转换函数；
7. 涉及权限、审计、事务、lease、fencing、重试、超时、回滚、资源释放的 private helper；
8. 复杂 fixture / fake / mock / stub / evidence gate。
```

## 5. 类型与抑制指令

1. public interface 必须有清晰类型、签名或 schema。
2. ORM 行、DTO、API response、repository 返回值、配置模型、消息模型和协议帧转换函数不得使用无约束 `Any`。
3. `Any` 只能用于外部未知边界、第三方库、动态反序列化或测试替身，并应说明原因和收敛方式。
4. 类型检查、lint 或静态分析抑制指令必须写明原因和后续移除条件。
5. nullable / Optional 使用前必须判断或转换。

## 6. 不合格 Docstring

以下 docstring 需要重写：

```text
1. 只有一句“将 X 转换为 Y”“获取 X”“创建 X”“更新 X”“删除 X”“返回 X”；
2. 只有英文短句业务说明；
3. 关键函数有参数但缺少 Args；
4. 关键函数返回非 None 但缺少 Returns；
5. 涉及权限、审计、事务、数据库、外部依赖、资源释放、lease/fencing、子进程、socket，却没有说明 side effect、失败边界或资源边界；
6. 只描述“做了什么”，没有说明调用契约、输入输出语义、边界或风险；
7. 把测试替身或局部检查写成真实生产能力；
8. 保留 Any、type ignore、noqa 等但没有说明原因和退出条件。
```

## 7. 普通注释

1. 注释解释为什么，而不是重复做了什么。
2. 适合写注释的内容包括：非显然业务规则、安全或审计要求、并发和时序约束、兼容性取舍、fallback 原因、异常继续执行原因、测试替身与真实依赖差异。
3. TODO 必须说明触发条件或后续处理方式。
4. 不给简单代码堆无意义注释。

## 8. 异常与清理说明

1. 裸 `except` 禁止。
2. 捕获通用 `Exception` 必须有明确处理意图。
3. 捕获异常后继续执行时，必须说明为什么继续是安全的。
4. 持有资源的函数必须说明释放路径，并使用 finally、context manager 或等价机制。

## 9. 质量治理清单

执行注释治理任务时，先生成清单，再修复。清单建议包含：

```text
文件路径；文件类型；是否有 module docstring；职责边界是否清楚；英文业务 docstring/注释；缺失 docstring 的 public class/function/method；复杂 private helper；缺失 Args/Returns/Raises；无约束 Any；无解释抑制指令；裸 except；静默吞异常；测试阶段误写风险；优先级；修复状态。
```

## 10. 禁止事项

1. public interface 无类型、签名或契约说明。
2. 复杂 public interface 无文档注释。
3. 文件无 module docstring。
4. 文件头只有空泛描述。
5. 新增或修改业务注释使用英文。
6. 关键函数缺少必要 Args / Returns / Raises。
7. ORM/API/DTO/schema 转换函数使用无约束 Any。
8. 无解释的类型检查或 lint 抑制指令。
9. 裸 `except` 或静默吞异常。
10. 大量无意义注释。
11. 把注释当作行为修复。
12. 把测试替身注释成真实生产能力。
