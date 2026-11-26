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
正式 PointTable 只有一个可测量业务点 wind_speed，Modbus 地址 1.3.0
(FC04，float32，ABCD)，每台随机风速独立变化。
这是首轮随机采集烟雾测试，不代表 49 个 DCEMS 历史点已经覆盖。

二、生成与上述 Gateway 配置一致的 DCEMS 数据库种子
-------------------------------------------------
mkdir -p run/dcems-smoke
export DCEMS_LICENSE_CODE='<本机真实合法授权码>'
python -m tests.third_party.dc_ems.prepare_seed \
    --seed third_party/dc-ems/app/server/data.json \
    --mapping tests/third_party/dc_ems/config/mapping_random_smoke.csv \
    --gateway-point-table run/dcems-smoke/configs/templates/point_tables/point_table_dcems_wtg_random_v1.0.0.yaml \
    --smoke-subset --device-count 10 \
    --output run/dcems-smoke/data.json \
    --gateway-host 127.0.0.1 --gateway-port 15221
unset DCEMS_LICENSE_CODE

派生文件保存在 run/ 下，使用 10 台设备、10 个 Gateway 地址、
共用一份 edgeaddrset 地址集，仅保留 emsWinSpd 一个采集变量。
不能直接修改 third_party/dc-ems/app/server/data.json。
初次部署必须使用全新隔离数据库卷。历史数据库不会因更换种子自动重置。
Web 登录所需账户数据来自本地隔离种子，不得把种子、令牌、许可证提交 Git。

三、启动 DCEMS 的本地容器
------------------------
确保本地 image、数据库连接配置、有效授权、JKS 都存在。
无需把大型镜像上传 GitHub。
export DCEMS_ALLOW_LOCAL_SMOKE=1
export DCEMS_TEST_SEED="$(pwd)/run/dcems-smoke/data.json"
bash tests/third_party/dc_ems/run_local.sh --check
bash tests/third_party/dc_ems/run_local.sh --up

Web： http://127.0.0.1:18080/dc-ems/
Server 默认为 HTTPS 443；测试 Web 由 Nginx 在 18080 代理。
注意：第三方容器使用 host network，先检查 443、5432、18080
和 15221..15230 端口不冲突。

四、执行测试
------------
PYTHONPATH=src python -m pytest \
    tests/third_party/dc_ems/test_seed_contract.py \
    tests/third_party/dc_ems/test_prepare_seed.py -q

DCEMS_LIVE=1 python -m pytest \
    tests/third_party/dc_ems/test_live_smoke.py -q

当前动态协议测试覆盖 10 台风机的风速读数，页面测试仅验证 Web 静态页与
API 代理未返回 502/504。尚未实现浏览器登录后 10 台实时点逐点比对、
DCEMS Web 控制按钮 -> Modbus 写请求 -> Commander -> DG 的执行闭环。
因此当前状态为 IMPLEMENTATION_IN_PROGRESS，不能声称全部指令收发 PASS。

五、清理
--------
bash tests/third_party/dc_ems/run_windhub.sh --down
容器使用同一套 compose 文件执行 down；不要默认 down -v 删除数据库卷。

禁止把 SCADA Redis 同步当作外部实时数据来源。本地镜像存在不代表当前
远端开发环境已执行任何运行时测试。
