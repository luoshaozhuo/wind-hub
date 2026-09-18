# wind-hub

风电场主控通信模块：协议无关、输出无关的工业通信内核。

## 核心特性

- **协议无关**：通过 ProtocolPort 抽象，支持 ADS / Modbus / IEC 60870-5-104
- **输出无关**：通过 SinkPort 抽象，支持消息中间件 / 文件 / 数据库
- **配置驱动**：YAML 配置系统 + 路由表，零代码切换数据流向
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
│   Protocol / Sink / Processor       │
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
- conda（可选，用于环境隔离）

### 安装

```bash
# 方式 1：Poetry（推荐）
poetry install --with dev --extras "modbus ads"

# 方式 2：pip
pip install -e ".[modbus,ads]"
```

### 环境变量

本地开发需准备 `.env.local`（本地文件，不进仓库）。从模板复制并激活：

```bash
cp .env.local.example .env.local
conda activate wind-hub
source .env.local
```

- `.env.local.example` 是提交到仓库的模板，含所有可配置项及注释说明；
- `.env.local` 为本地实际值，已在 `.gitignore` 中排除，请勿提交。

## 快速开始

```bash
# 校验配置（无副作用）
wind-hub validate --config configs/

# 前台启动引擎，SIGINT/SIGTERM 优雅停机
wind-hub run --config configs/

# 仅查看 run 子命令参数
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

压测框架在 `tests/perf/` 下：三个协议（Modbus / IEC104 / ADS）都用本地
真实 server 跑完整链路，用 `tc netem` 在专用 veth pair（10.99.0.1 ↔
10.99.0.2）上注入延迟/抖动/丢包/中断，输出 Markdown + JSON 报告
（吞吐、P50/P95/P99 延迟、重连行为、CPU/内存/FD）。

压测需要 **root**（tc/ip 命令），不进入 pytest 默认收集，统一经独立
脚本触发：

```bash
# 快速冒烟（3 场景 × 30 秒，单协议）
sudo /home/luo/miniconda3/envs/wind-hub/bin/python scripts/run_benchmark.py \
    --quick --protocol modbus

# 完整矩阵（3 协议 × 8 场景 × 60 秒 + 预热，约半小时）
sudo /home/luo/miniconda3/envs/wind-hub/bin/python scripts/run_benchmark.py

# 可选参数：--duration / --warmup / --output
```

说明：

- 脚本检测权限，**不会自动 sudo**；无 root 时报告并退出。
- Sink 用 NullSink（隔离外部 IO，测采集 + Pipeline + Router）。
- 资源采样直读 `/proc`（psutil 非项目依赖，刻意零新增依赖）。
- 组件单元测试（全部 mock，不需要 root）在 `tests/unit/perf/`。

## 许可证

TBD