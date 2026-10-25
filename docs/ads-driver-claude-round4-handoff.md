# ADSDriver 第四轮：Claude Code 独立验收交接

仓库：luoshaozhuo/wind-hub
分支：refactor/ads-driver-clean-rewrite
基线：main@8b6f654ab23dad3899e7016b26b3b5412689d5a4

## 角色边界
- ChatGPT 负责所有生产代码设计、实现与生产缺陷修复以及最终合并。
- 会话外 Claude Code 负责测试代码与 Fixture、格式整理、静态检查、单元/组件/集成/可靠性测试和独立审查复测。
- Claude 不直接改生产代码；发现缺陷附重现步骤和文件行号，交回 ChatGPT 修复；修复后独立复测。
- 不绕过 Ruff、Mypy、Import Linter，不添加忽略项掩盖缺陷。

## 优先级 P0（请先检查并报告）
1. pyads.Connection 与 _AdsSession 的真实 API：set_timeout、read_state、read_write(Sum Read/Write)、notification(datatype) 装饰器、add_device_notification、del_device_notification 的签名及返回值。
2. Driver connect() 超时/取消：后台线程迟到成功不能错误发布 CONNECTED，清理失败必须 FAULTED 并保留目标身份；路由添加耗时超过总预算时不得再次 connect。
3. Session single-lock：execute/read/write 与 close 不重叠；排队写取消前未发送；正在运行的 to_thread 不能因外层 wait_for 取消而提前释放资源。
4. Subscription start/stop/driver.close 竞态：注册 Handle 迟到成功必须记录并删除；注销失败、注册返回状态不明、Worker/Callback 仍存活时报告不确定状态；重复 stop 不重复删除。
5. Writer UNKNOWN：单点和 Sum Write 的通信失败、请求取消、设备逐点错误、分块后续未发送，不得自动重发或误报 CONFIRMED_SUCCESS。
6. 资源与重连：同一 AMS Net ID 只能有一个 Driver，占用释放不得早于确证清理；初始化/结束 AMS 的调用方取消、进程退出、多个 Driver 并发是否安全。
7. Connector/shared contract：ProtocolPort.subscribe 同步返回 start/stop；Collector 映射为 AcquisitionHandle.close；Modbus/IEC104 仍显式拒绝订阅；跨协议受影响调用点不遗留 await subscribe。
8. 数据：批量读顺序与质量、Symbol 在新 Session 重解析、点表热更新快照一致性、通知时标 UTC 和通知队列饱和。

## 执行门禁
- ruff check src tests scripts
- ruff format --check src tests scripts
- mypy src
- lint-imports
- 仓库定义的 CI fast/PR gate，及针对 ADS 的组件/契约/真实协议集成、可靠性测试。
- 逐项记录 64 项功能及可靠性、10 项清理门禁（R01-R10）；无法运行真实 PLC 的项目标记 NOT RUN。
- 核查删除旧 ADS 专属测试后应补的覆盖，不靠减测让 CI 绿。
- 尤其检查实际配置 roundtrip：ADSLocalConfig 三项超时、parse_domain、snapshot_writer、Web Settings、热重载安全。
- 核查新增生产代码的 Ruff、Mypy、pyads API 类型、导入路径和循环依赖。

## 独立报告格式
按 Git 基线、环境、命令/退出码、测试分层、P0 缺陷、P1 缺陷、未验证项、R01-R10 清理门禁、是否可合并，给出明确结论与可复现证据；不要直接合并。
