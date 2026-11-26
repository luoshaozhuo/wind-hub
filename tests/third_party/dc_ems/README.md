# DCEMS 独立容器联测（实施中）

本目录属于 `tests/third_party/dc_ems/`。来源为 wind-hub `main@9a17dcc2` 和第三方发布包 `1.1.4-583d5da`。正式设计书位于 `docs/design/DCEMS_跨系统测试设计_v1_2.md`。

**状态：静态契约、种子派生、Web 容器覆盖层已提交；完整链路尚未通过真实环境测试。** 本目录不会修改 `third_party/dc-ems/app/server/data.json`、JAR、现有 `main` 或现场数据库。所有外部实时量只允许通过 Gateway 的工业协议进入 DCEMS，不使用 SCADA Redis。

## 1. 静态检查

```bash
python -m pytest tests/third_party/dc_ems/test_seed_contract.py tests/third_party/dc_ems/test_prepare_seed.py -q
```

## 2. BusinessPoint 逐点配置

DCEMS 内部变量仍用 `edgeaddrset.varPath`，Gateway 对外使用正式 PointTable 的 `business_point_id`。两者通过**真实协议地址**绑定，不能用相同名字推断相同语义。

```bash
mkdir -p run/dcems-smoke
python tests/third_party/dc_ems/export_mapping_template.py \
  --seed third_party/dc-ems/app/server/data.json \
  --output run/dcems-smoke/mapping.csv
```

在 `mapping.csv` 的 `business_point_id` 列填写对应的 Gateway 正式业务点名，并核对 `modbus_addr` 是否与 Gateway PointTable 的实际区号、偏移及字序一致；**默认历史地址不代表当前 Gateway 配置**。DCEMS `41555/41557/41561` 存在读写共址别名，Gateway 端只能保留一个寄存器映射，别名须使用同一个业务点 ID。当前工具故意拒绝未填写或不完整的映射。

```bash
export DCEMS_LICENSE_CODE='<仅本地合法授权码>'
python tests/third_party/dc_ems/prepare_seed.py \
  --seed third_party/dc-ems/app/server/data.json \
  --mapping run/dcems-smoke/mapping.csv \
  --output run/dcems-smoke/data.json \
  --gateway-host 127.0.0.1 \
  --gateway-port 15221
unset DCEMS_LICENSE_CODE
```

上一步只产生隔离种子，不会修改既有数据库。首次启动时 DCEMS 的 `DbStructService` 只对**不存在的表**执行种子初始化。因此必须使用新建的专用 Docker 数据卷；复用老数据库不会自动替换设备和点表。原始 `userInfos` 可能带历史账号资料，仅允许在隔离机器上的受控测试环境使用，不能上传派生文件。

## 3. 本地容器检查与启动

远端未存储镜像 tar。需要先在本机准备以下三个镜像，标签可通过环境变量覆盖：`dc-ems-java:8`、`dc-ems-nginx:1.26-alpine`、`dc-ems-timescaledb:2.7.0-pg14`。

准备本地 `third_party/dc-ems/.env`、`config/db.property`、`config/ssl/dc-ems.jks`，并保证许可证对**当前容器机器码**有效；所有机密文件不得提交。现有 Server 容器使用 host network，默认占用主机 HTTPS 443；数据库映射 5432；测试 Web 占用主机 18080，启动前必须确认端口不冲突。

```bash
export DCEMS_ALLOW_LOCAL_SMOKE=1
export DCEMS_TEST_SEED="$(pwd)/run/dcems-smoke/data.json"
bash tests/third_party/dc_ems/run_local.sh --check
bash tests/third_party/dc_ems/run_local.sh --up
```

浏览器访问 `http://127.0.0.1:18080/dc-ems/`，但页面打开**不等于**数据/指令全链路通过。

```bash
DCEMS_LIVE=1 DCEMS_GATEWAY_HOST=127.0.0.1 DCEMS_GATEWAY_PORT=15221 \
  python -m pytest tests/third_party/dc_ems/test_live_smoke.py -q
```

`test_live_smoke.py` 当前只验证 Web 静态页、API 代理及 Gateway 动态 Modbus 测点；未覆盖实际登录/全部测点及命令写回。只有完成这些额外真实浏览器与协议验证后，才可给出完整联测 PASS。

## 4. 当前阻断与验收

- 需要生成实际 BusinessPoint↔DCEMS `varPath`↔协议地址的审核映射，且与 wind-hub 真实 Gateway PointTable 完整匹配。
- 需要生成 DG random 场景以及匹配的 Gateway 模型、点表、实例、服务与 Deployment；不可通过测试直接写 Gateway 伪装为 DG 数据源。
- DCEMS 的合法授权、初始用户账号与 Web 操作权限必须来自隔离部署环境。
- 必须观察 DCEMS Server 真实读取、Web 实时刷新、Web 控制→DCEMS Modbus 写→Gateway→Commander→DG 的独立执行回执。
- 本地 Docker 镜像缺失于远端，自动化环境无法重放容器启动；不提供运行成功声明。

测试停止后，请使用同一 Compose 文件集合执行 `down`，**不要默认使用 `down -v`**，以免误删仍需保存的试验数据库。
