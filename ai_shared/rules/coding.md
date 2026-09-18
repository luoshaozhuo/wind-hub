# 通用编码、接口、类型与注释规则

## 1. 基本原则

1. 优先清晰、直接、可读。
2. 保持当前模块、语言和框架的局部风格。
3. 控制复杂度；当一个单元同时承担校验、编排、外部调用、异常转换、审计、持久化或资源管理时，应拆分阶段。
4. 不为单一调用点制造不必要抽象。
5. 关键业务逻辑、安全、审计、租约、权限、回滚、重试、幂等、事务边界必须显式可见。
6. 不静默吞异常；捕获异常后必须转换稳定错误、记录必要上下文、保留异常链、执行清理后继续，或说明继续执行为何安全。
7. 外部系统差异应隔离在 adapter、provider、repository、gateway、client、runner、driver、connector 等边界层。
8. 不做无关格式化，不批量重排与任务无关的代码。
9. 不引入未经确认的新依赖。
10. 不恢复废弃文件，不把实验、测试、诊断能力接入生产路径。
11. 不把 mock、fake、stub、health check、脚本存在、局部通过伪装成真实闭环。
12. 修改代码必须同步评估测试、类型、注释、配置、schema、文档、索引和需求状态影响。
13. 默认禁止自动执行任何会修改 Git 历史、工作区状态或远端仓库状态的命令，除非用户明确要求。

## 2. 架构与边界

1. 生产路径、工具路径、测试路径必须边界清楚；工具或实验模块不得被生产路径直接依赖，除非已有明确决策允许。
2. 修改前先识别目标路径或模块既有的分层方式、依赖方向、扩展缝和职责切分；若本轮只是既有架构中的局部扩展，应优先沿用当前模式与边界，而不是平移出第二套实现。
3. 涉及目录归属时，先区分“运行时代码”和“部署交付资产”：被应用在运行时直接 import、调用或依赖的实现，仍属于源码；用于环境装配、发布、启动、回滚、巡检或部署说明的资产，才属于部署范围。
4. `deploy/` 只承载部署落地资产，例如部署清单、环境变量模板、样例配置、compose/helm/k8s/ansible/terraform 资产、拓扑说明、发布与回滚 runbook；不用于承载业务运行时代码、通用运维框架实现、需求文档、验证报告或与部署无关的长期说明。
5. 若某模块同时包含运行时实现和部署资料，应将运行时实现保留在 `src/` 或其既有源码目录，将部署资料放入 `deploy/<module>/`，而不是为了“集中”把运行时代码搬进 `deploy/`。
6. 用例、应用服务或 orchestrator 负责业务编排，不应包含协议细节、数据库细节、CLI 解析或测试专用分支。
7. adapter、runner、repository、provider、gateway、driver 负责外部系统差异，不应泄漏到领域层或核心业务层。
8. composition root / bootstrap / wiring 负责依赖装配；默认装配不得缺失必要的安全、审计、指标、租约、熔断、重试等生产横切能力。
9. API / CLI / CRUD 层不得绕过权限、审计、dry_run、幂等、乐观并发、事务和输入校验要求。
10. scheduler、worker、lease、fencing、retry、backpressure 等运行时能力必须明确失败语义和恢复语义。
11. 运行时能力声明必须区分 declared capability、actual runtime availability、validated evidence。

## 3. 接口、类型与契约

1. public interface 必须语义清楚，包括输入、输出、错误、side effect、幂等性、事务边界和资源生命周期。
2. 支持类型系统的语言中，public function / method / class / struct / interface / API handler 应有清晰类型、签名或 schema。
3. 弱类型或脚本语言必须通过 schema、参数校验、注释、测试或文档表达输入输出契约。
4. 稳定多字段数据不应长期使用松散 map/dict/object；优先使用 dataclass、struct、record、DTO、schema、TypedDict、Pydantic model、Protocol、interface 或领域模型。
5. 不使用无约束动态类型；确需使用时必须限制在外部边界、反序列化边界、第三方库边界或测试替身，并说明原因。
6. SQL schema、ORM 字段、配置项、环境变量、CLI 参数、消息格式、文件格式、API schema 都视为数据契约。
7. 修改 public interface 时，必须同步修改调用方、测试、文档和相关索引。
8. 并发、异步、线程、进程、子进程、socket、文件句柄、数据库 session、锁、lease 等资源必须有关闭、释放或取消路径。

## 4. 文件与注释

1. 生产代码、工具代码、测试代码和脚本文件应有文件级说明或模块文档注释。
2. 文件头不得只写空泛描述，例如 `Utilities`、`Tests for xxx`、`CRUD routes`、`Helpers`。
3. 文件头应说明职责、边界、不负责什么、关键外部依赖、重要 side effect、资源生命周期、事务/并发/超时/重试/回滚语义中涉及的关键项。
4. public class / Protocol / dataclass / enum / function / method / API route / CLI command 必须有必要文档注释。
5. private helper 涉及调度、协议、权限、审计、lease、fencing、事务、重试、异常转换、资源释放、复杂 fixture 时，也必须有文档注释或关键注释。
6. 注释解释原因、边界、假设、风险、约束和非显然行为，不重复代码表面行为。
7. 不允许无解释的类型检查、lint 或静态分析抑制指令。

## 5. 测试同步

当代码发生以下变化时，必须同步评估和更新测试、fixture、fake/mock/stub、测试索引和相关文档：

1. 行为变化；
2. public interface、port、Protocol、ABC、API、CLI 变化；
3. schema、配置、环境变量、迁移、消息格式、协议帧变化；
4. adapter、repository、gateway、driver 的外部依赖语义变化；
5. 权限、审计、幂等、重试、超时、事务、lease/fencing、回滚等运行时语义变化。

测试失败时，不得默认回滚正确的新行为以满足旧断言。应先判断生产代码缺陷、测试断言过期、需求或契约变化、环境或依赖缺失、既有失败。

## 6. 多语言质量

1. 遵循仓库配置的 lint、format、type-check、test 工具。
2. 修改代码后，必须优先执行或说明未执行原因：语法/编译检查、lint/static analysis、type-check、受影响测试。
3. 涉及全局治理时，应执行目录级检查，并区分既有问题与本次新增问题。
4. 禁止裸 catch / except / rescue。
5. 捕获通用异常时，必须明确处理意图，不得静默吞掉。
6. 异步、并发和子进程代码必须注意取消、清理、超时和 backpressure。
7. 网络、文件、数据库、队列、子进程、外部系统调用必须有超时、deadline 或可取消机制；长期任务必须有可观测心跳或状态。
8. 重试必须有上限、退避策略和错误分类，不得无限重试。
9. 不在生产代码中保留未隔离的调试分支、临时开关和测试专用逻辑。
10. 日志不得泄漏密钥、凭证、token、个人信息或敏感配置。

## 7. SQL / ORM / 配置

1. 不凭记忆推断字段，必须读取当前 ORM、schema、migration 或配置定义。
2. 查询优先沿用仓库当前风格。
3. 事务边界必须清晰；跨 session / context / transaction 对象不得直接修改并假设自动持久化。
4. schema 变化必须说明兼容性、迁移策略和存量数据处理。
5. 新增 NOT NULL 字段必须处理已有数据。
6. 配置项变化必须检查默认值、示例、测试、部署文档和回滚影响。
7. 运行时配置变更必须考虑并发、幂等、版本冲突和审计记录。
8. 外部依赖配置不得默认开启危险能力；写入、控制、删除等高风险能力应默认关闭。

## 8. 文档与状态

1. 代码、测试、配置、schema、脚本、报告与需求或任务状态必须保持一致。
2. 新增、删除、移动、重命名、职责变化文件必须触发目录树或索引更新判断。
3. 状态只能基于当前真实证据更新。
4. mock、contract、simulator 或局部检查不得写成真实生产闭环。
5. 发现状态高估时，必须降级或改为明确的 NOT_RUN / 未验证说明。

## 9. Git 与远端操作边界

1. 默认允许只读 Git 检查命令，例如 `git status`、`git diff`、`git diff --cached`、`git log`、`git show`、`git rev-parse`。
2. 默认禁止自动执行任何会修改 Git 历史、工作区状态或远端仓库状态的命令，除非用户明确要求。
3. `commit`、`push`、`reset`、`clean` 以及等价的 Git/GitHub 写操作都属于默认禁止范围。
4. `checkout --`、`restore`、`switch`、`merge`、`rebase`、`cherry-pick`、`tag`、`stash pop`、`stash drop`、`remote set-url`、`gh pr merge`、`gh repo rename` 等会修改历史、工作区或远端状态的命令，也属于默认禁止范围。
5. 任何 Git/GitHub 写操作即使被用户明确要求，也必须在结果中说明执行原因和影响范围。
