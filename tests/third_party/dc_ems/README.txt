DCEMS 十台风机随机数据独立联测
==============================

基线：wind-hub main@9a17dcc2；第三方发布包 third_party/dc-ems。
目标：DG(random,10) -> Gateway(Modbus) -> DCEMS Server -> Web。
数据源只通过真实 Gateway 工业协议进入 DCEMS。BusinessPoint 变量名
只是 Gateway 点表中的名称，Modbus 读写仍由区号、偏移、类型及字序决定。

一、生成 wind-hub 配置
-----------------------
export WIND_HUB_DCE_GENERATOR_GATEWAY_TOKEN='<本地独立随机32位以上令牌>'
export WIND_HUB_DCE_GATEWAY_OPERATOR_TOKEN='<本地独立随机32位以上令牌>'
export WIND_HUB_DCE_GENERATOR_CLI_TOKEN='<本地独立随机32位以上令牌>'
bash tests/third_party/dc_ems/run_windhub.sh --prepare
bash tests/third_party/dc_ems/run_windhub.sh --check
bash tests/third_party/dc_ems/run_windhub.sh --up

配置固定为 10 台 DCEMS_WTG01..10，端口 15221..15230。
正式 PointTable 的 wind_speed 为 1.3.0 (FC04, F32, ABCD)。
首台 Gateway 还提供 sim_plant_active_power=1.3.2 (W),
sim_plant_reactive_power=1.3.4 (var),
primary_frequency_mode=1.1.0 (FC02, bool)。
DCEMS 逻辑升压站 powAct/powReact 分别通过 coe=0.001 换算为 kW/kVar，
逻辑调频设备 isFreMode 更新 emsIsFreMod。机组总数仍为10台；
这两个辅助对象复用 Gateway01，不属于新增风机。
以上仍不代表49个历史变量及完整AGC/调频算法已经覆盖。

二、生成与上述 Gateway 配置一致的 DCEMS 数据库种子
-------------------------------------------------
mkdir -p run/dcems-smoke
export DCEMS_LICENSE_CODE='<本机真实合法授权码>'
python -m tests.third_party.dc_ems.prepare_seed \
    --seed third_party/dc-ems/app/server/data.json \
    --mapping tests/third_party/dc_ems/config/mapping_random_smoke.csv \
    --gateway-point-table run/dcems-smoke/configs/templates/point_tables/point_table_dcems_wtg_random_v1.0.0.yaml \
    --smoke-subset --with-auxiliary --device-count 10 \
    --output run/dcems-smoke/data.json \
    --gateway-host 127.0.0.1 --gateway-port 15221
unset DCEMS_LICENSE_CODE

派生文件保存在 run/ 下，使用 10 台设备、10 个 Gateway 地址、
10 个风机复用一份风机点表，仅保留 emsWinSpd；
另派生 device_type=1 的升压站和 device_type=2 的调频信号逻辑设备，
共 12 条 DCEMS 逻辑设备记录和 10 个物理 Modbus 通道。
不能直接修改 third_party/dc-ems/app/server/data.json。
初次部署必须使用全新隔离数据库卷。历史数据库不会因更换种子自动重置。
Web 登录所需账户数据来自本地隔离种子，不得把种子、令牌、许可证提交 Git。

三、启动 DCEMS 的本地容器
------------------------
确保本地 image、数据库连接配置、有效授权、JKS 都存在。
无需把大型镜像上传 GitHub。
# 发布包前端 WebSocket 地址在 localhost 下写死旧内网地址；只修隔离运行副本。
python -m tests.third_party.dc_ems.prepare_web \
    --output run/dcems-smoke/web
export DCEMS_TEST_WEB="$(pwd)/run/dcems-smoke/web"
export DCEMS_ALLOW_LOCAL_SMOKE=1
export DCEMS_TEST_SEED="$(pwd)/run/dcems-smoke/data.json"
bash tests/third_party/dc_ems/run_local.sh --check
bash tests/third_party/dc_ems/run_local.sh --up

Web： http://127.0.0.1:18080/dc-ems/
先运行 python -m tests.third_party.dc_ems.audit_external_signals
--seed run/dcems-smoke/data.json --require-closed 来查看未关闭接口（预期退出2）。
Server 默认为 HTTPS 443；测试 Web 由 Nginx 在 18080 代理。
注意：第三方容器使用 host network，先检查 443、5432、18080
和 15221..15230 端口不冲突。

四、执行测试
------------
PYTHONPATH=src python -m pytest \
    tests/third_party/dc_ems/test_seed_contract.py \
    tests/third_party/dc_ems/test_prepare_seed.py \
    tests/third_party/dc_ems/test_auxiliary_signals.py \
    tests/third_party/dc_ems/test_external_signals.py \
    tests/third_party/dc_ems/test_config_smoke.py \
    tests/third_party/dc_ems/test_prepare_web.py -q

DCEMS_LIVE=1 python -m pytest \
    tests/third_party/dc_ems/test_live_smoke.py -q

当前动态协议测试覆盖 10 台风机的风速读数，页面测试仅验证 Web 静态页与
API 代理未返回 502/504，并验证 WebSocket 101 握手。尚未实现浏览器登录后 10 台实时点逐点比对、
DCEMS Web 控制按钮 -> Modbus 写请求 -> Commander -> DG 的执行闭环。
因此当前状态为 IMPLEMENTATION_IN_PROGRESS，不能声称全部指令收发 PASS。

五、清理
--------
bash tests/third_party/dc_ems/run_windhub.sh --down
容器使用同一套 compose 文件执行 down；不要默认 down -v 删除数据库卷。

禁止把 SCADA Redis 同步当作外部实时数据来源。本地镜像存在不代表当前
远端开发环境已执行任何运行时测试。

六、AGC和一次调频源码核对
---------------------------
源代码：dc-ems/main 的 ConfigLoader、SyzDevice、YctpDevice、
FactoryPoints、EmsDispatcher、ActPowerFun、FreActPowerFun。
当前可经设备Modbus采集更新：emsSyzAct、emsSyzReact、emsIsFreMod。
尚无已证实的设备协议→FactoryPoints 更新：emsGridDispatchAct (AGC目标,
kW)、emsFreDst (一次调频目标功率,kW)、emsDeltapValue (功率增量,kW)。
grid_frequency (Hz) 不能代替 emsFreDst；需要另行确定频率→功率转换逻辑。
由于这些目标尚无接口，本轮禁止声明AGC/一次调频控制闭环PASS。
