DCEMS / Commander 控制闭环隔离测试
==================================

范围：独立 PPC 物理场景、单个 AGC 使能控制。
不是 DCEMS Server/Web 生产验收；不替代原十机测量链路。

控制路径：
  FC06 Holding[0] → Gateway1 (127.0.0.1:15431, PPC_SIM01)
  → Commander (127.0.0.1:52152)
  → Gateway2 ExecuteSimulationCommand (127.0.0.1:52173)
  → Data Generator (127.0.0.1:52161)
  → 查询 SUCCEEDED/CONFIRMED_SUCCESS + GetInputExecution APPLIED
  → Gateway2 PPC Holding[0] (127.0.0.1:15425)

测试点来自原 e2e-final 物理 PPC 点表：agc_enable，uint16，0/1。
所有其他物理仿真 Gateway2 Modbus 端口为 15421..15424。
Gateway1 和 Gateway2 分属两个正式独立服务进程。

环境：
  在 wind-hub 项目根目录运行。
  Python 依赖按项目环境安装；PYTHONPATH 由脚本设置。
  NATS/JetStream 由线下测试环境在 127.0.0.1:14223 先行启动，
  并保证 COMMAND_AUDIT 审计流符合 Commander 配置。
  测量专项使用的 14222 端口与本专项分离。
  不能连接生产环境，不得重用已有实例状态。

以下 9 个环境变量均使用不同的随机 Bearer Token，长度至少 32：
  WIND_HUB_E2E_GATEWAY_OPERATOR_TOKEN
  WIND_HUB_DCE_CONTROL_INGRESS_OPERATOR_TOKEN
  WIND_HUB_E2E_COMMANDER_EMS_TOKEN
  WIND_HUB_E2E_COMMANDER_MAINTENANCE_TOKEN
  WIND_HUB_E2E_GATEWAY_COMMANDER_TOKEN
  WIND_HUB_E2E_COMMANDER_GATEWAY_TOKEN
  WIND_HUB_E2E_GENERATOR_GATEWAY_TOKEN
  WIND_HUB_E2E_GATEWAY_GENERATOR_TOKEN
  WIND_HUB_E2E_GENERATOR_CLI_TOKEN

令牌只能通过本地 Shell 注入，严禁提交、打印或硬编码真实密钥。

准备及静态 Loader 检查：
  bash tests/third_party/dc_ems/run_control_chain.sh --prepare
  bash tests/third_party/dc_ems/run_control_chain.sh --check

启动并运行：
  bash tests/third_party/dc_ems/run_control_chain.sh --up
  export DCEMS_CONTROL_LIVE=1
  python -m pytest -q tests/third_party/dc_ems/test_control_chain_live.py

输出：
  run/dcems-control/state/control-evidence.json
记录 event_id→request_id→command_id、Commander 状态、DG APPLIED 与
Gateway2 协议回读。APPLIED 证明模型控制输入执行；不等于电厂物理响应通过。
真实物理响应、AGC/AVC/PFR 算法及 DCEMS Server/Web 不在本阶段覆盖范围。

停止：
  bash tests/third_party/dc_ems/run_control_chain.sh --down

要求：
  1. 所有 wind-hub 服务由正式 CLI 启停。
  2. 生产 Loader/Deployment、真实 Modbus、Bearer、Commander、DG 均需测试。
  3. 失联/UNKNOWN/超时和幂等必须区分，不允许以 Modbus ACK 替代执行确认。
  4. Modbus TCP 写入没有 Bearer 用户身份验证，测试中仅允许 loopback。
     错误 Bearer 拒绝测试针对 Gateway/Commander 的 gRPC 管理接口。
  5. 新增失败证据时保留 CLI 进程日志，不修改第三方镜像或已通过的测量链路。
  6. 不生成 PR，不合并 main。

初始交付仅为生产配置及联测测试代码。真实进程执行和独立验收
应由 Claude Code 在线下完整环境复测，不得提前宣称 PASS。
