# wind-hub

风电场主控通信模块：协议无关、输出无关的工业通信内核。

## 核心特性

- **协议无关**：通过 ProtocolPort 抽象，支持 ADS / Modbus / IEC 60870-5-104
- **输出无关**：通过 SinkPort 抽象，支持消息中间件 / 文件 / 数据库
- **配置驱动**：YAML 配置 + 采集 Task 声明周期采集与输出去向，零代码切换数据流向
- **多入口**：CLI + Web API（FastAPI）+ gRPC 控制面
- **六边形架构**：Domain 核心零外部依赖，所有 I/O 通过 Port/Adapter 接入

## 模块与分工

| 模块 | 形态 | 职责 |
|------|------|------|
| `src/wind_hub_core` | 共享库 | 跨进程稳定核心：领域模型、静态配置语义、DeviceSession、协议 Driver 与 gRPC 契约（`core.rpc`）、主动验证模型、网络探测。不含运行时、Sink、Web API，禁止依赖任何可执行组件 |
| `src/wind_hub_collector` | 进程 `wind-hub-collector` | 采集运行时：协议接入、采集 Task 调度、Sink 分发，对外提供 gRPC 控制面 |
| `src/wind_hub_commander` | 进程 `wind-hub-commander` | 即时设备命令与诊断进程 |
| `src/wind_hub_server` | 进程 `wind-hub-server` | 管理 API（FastAPI）进程宿主：装配与生命周期；业务 Use Case 复用 collector，不形成第二套后端 |
| `src/wind_hub_ctl` | CLI `wind-hub-ctl` | 只读诊断客户端，只通过 gRPC 管理单个 Collector |
| `src/wind-hub-admin` | 前端（Vue 3 + TS + Vite） | 管理界面 |
| `scripts/` | 工具 | `dev.py`（本机环境入口）、`ci_gate.py`（质量门禁）、`ci_scope.py`（变更范围识别）、`run_benchmark.py`（压测）、`generate_rpc.py`（proto 代码生成） |
| `ai_shared/` | 治理 | Coding Agent 规则、Skill、Hook 与本机工具配置模板，`.claude/`、`.codex/`、`.agents/` 仅为适配层 |
| `tests/` | 测试 | 分层测试体系，见下文「测试」 |

依赖方向由 import-linter 固化：`collector / commander / server / ctl → core`，
core 无出向依赖；进程间协作一律经 `core.rpc` 生成的 gRPC 契约，禁止直接
import 对端进程包。

## 架构

```
┌─────────────────────────────────────┐
│         Adapter (inbound)           │
│      CLI / WebAPI / gRPC            │
├─────────────────────────────────────┤
│         Application                 │
│      Command / Task / Config /      │
│      Status Services                │
├─────────────────────────────────────┤
│         Domain (core)               │
│   ┌──────┐  ┌──────┐  ┌────────┐   │
│   │Model │  │ Port │  │ Engine │   │
│   └──────┘  └──────┘  └────────┘   │
├─────────────────────────────────────┤
│         Adapter (outbound)          │
│   Protocol / Sink                   │
└─────────────────────────────────────┘
```

## 支持的协议

| 协议 | 状态 |
|------|------|
| ADS (Beckhoff) | 已规划 |
| Modbus | 已规划 |
| IEC 60870-5-104 | 已规划 |

## 支持的输出

| 输出类型 | 后端 |
|----------|------|
| 消息中间件 | Kafka |
| 文件 | 本地 / 远程 |
| 数据库 | PostgreSQL / InfluxDB |

### 协议默认值说明

- **ADS 默认 TwinCAT 2**：`ADSConfig.twincat_version` 默认 `"2"`，对应 AMS 端口
  **801**；使用 TwinCAT 3 时需在设备 `endpoint.extensions` 中显式设置
  `twincat_version: "3"`（端口 851）。`target_port` / `ams_port` 显式指定时始终
  优先于版本推导的默认端口。

## 开发环境搭建

### 前置要求

- Python 3.11+（推荐用 conda 建环境）
- Poetry（推荐）或 pip
- Node.js ≥ 20.19（建议 22 LTS；仅前端开发需要，Vite 7 的最低要求）
- Docker（可选，用于 integration 测试的真实 Kafka / PostgreSQL）

### 后端安装

```bash
conda create -n wind-hub python=3.11
conda activate wind-hub

# Poetry（推荐）：依赖锁定在 poetry.lock
poetry install --with dev --extras "modbus ads"

# 或 pip
pip install -e ".[modbus,ads]"
```

可用 extras：`modbus`、`ads`、`iec104`（无依赖占位）、`kafka`、`db`、`all`。

到此开发环境就就绪了，直接使用 `pytest`、`ruff`、`mypy` 等命令即可。

### 前端安装

Node 不在 Poetry 管理范围内，需单独安装。两种方式：

```bash
# 方式 1（推荐）：装进同一个 conda 环境，一次 activate 获得 python + node
conda install -n wind-hub nodejs

# 方式 2：系统级安装（nvm / fnm / 官网安装包），Node ≥ 20.19
```

```bash
npm --prefix src/wind-hub-admin install
npm --prefix src/wind-hub-admin run dev
```

Coding Agent 启动命令时自动激活 conda 环境，装在同一环境内的 Node / npm
随 PATH 一起生效（见下节）。

### Coding Agent 环境（仅 Claude Code / Codex 插件需要）

> 人工开发跳过本节。

Codex、Claude Code 的插件或桌面入口不能依赖交互式终端的激活状态。
共享脚本 `ai_shared/agent_config/activate-env.sh` 通过 `~/miniconda3/etc/profile.d/conda.sh`
执行 `conda activate wind-hub`，同时加载该环境的激活脚本和 Python / Node 工具链。

- Claude Code：`.claude/settings.json` 的 SessionStart hook 把共享脚本登记到
  `CLAUDE_ENV_FILE`，后续 Bash 命令自动加载。
- Codex：`.codex/config.toml` 的 `shell_environment_policy.set.BASH_ENV`
  指向共享脚本，非交互 Bash（包括 login shell）自动加载；本项目关闭
  `shell_snapshot`，避免快照恢复旧 PATH 后覆盖已激活的环境。

更换本机 Conda 安装位置时修改共享脚本；移动仓库或使用独立 worktree 时，
同步修改 Codex 配置里的脚本绝对路径。需要信任该项目并重新启动插件/桌面会话，
以便加载新配置。此适配面向本机 Bash 命令，不替远端执行环境配置 Conda。

`scripts/dev.py env` 验证当前解释器确实属于已激活的 `wind-hub` 环境，
未激活或解释器不匹配时直接报错，不再偷偷切换解释器：

```bash
python3 scripts/dev.py env            # 检查后端环境
python3 scripts/dev.py env --frontend # 连带检查 Node / npm

# Agent 执行工具的入口
python3 scripts/dev.py python -m pytest
python3 scripts/dev.py npm --prefix src/wind-hub-admin test
```

### 运行时环境变量

运行时、真实服务或测试参数统一放在仓库根目录的 `.env.local`（已在
`.gitignore` 中排除，不进仓库）。它不负责 Python 解释器、conda/venv 激活
或 Coding Agent 启动，仓库也不要求 Coding Agent `source .env.local`。

需要这些变量时，由实际运行入口（例如 VS Code launch、容器、服务管理器或
人工 shell）显式注入。

## 快速开始

```bash
# 管理 API + 通信 Runtime（默认 127.0.0.1:8080）
wind-hub-server --config configs/template

# 采集进程（gRPC 控制面默认 127.0.0.1:50051）
wind-hub-collector --config configs/template

# 即时命令与诊断进程（gRPC 默认 127.0.0.1:50052）
wind-hub-commander --config configs/template

# 只读诊断客户端（JSON 输出）
wind-hub-ctl status
wind-hub-ctl devices
wind-hub-ctl tasks
```

更多参数见各入口 `--help`。

## 测试

测试按层级组织在 `tests/` 下，pytest marker 由 `tests/conftest.py` 按第一层
目录自动打标。原则：未配置真实服务的测试以 SKIPPED 报告，**不会自动退化为
mock**。

### 测试分层与执行

| 层级 | 目录 | 内容 | 执行 |
|------|------|------|------|
| unit | `tests/unit` | 单函数/单类逻辑，无网络、无真实外部依赖 | `pytest tests/unit -q` |
| component | `tests/component` | 单进程内多组件协作（允许 fake/mock 外部依赖） | `pytest tests/component -q` |
| contract | `tests/contract` | 模块间稳定契约（RPC / config / API schema） | `pytest tests/contract -q` |
| integration | `tests/integration/{protocols,rpc,sinks}` | 两个以上真实组件协作（真实协议服务 / 文件 / Docker 服务） | `pytest tests/integration -q` |
| system | `tests/system/{acquisition,command,diagnostics,reload,startup,task_control,e2e}` | 完整或接近完整系统的外部可观察行为 | `pytest tests/system -q`（或按子目录单跑） |
| reliability | `tests/reliability/{fault_injection,recovery}` | 故障检测、重试、恢复与资源释放 | `pytest tests/reliability -q -m "not soak"` |
| soak | `tests/reliability/soak` | 长时间稳定性（负载、资源、漂移） | 见下文「Soak」 |
| performance | `tests/performance` | `tc netem` 压测，需 root，不进 pytest 默认收集 | 见下文「性能压测」 |
| hardware | `tests/integration/protocols/ads` | 真实 TwinCAT PLC | `pytest tests/integration/protocols/ads -q -m hardware` |

常用组合：

```bash
# 日常开发 Fast 集合
pytest tests/unit tests/component tests/contract -q

# Release 后端全量（排除 hardware / performance / soak）
pytest tests -q -m "not hardware and not performance and not soak"
```

### 真实外部服务

integration / system 测试的真实服务地址经环境变量或 `tests/test.env`
（不入库）配置：

```bash
cp tests/test.env.example tests/test.env
# 按本机环境修改 tests/test.env
```

- Modbus / IEC104：默认使用 `tests/fixtures/servers/` 的本地协议 fixture。
- Kafka / PostgreSQL：未配置地址时由 `tests/fixtures/services/docker-compose.yml`
  在本机 Docker 拉起；配置后直接使用该实例。
- ADS（hardware）：无默认值，未配置 `WIND_HUB_TEST_ADS_*` 时全部 SKIPPED。

### Soak

```bash
WIND_HUB_SOAK_PROFILE=smoke_1h      pytest -m soak tests/reliability/soak
WIND_HUB_SOAK_PROFILE=prerelease_8h pytest -m soak tests/reliability/soak   # 预发布
WIND_HUB_SOAK_PROFILE=rc_24h        pytest -m soak tests/reliability/soak   # 发布候选
```

必须显式指定 profile；报告写入 `artifacts/soak/`（运行产物，不提交仓库）。

### 前端测试

```bash
# Vitest 单元/组件测试
npm --prefix src/wind-hub-admin test

# Playwright E2E
npm --prefix src/wind-hub-admin run test:e2e
```

### 质量门禁

本地与 GitHub CI 共用 `scripts/ci_gate.py`：

```bash
# Fast Gate：静态检查 / 后端快测 / 前端
python3 scripts/ci_gate.py fast --part backend-static
python3 scripts/ci_gate.py fast --part backend-tests --targets unit,component,contract
python3 scripts/ci_gate.py fast --part frontend

# PR Gate：按变更风险选择的 integration / system 目标
python3 scripts/ci_gate.py pr --targets integration-rpc,system-startup

# 前端 Playwright E2E
python3 scripts/ci_gate.py frontend-e2e

# Release / hardware / performance / soak
python3 scripts/ci_gate.py release --part all
python3 scripts/ci_gate.py hardware
python3 scripts/ci_gate.py performance --mode quick
python3 scripts/ci_gate.py soak
```

PR target 由 `scripts/ci_scope.py` 按变更路径识别；没有 target 的普通变更不
执行额外 integration / system / E2E。

## 静态检查

```bash
poetry run ruff check .
poetry run ruff format --check .
poetry run mypy src/
poetry run lint-imports
```

## 性能压测

压测框架使用 `tc netem` 在专用网络环境注入延迟、抖动、丢包和中断，并输出
Markdown + JSON 报告。压测需要 **root**，不进入 pytest 默认收集。

```bash
PYTHON_BIN="$(python3 scripts/dev.py resolve python)"

sudo "$PYTHON_BIN" scripts/run_benchmark.py --quick --protocol modbus
sudo "$PYTHON_BIN" scripts/run_benchmark.py
```

说明：

- 脚本检测权限，**不会自动 sudo**；无 root 时报告并退出。
- Sink 用 NullSink（隔离外部 IO，测采集 + Task 分发）。
- 资源采样直读 `/proc`（psutil 非项目依赖，刻意零新增依赖）。
- 性能报告属于运行产物，不提交仓库。

## 安全提示

> **本模块当前未实现认证**，Web API 默认监听 `127.0.0.1`（仅本机可访问）。
> 请勿将 API 端口直接暴露到公网；如需远程访问，应在反向代理/TLS 层
> 增加认证与加密后方可暴露。

## 许可证

TBD
