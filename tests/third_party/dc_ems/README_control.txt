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

复测轮次约束：
  1. test_control_chain_live.py 的 UNKNOWN 用例会使 PPC_SIM01 进入恢复阻断；
     每轮完整主验收必须使用全新隔离根。--prepare 拒绝覆盖既有根。
  2. Gateway2 连接超时专项 test_control_chain_timeout_live.py 单独使用另一个
     全新隔离根，并设置 DCEMS_CONTROL_TIMEOUT_ROUND=1；不得与主验收同根顺序执行。
  3. DG `readonly` 身份复用 WIND_HUB_E2E_COMMANDER_MAINTENANCE_TOKEN，仅提供
     variables_read，用于负向鉴权；不得据此声明生产令牌权限模型。


下一阶段：DCEMS 真实控制输出审查（2026-11-27）
--------------------------------------------
新增离线合同：
  tests/third_party/dc-ems/config/dcems_control_output_contract_v1.0.0.yaml
验证入口：
  python -m pytest -q tests/third_party/dc_ems/test_dcems_control_output_contract.py

dc-ems/main 的 DataGetterModbusImpl.control() 确实使用已绑定 isCtrl 点
向外部风机 Gateway 下发 Modbus；IEC104 的 control() 直接返回 false。
原始 DCEMS 发布种子共 9 个单机写点，其中有功 ctrlActPow 使用
1.4.41557，无功 ctrlReActPow 使用 1.4.41555；均为 32 位浮点 CDAB。
两个目标分别拟映射 wind-hub WTG 的 active_power_setpoint /
reactive_power_setpoint，DCEMS kW/kvar 对应 DG W/var，须乘以 1000。
当前 Gateway Modbus codec 仅负责数据类型和字序，并不执行 scale
换算。上述候选映射标为 PROPOSED_UNVERIFIED，禁止直接运行成
已验证控制入口，更不能用 PPC_SIM01.agc_enable 代替风机级输出。

真实 DCEMS Server 发出的 FC16 报文、设备绑定、单位换算、Commander
执行以及 WTG 物理响应均尚未由本阶段审查证明。下一步须在隔离根内
明确转换边界并补真实协议测试后才能解除该阻断。
