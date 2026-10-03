# wind-hub

风电场主控通信模块：协议无关、输出无关的工业通信内核。

## 核心特性

- **协议无关**：通过 ProtocolPort 抽象，支持 ADS / Modbus / IEC 60870-5-104
- **输出无关**：通过 SinkPort 抽象，支持消息中间件 / 文件 / 数据库
- **配置驱动**：YAML 配置 + 采集 Task 声明周期采集与输出去向，零代码切换数据流向
- **多入口**：CLI（typer） + Web API（FastAPI）
- **六边形架构**：Domain 核心零外部依赖，所有 I/O 通过 Port/Adapter 接入

## 架构

```
┌─────────────────────────────────────┐
│         Adapter (inbound)           │
│      CLI / WebAPI                   │
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

## 开发环境

### 前置要求

- Python 3.11+
- Poetry（推荐）或 pip
- conda / venv（可选，用于环境隔离）

### 安装

```bash
# 方式 1：Poetry（推荐）
poetry install --with dev --extras "modbus ads"

# 方式 2：pip
pip install -e ".[modbus,ads]"
```

### Coding Agent / VS Code 插件环境

Codex、Claude Code 作为 VS Code 插件启动时，不假设它们继承某个交互式 shell 或
conda 环境。需要固定本机工具路径时：

```bash
cp .agent/local.example.json .agent/local.json
# 按本机实际路径修改 .agent/local.json
python3 scripts/dev.py env
python3 scripts/dev.py env --frontend
```

`.agent/local.json` 仅保存本机 Python / Node / npm 可执行文件路径，不进仓库。
Agent 执行工具时统一通过：

```bash
python3 scripts/dev.py python -m pytest
python3 scripts/dev.py python -m ruff check .
python3 scripts/dev.py npm --prefix src/wind-hub-admin test
```

### 运行时环境变量

`.env.local` 仅用于 wind-hub 运行时、真实服务或测试参数，不负责 Python
解释器、conda/venv 激活或 Coding Agent 启动。仓库不要求 Coding Agent
`source .env.local`。

`.env.local.example` 是提交到仓库的运行时配置模板；`.env.local` 为本地实际值，
已在 `.gitignore` 中排除。需要这些变量时，由实际运行入口（例如 VS Code launch、
容器、服务管理器或人工 shell）显式注入。

## 快速开始

```bash
wind-hub validate --config configs/template
wind-hub run --config configs/template
wind-hub run --help
```

## 安全提示

> **本模块当前未实现认证**，Web API 默认监听 `127.0.0.1`（仅本机可访问）。
> 请勿将 API 端口直接暴露到公网；如需远程访问，应在反向代理/TLS 层
> 增加认证与加密后方可暴露。

## 开发

```bash
poetry install --with dev --extras "modbus ads"
poetry run pytest
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

## 许可证

TBD
