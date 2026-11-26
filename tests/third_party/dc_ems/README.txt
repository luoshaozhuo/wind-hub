DCEMS 隔离联测操作说明
=======================

适用分支：feat/dcems-gateway-smoke-20261126
约束：不改 src/、configs/、third_party/dc-ems 原始发布内容。
所有提交的测试专用 YAML / CSV：
  tests/third_party/dc-ems/config/
所有测试 Python、Shell、Compose Overlay：
  tests/third_party/dc_ems/
运行时文件：run/dcems-smoke/（已被 .gitignore 忽略）。

范围：10 台随机风机 + 复用首台 Gateway 的升压站与调频逻辑设备。
数据：DG random -> Gateway 真实 Modbus -> DCEMS Server -> Web。
AGC/PFR 算法的 FactoryPoints 外部目标目前没有确认的工业协议入口，
因此首轮只验收数据采集与 Web 实时显示；控制算法默认关闭。
调频外部模式固定为 false，不进入缺少 emsFreDst 的激活分支。
不得将本轮数据烟雾 PASS 等同于完整 AGC/PFR/Commander 闭环 PASS。

一、静态配置与源码约定检查
--------------------------
在 wind-hub 根目录、正确的 Python 虚拟环境中执行：

export PYTHONPATH="$PWD/src:$PWD"
python -m pytest -q \
  tests/third_party/dc_ems/test_seed_contract.py \
  tests/third_party/dc_ems/test_prepare_seed.py \
  tests/third_party/dc_ems/test_config_smoke.py \
  tests/third_party/dc_ems/test_auxiliary_signals.py \
  tests/third_party/dc_ems/test_external_signals.py \
  tests/third_party/dc_ems/test_prepare_web.py \
  tests/third_party/dc_ems/test_prepare_runtime.py \
  tests/third_party/dc_ems/test_official_loader.py

其中 test_official_loader.py 强制使用生产 SharedDocument/Gateway/DG Loader
解析全部精确版本引用与随机模型，不是简单 YAML 语法测试。
失败应阻断部署，不应跳过或修改 src/、configs/。

二、准备随机 DG 和 Gateway
--------------------------
# 以下三个 Bearer 令牌必须在本地生成，不可写入 Git。
export WIND_HUB_DCE_GENERATOR_GATEWAY_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export WIND_HUB_DCE_GATEWAY_OPERATOR_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export WIND_HUB_DCE_GENERATOR_CLI_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"

bash tests/third_party/dc_ems/run_windhub.sh --prepare
bash tests/third_party/dc_ems/run_windhub.sh --check
bash tests/third_party/dc_ems/run_windhub.sh --up

对应设备 WTG01..10；监听 127.0.0.1:15221..15230。
Gateway 点键为正式 BusinessPoint 变量名：
  wind_speed = 1.3.0, FC04 F32 ABCD, m/s（每台）
  sim_running_status = 1.3.6, FC04 I32 ABCD，固定工作模式 32（每台）
  sim_active_power = 1.3.8, FC04 F32 ABCD, W（每台）
  sim_reactive_power = 1.3.10, FC04 F32 ABCD, var（每台）
  active_power_limit = 1.3.12, FC04 F32 ABCD, kW，固定 2000（每台）
  generator_speed / blade_1_pitch_angle / pitch_angle = 1.3.14/16/18（每台）
  device_fault_active = 1.1.1, FC02 bool，固定 false（每台）
  sim_plant_active_power = 1.3.2, FC04 F32 ABCD, W（首台）
  sim_plant_reactive_power = 1.3.4, FC04 F32 ABCD, var（首台）
  primary_frequency_mode = 1.1.0, FC02 bool，固定 false（首台）
Power 的 0.001 系数在 DCEMS edgeaddrset 将 W/var 转为 kW/kVar。
上述补充点用于满足 DCEMS Value 单参数构造当前值为空的算法依赖，
不是控制写入或模拟控制成功。
Modbus 地址只作为协议契约，变量名并不进入 Modbus 报文。
首台随机 P/Q 是独立试验激励，不是 10 台机组功率之和。

三、生成独立 DCEMS 数据库和 Web 资源
-----------------------------------
上游发布包按约定不提交现场 .env 和 JKS；必须从本机合法现场资源派生
run/dcems-smoke/release-config，使用全新随机数据库密码、匹配 Server
固定加载契约的 JKS，以及官方管理员工具针对当前机器码生成的授权。
该目录和以下运行副本均不得提交 Git。不得使用其他设备的授权码。

python -m tests.third_party.dc_ems.prepare_runtime \
  --source run/dcems-smoke/release-config \
  --output run/dcems-smoke/docker

python -m tests.third_party.dc_ems.prepare_web \
  --output run/dcems-smoke/web

# 许可证只能来自本机合法授权，不得硬编码进测试源码。
export DCEMS_LICENSE_CODE='<本机 DCEMS 合法授权码>'
python -m tests.third_party.dc_ems.prepare_seed \
  --seed third_party/dc-ems/app/server/data.json \
  --mapping tests/third_party/dc-ems/config/mapping_random_smoke.csv \
  --gateway-point-table run/dcems-smoke/configs/templates/point_tables/point_table_dcems_wtg_random_v1.0.0.yaml \
  --smoke-subset --with-auxiliary --device-count 10 \
  --gateway-host host.docker.internal --gateway-port 15221 \
  --output run/dcems-smoke/data.json
unset DCEMS_LICENSE_CODE

派生 DB 含 10 台 WTG、1 台逻辑升压站、1 台逻辑调频设备，
合计 12 条 DCEMS 设备记录、10 个 Gateway 协议通道。
原发布数据仍保留，不得把带证书/账户/许可证的 run/ 文件提交。
源代码已核对 SyzDevice.powAct/powReact 与 YctpDevice.isFreMode 更新路径。

四、启动独立 DCEMS 数据库、Server、Web 容器
-------------------------------------------
原 dc-ems 默认数据库、服务端和 Web 端口为 5432/443。
执行前确认本机对应端口不被其他业务占用；
Docker Compose 项目固定 dcems-smoke，独立 volume，
DB_NAME 固定 dcems_smoke，禁止复用旧业务数据库。
本轮将设备 ID 从 DCEMS_WTG01..10 改为 WTG01..10（辅助设备 SUBSTATION、PFR）。
旧 run/dcems-smoke/configs 和 run/dcems-smoke/data.json 不会自动更新；
请先停止本测试全部进程，并将旧运行产物归档，再重新执行 --prepare、
prepare_runtime.py、prepare_seed.py 和 prepare_web.py。
旧隔离数据库卷内仍是旧设备 ID，不能设置 DCEMS_SMOKE_ALLOW_REUSE=1
绕过本次重新导入。线下人员应先核对 volume 标签确属
com.docker.compose.project=dcems-smoke、备份必要证据，之后仅清理
本测试专用卷并让新空卷导入新的 data.json；不得清理其他项目的卷。
重新启动后必须检查设备表含 WTG01..10 且不含 DCEMS_WTGxx。
只有后续重复运行且确认数据库与当前派生种子一致时，才允许设置
DCEMS_SMOKE_ALLOW_REUSE=1。

export DCEMS_ALLOW_LOCAL_SMOKE=1
export DCEMS_TEST_SEED="$PWD/run/dcems-smoke/data.json"
export DCEMS_TEST_WEB="$PWD/run/dcems-smoke/web"
export DCEMS_RUNTIME_DIR="$PWD/run/dcems-smoke/docker"
bash tests/third_party/dc_ems/run_local.sh --check
bash tests/third_party/dc_ems/run_local.sh --up

启动脚本根据 third_party/dc-ems/VERSION 的 release_version
寻找 dc-ems-server、dc-ems-web 的镜像标签，不执行 Docker pull。
Docker Desktop 下 Server/Web 使用 bridge 网络，分别映射 18443/18080；
Server 通过 dc-ems-db:5432 访问独立数据库，通过 host.docker.internal
访问真实 Gateway，不使用容器 loopback 冒充宿主机服务。
Web: http://127.0.0.1:18080/dc-ems/
Server: https://127.0.0.1:18443 （自签名证书仅测试用途）。
Web 运行副本将旧硬编码 WebSocket 内网 IP 改为当前 origin，
不修改原始 Java JAR 或 Web 构建包。

五、真实协议与 WebSocket 动态检查
----------------------------------
export DCEMS_LIVE=1
export DCEMS_GATEWAY_HOST=127.0.0.1
export DCEMS_GATEWAY_PORT=15221
export DCEMS_WEB_HOST=127.0.0.1
export DCEMS_WEB_PORT=18080
python -m pytest -q tests/third_party/dc_ems/test_live_smoke.py

验收内容：
- 10 台风机各自在 FC04 风速寄存器有动态值；
- 首台 Gateway 的 FC04 站控 P/Q 与 FC02 调频模式存在；
- Web 静态页面和 API 代理可达，WebSocket 返回 101；
- 实际发送订阅报文并核实 DCEMS Server 对 10 台风机的数据有更新；
- 并发读取 Gateway 与 WebSocket，逐台数值对账并检查 W/var→kW/kVar；
- DCEMS SUBSTATION 和 PFR 模式的实际 WebSocket 数据存在；
- Playwright Chromium 真实登录并逐行检查十机数值、持续刷新、站控 P/Q 和调频 false；
- 不以页面 HTTP 200、WebSocket 101 或 Modbus ACK 冒充业务通过。

浏览器口令仅通过当前 Shell 注入，不得写入 Git：
export DCEMS_WEB_PASSWORD='<从本机隔离种子的 admin 记录读取>'
若本机 Chromium 共享库已由 Conda 提供，可在当前 Shell 设置：
export LD_LIBRARY_PATH="$HOME/miniconda3/envs/wind-hub/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

六、AGC/一次调频限制及停止
--------------------------
审查算法真实消费者：
emsGridDispatchAct = AGC 有功目标（kW）；
emsFreDst = 一次调频目标功率（kW），不是 Hz 频率；
emsDeltapValue = 一次调频增量功率（kW）。
这几个 FactoryPoints 的工业协议生产者尚未核实。
因此初始种子中 emsActPowerEnable、emsReactPowerEnable、
bFreCtrlEnableFlag 均为 false，不能验收 DCEMS 自动控制回写。
audit_external_signals.py 会报告阻断项，禁止绕过 Gateway 注入。

停机：
bash tests/third_party/dc_ems/run_windhub.sh --down
bash tests/third_party/dc_ems/run_local.sh --down

禁止 docker compose down -v（会删除隔离数据库和验收证据）。
如出现 Web 200 但 WebSocket 不更新、配置 Loader 失败或容器退出，
结论均为 BLOCKED，必须保存日志、配置哈希与第一处失败堆栈交给生产代码整改。
