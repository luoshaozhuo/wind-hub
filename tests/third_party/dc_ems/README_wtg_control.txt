DCEMS 真实风机控制出口 — 编码交付与集中测试入口
================================================

独立开发分支：feat/dcems-gateway-smoke-20261126
目标：真实 dc-ems Server HTTP 控制操作 -> Modbus TCP FC16 ->
Gateway1 -> Commander -> Gateway2 -> DG 模型执行。
不改 dc-ems 发布源码/JAR/镜像，不修改已验证的十机测量场景。

一、控制契约
------------
DCEMS 数据库 Device ID：WTG01（其 Web setParam 将键按下划线拆分）。
wind-hub Device ID：WTG_SIM01。两者只通过 Gateway1 Modbus 端口关联。
DCEMS WTG01 -> Gateway1 :15531 -> Commander :52152 ->
Gateway2 :52173 -> DG :52161。

- ctrlActPow 1.4.41557，F32/CDAB，kW；Gateway 转为 W 乘 1000。
- ctrlReActPow 1.4.41555，F32/CDAB，kvar；Gateway 转为 var 乘 1000。
- emsWinSpd 1.4.41503，F32/CDAB，仅作为 DCEMS 实际 Modbus
  FC03 成功通信条件的最小可读点。当前 Gateway1 未接收真实 DG
  测量同步，不得以该点默认值冒充可信风速。
- 其余 7 个 DCEMS 原始控制点未接入，所有自动 AGC/AVC/PFR 算法
  在隔离种子中保持禁用。

此阶段只测试 DCEMS 设备级输出；全场级 FactoryPoints 调度输入尚无
经过验证的工业协议入口，不能宣称 DCEMS AGC/AVC/PFR 全自动闭环成功。
WTG_SIM01 从 PPC 控制清单中移除，由 DG local 管理控制输入。
其他 PV/BESS 仍由 PPC 控制，不得修改原生产场景。

二、准备独立 wind-hub 运行环境
--------------------------------
在项目根目录，激活仓库已安装依赖的 Python 环境：

  export PYTHONPATH="$PWD/src:$PWD"
  export DCEMS_CONTROL_ROOT="$PWD/run/dcems-control-wtg"
  export DCEMS_WTG_CONTROL_ROOT="$DCEMS_CONTROL_ROOT"

先由线下测试基础设施启动隔离 NATS 127.0.0.1:14223。
按照 README_control.txt 生成九个独立且长度至少 32 的令牌，
仅通过 Shell 环境变量注入。

  bash tests/third_party/dc_ems/run_wtg_control_chain.sh --prepare
  bash tests/third_party/dc_ems/run_wtg_control_chain.sh --check
  bash tests/third_party/dc_ems/run_wtg_control_chain.sh --up

真实 wind-hub 控制链专项：
  DCEMS_WTG_CONTROL_LIVE=1 python -m pytest -q \
    tests/third_party/dc_ems/test_wtg_control_chain_live.py

三、准备真实 DCEMS Server 的专用数据库
------------------------------------
必须从授权的发布配置副本准备 db.property/JKS 和合法的授权码，
不提交数据库凭据、许可证、令牌或运行文件。
运行独立种子构造器：

  python -m tests.third_party.dc_ems.prepare_wtg_dcems_seed \
    --output "$DCEMS_CONTROL_ROOT/data.json" \
    --gateway-host host.docker.internal

  python -m tests.third_party.dc_ems.prepare_runtime \
    --source "$DCEMS_RELEASE_CONFIG_DIR" \
    --output "$DCEMS_CONTROL_ROOT/docker"

  python -m tests.third_party.dc_ems.prepare_web \
    --output "$DCEMS_CONTROL_ROOT/web"

其中 DCEMS_RELEASE_CONFIG_DIR 应指向本机合法、完整的发布配置副本。
必须使用与本机匹配的 DCEMS_LICENSE_CODE，不能伪造许可。
数据文件包含可能敏感的账号字段，权限为 0600，不得提交。

隔离 Compose 项目：dcems-wtg-control。
本机绑定：DB :15433、Server :18453、Web :18090。
独立数据库卷，禁止复用 dcems-smoke、生产 DB 或 Web 运行副本。

  export DCEMS_ALLOW_LOCAL_SMOKE=1
  bash tests/third_party/dc_ems/run_wtg_dcems_server.sh --check
  bash tests/third_party/dc_ems/run_wtg_dcems_server.sh --up

Docker bridge 到宿主 Gateway1 的访问必须在运行时实测。
若 host.docker.internal 无法连接只绑定 127.0.0.1 的 15531，
报告环境 BLOCKED。不可擅自暴露无认证的 Modbus TCP 到生产网卡。

真实 Server→Modbus→DG：
  DCEMS_WTG_DCEMS_LIVE=1 python -m pytest -q \
    tests/third_party/dc_ems/test_wtg_dcems_server_live.py

四、证据与限制
--------------
必须记录 HTTP 请求、Gateway1 协议写事件、Commander request_id、
command_id/outcome、DG APPLIED/effective_step 和 Gateway2 SI 回读。
DCEMS Web HTTP 200、其"控制成功"文案和 Modbus ACK 都不是执行证明。

DCEMS 源代码存在一个限制：Web setParam 忽略 getter.control()
返回值。因此必须检查 Gateway1 真实事件与下游执行，而不能只看 Web 响应。
已有代码不直接证明真实风机电功率动态响应。风机控制模式的安全初始化
和物理响应曲线应另行作为真实联测验收门槛，不能从 APPLIED 推断。
如检测到非预期控制或身份混淆，立即停机，不继续自动发令。

关停：
  bash tests/third_party/dc_ems/run_wtg_dcems_server.sh --down
  bash tests/third_party/dc_ems/run_wtg_control_chain.sh --down

禁止 docker compose down -v；保留独立运行日志，绝不清理其他项目卷。

当前仅提交代码和隔离配置：所有新测试和 Docker 联测尚未执行。
请在全部实现冻结后集中交由 Claude 进行静态/Loader/协议/真实 Server 测试。
