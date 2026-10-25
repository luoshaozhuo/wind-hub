# ADSDriver 全新实现：四轮开发计划

基线：main@8b6f654ab23dad3899e7016b26b3b5412689d5a4
设计基线：ADSDriver 完整技术设计 v0_3

## 第一轮：旧实现清理与接口定稿
盘点 ADS 全部生产文件、调用方、配置、测试及 Fixture，逐文件判定保留／重写／删除。清除旧 Driver、Session、Subscription 的废弃机制与耦合测试；保留有效编解码算法和公共业务契约。同步确认 ProtocolPort.subscribe 与 SubscriptionHandle 新接口对 Collector、Commander 及其他协议的影响。中间提交不得造成无意的导入断裂。

## 第二轮：_AdsSession
一次性 pyads.Connection；异步 open/execute/close；单个 asyncio.Lock 串行化物理命令；内部 Task 持续跟踪 to_thread 的真实结束；调用方取消或应用超时不提前释放资源；迟到关闭自动收敛，清理不确定时 fail-closed。

## 第三轮：ADSDriver 与 ADSSubscription
Driver 五态、PLC 身份占用、路由失败修复一次重试、全连接超时、原生 Sum Read/Write、Symbol 映射与更新、写入 UNKNOWN 不重传；Subscription 同步构造、异步 start/stop、Handle 归属及 Driver.close 并发安全。

## 第四轮：关联模块迁移与清理收尾
修改 ProtocolPort、Collector、Commander 及必需配置；统一进程 AMS 初始化与退出；删除所有旧接口、冗余代码、旧配置、无效测试和 Fixture。交由会话外 Claude Code 集中进行大规模测试、测试与 Fixture 修复、格式修正及独立复测；ChatGPT 修复生产代码并据验收决定合并。

## 门禁
- 64 项功能可靠性要求及 10 项清理要求逐项追踪。
- 不引入兼容层、无依据的异常屏蔽或额外状态机。
- 提交前执行 Ruff Lint、Ruff Format、Mypy 与 Import Linter 等项目质量门禁；无法执行必须明确列为未验证。
- 无法确认物理资源释放时禁止重连或释放 PLC 身份。
