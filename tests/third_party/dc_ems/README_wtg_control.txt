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
- 最小种子仅保留两个相邻可回读的 Holding 控制点，DCEMS
  可通过 FC03 读回验证 Modbus 通道连接；未配置风速测量点。
  Gateway1 不含真实风速/有功测量回传，本专项不能用于自动算法验收。
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
先运行非破坏性的发布资源预检：

  python -m tests.third_party.dc_ems.preflight_wtg_dcems_server \\
    --root "$DCEMS_CONTROL_ROOT" --json

退出码 2 代表外部资源缺失，输出逐项 MISSING/READY；此时不应
执行 Docker 启动。数据库业务 Schema 和真实 FC16 在准备阶段一律
标记 UNVERIFIED，只有正式 Server 成功初始化和发令才算通过。

dc-ems 发布源码的 DbStructService 包含首次建表与 data.json
基础数据导入逻辑，因此不能假定需要人工提供独立 SQL。
同时不能仅根据 PostgreSQL SELECT 1 或 Docker Running 宣称
业务表已经正确建好。真实启动后核查 ems_config、device、
edge_addr_set 等实际表与 WTG01/ctrlActPow/ctrlReActPow 记录。

所需外部材料：匹配 VERSION 的两份官方预构建镜像
dc-ems-server、dc-ems-timescaledb；发布配置副本
(.env、log4j.properties、SSL JKS 或由授权部署流程生成的 JKS)；
合法 LICENSE_CODE。发布包不导出独立 dc-ems-web 镜像：Server 镜像
内含 html/，在同一 HTTPS 443 上提供 /dc-ems 静态页与 /api。
只允许从受信任发布包校验 MANIFEST.sha256 后执行 docker load，
禁止使用 docker build 或自动拉取未知镜像。
例如在完整受信任发布包目录中：

  (cd "$DCEMS_RELEASE_DIR" && sha256sum -c MANIFEST.sha256)
  docker load -i "$DCEMS_RELEASE_DIR/images/dc-ems-server.tar"
  docker load -i "$DCEMS_RELEASE_DIR/images/timescaledb.tar"

仅在文件已存在、校验通过且人工确认来源后执行；不存在则保持
T02=ENVIRONMENT_BLOCKED。不得生成或伪造 license。
必须从授权的发布配置副本准备 db.property/JKS 和合法的授权码，
不提交数据库凭据、许可证、令牌或运行文件。
运行独立种子构造器：

  python -m tests.third_party.dc_ems.prepare_wtg_dcems_seed \
    --output "$DCEMS_CONTROL_ROOT/data.json" \
    --gateway-host host.docker.internal

  python -m tests.third_party.dc_ems.prepare_runtime \
    --source "$DCEMS_RELEASE_CONFIG_DIR" \
    --output "$DCEMS_CONTROL_ROOT/docker"

其中 DCEMS_RELEASE_CONFIG_DIR 应指向本机合法、完整的发布配置副本。
必须使用与本机匹配的 DCEMS_LICENSE_CODE，不能伪造许可。
数据文件包含可能敏感的账号字段，权限为 0600，不得提交。

隔离 Compose 项目：dcems-wtg-control。
本机绑定：DB :15433、Server/Web :18453（HTTPS，自签名证书）。
独立数据库卷，禁止复用 dcems-smoke、生产 DB 或 Web 运行副本。
Web/API 使用真实登录会话：GET /api/login 获取 token，后续请求携带 token 头。

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

已有独立验收：WTG_PHYSICS_PASS、WINDHUB_CONTROL_PASS。
真实 DCEMS Server T02 未执行，仍为 ENVIRONMENT_BLOCKED。
本轮新增发布资源预检及运行脚本调整，尚未进行新的集中测试。


物理模型 P1 修复说明（2026-11-27）
--------------------------------
控制模式 0 为最优功率跟踪，按照模型定义不使用主动功率设定值。
本专项 run_wtg_control_chain.sh --up 在隔离 Generation 启动之后，
通过正式 CLI 将 WTG_SIM01.control_mode 初始化为 1（恒功率目标）。
这一初始化是试验场景的显式控制契约，不修改 DG 的全局默认模式。

WTG 模型在超出 Cp 曲线最后一个叶尖速比点后采用连续且非增长的
线性尾段，避免跳变到 0。限功率模式下，发电机电磁转矩在变桨
逐步降低捕获气动能量的过程中按当前可用功率响应，并通过桨距
前馈反演目标气动捕获功率。保留原机械惯量、变桨速率、转矩
时间常数、超速保护，不引入瞬时变桨或假定瞬时限功率成功。

由于出厂模型 pitch_rate_rad_s=0.12 rad/s，初始高功率转入
150 kW 限发有显著的变桨过渡时间。物理响应验收应先记录
稳定的低功率基线，再施加 650 kW 阶跃并记录稳定结果及完整
过渡曲线。不得使用不足以完成真实变桨的 5 秒窗口评判稳态，
也不得降低功率变化或安全保护的验收要求。
本轮生产修改及模式初始化尚未运行独立集中复测。


标准部署缺陷整改（当前 wind-hub 开发分支）
----------------------------------------
本分支已修订 third_party/dc-ems/deploy.sh 中的授权系统标识为
DC-EMS；通过持久化 machine-id 让一次性机器码生成容器与正式
Server 容器使用相同机器身份。同步更新 Compose 及 MANIFEST.sha256，
不能再以修改前的发布包摘要作为修改后脚本的来源证明。

特别注意：本仓库的 third_party/dc-ems 为已构建发布包，仅包含
dc-ems.jar，没有 FactoryPoints.java 的可编译源代码。FactoryPoints
布尔调度使能空值修复已保存在
tests/third_party/dc_ems/patches/factorypoints-null-dispatch-flags.patch，
此文件是供持有 DCEMS Java 源码的构建环境应用的补丁，不会改变
现有 JAR 的行为。必须重新构建并独立验证合法发布包后，才能认定
该 NPE 的生产缺陷关闭；不得将本轮的补丁存在视为测试通过。
