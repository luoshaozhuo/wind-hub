# wind-hub 部署基线

## 定位

`deploy/Dockerfile` 构建共享 OCI 镜像，覆盖 Collector、Commander、
Industrial Gateway、Data Generator、Sink Service 及 CLI。Sink 容器内仍由
Main 管理 Realtime/Reliable 两个 Worker，不拆成三个容器。

`deploy/compose.yaml` 是 **隔离 Linux 主机的便利性集成部署**，并非
跨模块软件准出测试的启动器。完整 E2E 仍须通过正式 CLI 启停业务进程；
中间件由测试框架单独启动。不得将 Compose 直接启动视为 CLI 生命周期覆盖。

镜像可用于 OCI 运行时，但 Docker Compose 创建的容器**不能自动当作**
CLI `manager: containerd` 的原生 Tasks，亦不能当作 iSulad 容器；
两个后端的容器创建和真实启停需分别验证。

## 准备

1. 在隔离 Linux 测试主机安装 Docker Engine/Compose，确认端口可用。
2. 准备私有部署根目录，例如 `/srv/wind-hub`，其中必须有完整的
   `configs/deployments`、`configs/instances`、`configs/templates`、`configs/services`，
   以及已签发的 `certs/` 和可写的 `state/`。
3. 将部署配置中的服务端点、NATS/JetStream、数据库连接、证书身份、
   模型及点表引用调整到同一个真实部署环境。
4. 将 Sink Service 的 `config_root` 设置为 `/etc/wind-hub`，
   并确认它的 `config_sources.instances` 中的逻辑 ID 均能由 Deployment 解析。
5. 使用已发布的 `service_collector-realtime_v1.1.0.yaml` 与
   `service_collector-reliable_v1.0.0.yaml`，由 Deployment 选择版本；
   不再生成无版本号 Collector 配置。
6. 从 `deploy/env.example` 创建私有的 `deploy/.env`，
   将 `WIND_HUB_UID/GID` 设为部署目录的所有者。
   不要把管理 Token 或 TLS 私钥提交入库。

## Deployment 版本选择与重启

CLI 进程及 OCI 初始化参数必须由显式选中的 Deployment 解析，不得扫描目录并自动采用最高版本。更新 Deployment 并发布新 Service 版本后，运维端必须使用 `deploy/render_cli.py --deployment configs/deployments/deployment_<id>_v<version>.yaml` 重新生成 CLI 运行清单；OCI 使用 `deploy/provision.py --deployment ...` 生成相同版本的启动参数。两者均传递 `WIND_HUB_DEPLOYMENT_ID` 与 `WIND_HUB_DEPLOYMENT_VERSION`。旧清单不能自动获得新的 Service 文件启动路径。

Sink 初次启动显式指定 `--service-file`；已有 CURRENT Generation 时，Main 重启按 CURRENT 中唯一版本化 Service 启动，Worker 使用 Main 实际激活 Generation。Compose 仅为独立的便利部署，需要在 `deploy/.env` 中同步更新 Deployment 身份及每个 Service 文件路径（包括 Sink），不得把 Compose 视为配置自动发布或 E2E 启动器。

## 构建与使用

从仓库根目录执行：

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml build
docker compose --env-file deploy/.env -f deploy/compose.yaml config --quiet
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
docker compose --env-file deploy/.env -f deploy/compose.yaml logs --tail=100
docker compose --env-file deploy/.env -f deploy/compose.yaml down
```

进程 `READY` 必须另外通过 gRPC/mTLS 查询确认；容器 `running`
不等于业务就绪。

可执行非生命周期 CLI 操作（CLI YAML 必须引用宿主网络可达的真实端点）：

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml \
  run --rm --no-deps cli --config /etc/wind-hub/cli-runtime.yaml service status
```

## 约束

- 使用 `network_mode: host` 适配既有 `127.0.0.1` 通信配置；
  **仅适用于独占 Linux 测试主机**。不可用于共享宿主机和生产多租户环境。
  实际暴露接口与访问控制由部署配置决定，务必校验防火墙、mTLS 和权限。
- 不自动生成/覆盖生产配置与证书，不自动启动数据库及消息中间件。
- `restart: "no"`，避免容器启动器改变 CLI/E2E 对生命周期的观察。
- 配置目录必须可由指定非 root UID/GID 读写，私钥仅授权服务账户读取；
  共享挂载不替代各服务所需的访问控制与证书权限隔离。
- 不删除任何持久化测试数据；`down` 不添加 `-v`。
- 当前文件属于未验证的部署编码交付：需要线下验证镜像构建、
  各服务 READY、容器停止、持久化和运行时适配器。未验证前不得宣称准出。
