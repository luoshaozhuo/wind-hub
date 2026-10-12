# wind-hub 生产实施前最终验收报告

- 日期：2026-10-12
- 分支：`refactor/production-readiness-final`
- 验收基线 Git SHA：`f40264f4d7f2160929bb2556e4fbd0ceca6e298f`
  （最终 PR 合并前如有新提交，以 PR 页展示的最终 SHA 为准并复核本节标注的增量）
- 执行方式：Claude Code 自动执行，全部门禁本地真实运行，无任何 mock 通过项

## 1. 结论

**CONDITIONAL READY** —— 全部功能、质量、安全、真实链路验收通过；
以下条件项满足后可判 **READY**：

1. 8h（prerelease_8h）与 24h（rc_24h）soak 尚未执行（单次运行合计 32 小时，
   超出本次会话窗口）；1h smoke soak 已通过。执行命令见 §6。
2. tc netem 性能压测（`scripts/run_benchmark.py`）需要 root，本次会话沙箱
   无法 sudo；压测框架已在 Phase 4 修复（netem 空转缺陷）并有可靠性层
   覆盖，但完整性能基线数字需人工以 root 执行一次后归档。命令见 §6。

除上述两项外，无其他未决阻断项。

## 2. 审查与修复总览（Phase 1–3）

| 提交 | 内容 |
|------|------|
| `5bb42a1a` | refactor(core)：修复协议异常语义并清理死代码 |
| `8724bbd1` | refactor(collector)：修复 Sink 隔离与订阅恢复缺陷并清理死代码 |
| `8b6c7e14` | fix(commander,web_server)：修复超时失配与状态接口隔离并清理死代码 |
| `976275cf` | fix(web_admin)：修复写入结果三态契约并补齐运行时运维能力 |
| `3743c17e` | test(reliability,performance,web_server)：修复 netem 空转并补齐测试体系缺口 |
| `6fb602cd` | feat(simulator)：40 设备多协议 PLC 模拟器与真实配置树 |
| `1ec2fae2` | test(system)：一键验收环境与真实全链路 E2E |
| `f40264f4` | fix(security)：依赖漏洞清零与验收环境暴露面收敛 |

关键业务语义修复（摘录）：

- **命令三态契约**：COMMAND_OUTCOME_CONFIRMED_SUCCESS / CONFIRMED_FAILURE /
  UNKNOWN 端到端贯通（Commander → Web Server → Web Admin），UNKNOWN 在 UI
  明确提示「命令可能已执行，禁止盲目重试」，并展示 `command_id` 供审计对账。
- **Commander/Web Server 超时失配**：HTTP 硬超时与 gRPC 超时对齐，消除
  「网关已超时但命令仍在执行」的误判窗口。
- **Collector Sink 隔离**：任务 targets 与 sink 启用状态严格一致校验；
  订阅模式断连恢复语义修复。
- **Web Server 状态接口隔离**：状态查询不再与控制操作共享失败域。

## 3. 测试体系（Phase 4）

- 分层：unit / component / contract / integration / system / reliability /
  soak / performance / hardware，marker 由 `tests/conftest.py` 按目录自动打标。
- 原则落实：未配置真实服务的用例以 SKIPPED 报告，**不退化为 mock、不计入
  PASS**；本报告所有通过项均为真实执行。
- 修复的测试基础设施缺陷：同命名空间 veth netem 空转（压测无效化）、
  netem 命名空间子进程默认行为、`aiokafka 0.14.0` stop 泄漏、Playwright
  spec 模块解析（`@playwright/test` walk-up 不可达）。

## 4. 模拟器（Phase 5）

- `scripts/simulator/`：20 Modbus + 10 ADS + 10 IEC104 = 40 台真实协议栈
  模拟 PLC；seeded 数据生成（可复现）、fault/recover 故障注入（down 注入
  主动断开客户端连接，呈现真实「设备断电」语义而非半开连接）。
- `configs/simulation/`：由 `scripts/simulator/gen_config.py` 生成的真实
  Core 配置树（`load_domain` 校验通过），IEC104 控制点使用 ASDU 命令类型
  ID（C_SE_NC_1 / C_SC_NA_1）满足 writable 契约。

## 5. 真实全链路验收（Phase 6）

`python3 scripts/acceptance.py start` 一条命令拉起：模拟器（40 台）→
Collector → Commander → Web Server（可选 --with-redis 真实 Redis sink、
--with-frontend Vite dev server）。

**Python 系统测试**（`tests/system/e2e/full_chain`，4/4 通过，73s）：

1. 40 台设备经三协议真实连接全部 connected；
2. 任务启动后数据真实落入 CSV sink 与 Modbus SCADA sink（pymodbus 客户端
   回读 holding register 得到实时功率值）；
3. Commander 写入读回环（写 666.25 → CONFIRMED_SUCCESS → 回读一致）；
4. 故障注入 sim-mb-05 down → 设备离线 → 写入 CONFIRMED_FAILURE → recover
   恢复。

**真实浏览器 E2E**（Playwright，`tests/system/web_admin/real_chain.spec.ts`，
3/3 通过）：设备列表与连接状态、任务页启动采集任务、写入三态展示
（「Command succeeded」+ command_id）与最新值回读 777.7。

## 6. 性能与浸泡（Phase 7）

| 档位 | 状态 | 证据 |
|------|------|------|
| smoke_1h | ✅ 通过 | `artifacts/soak/soak_report_smoke_1h.md/.json`（运行产物不入库）：540 万点零丢失/零重复/零错过周期，吞吐 1500 点/s，fast 组 P99 间隔 0.1013s，内存峰值 64.7MB，CPU 1.3% |
| prerelease_8h | ⏳ 未执行 | `WIND_HUB_SOAK_PROFILE=prerelease_8h pytest -m soak tests/reliability/soak` |
| rc_24h | ⏳ 未执行 | `WIND_HUB_SOAK_PROFILE=rc_24h pytest -m soak tests/reliability/soak` |
| tc netem 压测 | ⏳ 需 root | `sudo "$PYTHON_BIN" scripts/run_benchmark.py --quick --protocol modbus` |

smoke_1h 判据（全部满足）：零背压丢弃、零重复、错过周期率 < 1%、内存
增长有界（< +20% 且 < +100MB）、asyncio 任务数不爬升。

> 环境注意：soak 专用 Modbus 端口 10512 与模拟器 sim-mb-11 端口冲突，
> soak 与 `scripts/acceptance.py` / full_chain 系统测试须串行执行。

## 7. 可观测性核查

- 统一进程日志：UTC ISO-8601 时间戳 + 级别 + logger 名
  （`core/infrastructure/diagnostic/log_setup.py`）。
- Web Server：每请求结构化访问日志（request_id / method / route 模板 /
  status / duration_ms），控制操作追加 `control_audit` 审计行；响应携带
  `X-Request-ID`；不记录查询参数、请求体与凭据。
- 健康与指标：`/health/live`、`/api/v1/collector/metrics`（采集/路由/丢弃
  计数、队列深度）、设备连接状态接口。
- Collector 运行时计数：points_collected / routed / dropped，soak 以这些
  计数做零丢弃断言。

## 8. 安全核查

- **依赖漏洞**：`pip-audit` 零已知漏洞（升级 fastapi 0.143.0 /
  starlette 1.7.0 / pytest 9.1.1 / pytest-asyncio 1.4.0 后复扫）。
- **暴露面**：Web API 与 gRPC 控制面默认绑定 127.0.0.1；Modbus SCADA
  上行 sink 验收配置收敛至 127.0.0.1（生产 schema 默认 0.0.0.0，README
  安全提示已补充部署指引）。
- **写保护**：Web 写接口由 `WIND_HUB_WEB_WRITE_TOKEN`（`X-API-Key`，
  `compare_digest` 常量时间比较）保护；未配置时写接口整体 503 禁用。
- **凭据卫生**：仓库无硬编码凭据（扫描通过）；`.env.local` 不入库且
  本验收未读取任何敏感值；验收环境写令牌每次启动随机生成。
- **已知边界**（设计内，README 已声明）：gRPC 控制面无认证，远程部署
  必须网络隔离 + 反向代理/TLS。

## 9. 质量门禁终态（基线 SHA 上全部真实执行）

| 门禁 | 结果 |
|------|------|
| `ruff check src tests scripts` | ✅ 0 问题 |
| `mypy src`（strict） | ✅ 134 文件 0 问题 |
| `pytest tests/unit tests/component tests/contract` | ✅ 842 passed |
| `lint-imports` | ✅ 9 kept / 0 broken |
| 前端 vitest | ✅ 6 passed |
| 前端 build / lint / format:check / check:api | ✅ |
| Playwright shell 冒烟 | ✅ 1 passed |
| Playwright 真实后端 E2E | ✅ 3 passed |
| 系统测试（acquisition/command/full_chain） | ✅ 11 passed |
| soak smoke_1h | ✅ 通过（判据见 §6） |
| pip-audit | ✅ 0 已知漏洞 |

## 10. 遗留 P2（不阻断，已记录）

- Collector 订阅模式 stale 数据检测（建议文档化或后续实现）。
- Web/CLI int64 序列化一致性（建议文档化）。
- ADS 订阅池重建时的重连噪声退避。
- IEC104/ADS 的长 soak 与 netem 压测矩阵覆盖（当前 soak/perf 以 Modbus
  为主链路）。
- `influxdb-client` optional 依赖当前无使用方（保留，未强行删除）。
