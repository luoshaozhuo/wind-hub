# DC-EMS 1.1.4-583d5da

构建 Commit：583d5dae6887f2a871f0a99b0c686803258b6877；构建时间：2026-09-12T21:31:48+08:00。

## 发布包与部署源码

仓库 `deploy/` 只保存现场部署所需资源；`tools/release/` 保存构建和打包阶段辅助脚本，不复制进发布包。构建结果保持在 `server/dc-ems/target/`、`web/dist/`。最终发布包仅生成于 `release/`，重新打包仅替换同版本目录及归档。

```text
app/server/dc-ems.jar
app/server/data.json
app/web/
config/log4j.properties
config/db.property.template
config/env.template
config/ssl/README.md
runtime/containerized/compose.yaml
runtime/containerized/web/nginx.conf
runtime/host-based/dc-ems.service
deploy.sh  start.sh  stop.sh  status.sh
MANIFEST.sha256
VERSION  README.md
```

VERSION 来自构建成功生成的 `.build-info`，包含构建开始时 Commit、dirty 状态、构建完成时间及产物摘要。打包校验实际产物摘要，不把打包时 HEAD 当作旧产物来源。dirty=true 表示构建含未提交源码或构建脚本修改，Commit 不是完整源码快照。

## 现场配置

需要 Bash 和 keytool。复制 `config/env.template` 为包根 `.env`，填写密码、镜像和证书 SAN 等参数。仅支持 `KEY=value`，无 Shell 表达式、变量展开或引号规则。实际 `.env`、db.property、JKS 和运行数据不得提交 Git 或重新加入发布归档。

`deploy.sh` 根据模板生成 config/db.property，容器化连接 dc-ems-db:5432，宿主机连接配置的 DB_HOST/DB_PORT。实际 driver 使用 org.postgresql.Driver；应用 DbService 会回写此文件，因此容器通过可写配置目录和符号链接使用它。已有配置冲突时停止，不覆盖密码。

证书按 config/ssl/README.md 提供或生成。预构建 JAR/Web 原样复制，未修改其中内嵌应用默认配置，不复制旧 ems 文件、现场 db.property 或私钥。

## 容器化部署（Containerized Deployment）

构建/打包机在 `package.sh` 内使用已验证的 Java runtime、Nginx 与 TimescaleDB 镜像，导出 `images/dc-ems-server.tar`、`images/dc-ems-web.tar` 与 `images/timescaledb.tar`。现场不再执行 `docker build`，也不再要求 `.env` 包含 `BASE_IMAGE` 或 `DB_IMAGE`。

三个容器职责：

- **dc-ems-web**（Nginx）：对外暴露 80/443，提供 `/dc-ems/` 静态前端，反向代理 `/api/`、`/api/ws`、`/file/` 到 dc-ems-server。
- **dc-ems-server**（Java）：仅内部暴露 8443，处理业务逻辑、文件服务和数据库连接。
- **dc-ems-db**（TimescaleDB）：仅内部暴露 5432，持久化时序数据。

网络链路：`dc-ems-web → dc-ems-server → dc-ems-db`

最终发布包已固定：

- Java runtime: `dc-ems-java:8`（对应已验证的 `jdk:1.8` 运行时）
- Nginx: `dc-ems-nginx:1.26-alpine`（对应已验证的 `nginx:1.26-alpine`）
- TimescaleDB: `dc-ems-timescaledb:2.7.0-pg14`（对应已验证的 `timescale/timescaledb:2.7.0-pg14`）

现场执行 `./deploy.sh --mode containerized`（或 `-m`）时，脚本只做：`docker load` 三个镜像、校验 `MANIFEST.sha256`、准备 `.env`/`db.property`/JKS/Nginx SSL 证书、运行 `docker compose config`，随后 `docker compose up -d --pull never`。数据库及 files/logs/cache/models 用持久化 volume，SSL 外部只读挂载。包目录需长期保留，供配置挂载和管理脚本使用。

Compose 项目固定 dc-ems；拒绝覆盖其他发布目录管理的同名容器。重复部署同一目录允许，但升级迁移、跨目录切换及数据库备份/回滚需要现场安排。

## 宿主机部署（Host-based Deployment）

先安装 Java 8+、PostgreSQL/TimescaleDB、psql、systemd；脚本只验证数据库认证连接，不自动安装数据库。执行 `sudo ./deploy.sh --mode host-based`。安装到固定 `/opt/dc-ems`，以专用 `dc-ems` 用户运行 systemd service，WorkingDirectory 与 Java 相对路径一致，并授予绑定 80/443 的 capability。

已有非本包管理的目录/service 不会覆盖。同包重复部署先停止 service，再更新应用并保留运行数据；没有自动回滚，安装失败需检查现场后重试。改变数据库密码或模式需人工规划；不自动覆盖旧配置。

## 启停与验证边界

部署编排成功后才写 `.deploy-mode`。随后直接使用 `./start.sh`、`./stop.sh`、`./status.sh`，从任意当前目录均可执行；脚本自动识别模式。禁止直接切换已有部署模式。

现场最小依赖如下：

- 容器化部署：Bash、Docker、Docker Compose Plugin、sha256sum、openssl。
- 宿主机部署：Bash、Java、keytool、PostgreSQL/TimescaleDB client、systemd、sha256sum。
- Python 不是现场 Runtime Prerequisite。

当前仓库没有已确认的新版数据库初始化 schema/migration，空数据库只有 PostgreSQL 系统表，不能视为 DC-EMS 业务数据库已就绪。现场仍需确认并导入正确 schema，配置 SCADA/Heart Redis、设备和其他业务数据；本任务不编造初始化来源。容器启动、数据库 SELECT 1 或 systemd active 都不代表 HTTPS/API/数据库业务链路验收通过。

默认 HTTP 重定向假设外部 HTTPS 端口443；若在 Compose 修改 HTTPS_PORT，需另行配置匹配的入口转发策略。正式证书信任、业务验收、备份/恢复、跨版本迁移和回滚不由本轮基础脚本自动完成。