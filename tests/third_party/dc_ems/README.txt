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

对应设备 DCEMS_WTG01..10；监听 127.0.0.1:15221..15230。
Gateway 点键为正式 BusinessPoint 变量名：
  wind_speed = 1.3.0, FC04 F32 ABCD, m/s（每台）
  sim_plant_active_power = 1.3.2, FC04 F32 ABCD, W（首台）
  sim_plant_reactive_power = 1.3.4, FC04 F32 ABCD, var（首台）
  primary_frequency_mode = 1.1.0, FC02 bool（首台）
Power 的 0.001 系数在 DCEMS edgeaddrset 将 W/var 转为 kW/kVar。
Modbus 地址只作为协议契约，变量名并不进入 Modbus 报文。
首台随机 P/Q 是独立试验激励，不是 10 台机组功率之和。

三、生成独立 DCEMS 数据库和 Web 资源
-----------------------------------
本地发布包中应已有合法的 .env、JKS、Server/Web/DB 镜像和授权。
不要从 GitHub 下载大型镜像，也不要覆盖旧数据库。

python -m tests.third_party.dc_ems.prepare_runtime \
  --source third_party/dc-ems \
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
  --gateway-host 127.0.0.1 --gateway-port 15221 \
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
若 dcems-smoke_database 卷已存在，启动会拒绝静默复用。
只有核实该卷就是本次测试且无需重新导入种子时，才允许本地设置
DCEMS_SMOKE_ALLOW_REUSE=1；禁止直接删除其他 Compose 项目的数据库卷。

export DCEMS_ALLOW_LOCAL_SMOKE=1
export DCEMS_TEST_SEED="$PWD/run/dcems-smoke/data.json"
export DCEMS_TEST_WEB="$PWD/run/dcems-smoke/web"
export DCEMS_RUNTIME_DIR="$PWD/run/dcems-smoke/docker"
bash tests/third_party/dc_ems/run_local.sh --check
bash tests/third_party/dc_ems/run_local.sh --up

启动脚本根据 third_party/dc-ems/VERSION 的 release_version
寻找 dc-ems-server、dc-ems-web 的镜像标签，不执行 Docker pull。
Web: http://127.0.0.1:18080/dc-ems/
Server: https://127.0.0.1:443 （自签名证书仅测试用途）。
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
- DCEMS SUBSTATION 和 PFR 模式的实际 WebSocket 数据存在；
- 人工登录 Web，确认 10 台机组显示及全场界面，不以页面 HTTP 200 冒充通过。

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
