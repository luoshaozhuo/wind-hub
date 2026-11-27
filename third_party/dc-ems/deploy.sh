#!/usr/bin/env bash
# 现场部署入口：只使用 Bash，不能 source .env 或调用外部解释器。
set -e
set -o pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly ROOT
source "${ROOT}/lib.sh"
INSTALL_ROOT="${DC_EMS_INSTALL_ROOT:-/opt/dc-ems}"
readonly INSTALL_ROOT
SYSTEMD_UNIT="${DC_EMS_SYSTEMD_UNIT:-/etc/systemd/system/dc-ems.service}"
readonly SYSTEMD_UNIT

require() {
    command -v "$1" >/dev/null 2>&1 || fail "缺少 ${1}，请先安装对应运行环境"
}

ensure_env_file() {
    local path="${ROOT}/.env"
    if [[ ! -f "${path}" ]]; then
        cp -- "${ROOT}/config/env.template" "${path}"
        chmod 600 "${path}"
        fail '已生成 .env，请填写现场配置后重新执行 ./deploy.sh --mode ...'
    fi
}

run_app_keytool() {
    "${DOCKER}" run --rm \
        --user "$(id -u):$(id -g)" \
        --entrypoint /opt/java/openjdk/bin/keytool \
        -v "${ROOT}/config/ssl:/ssl" \
        "${SERVER_IMAGE}" \
        "$@"
}

write_db_property() {
    local mode="$1"
    local host port con expected path current
    path="${ROOT}/config/db.property"
    if [[ "${mode}" == "containerized" ]]; then
        host='127.0.0.1'; port='5432'
    else
        host="${DB_HOST}"; port="${DB_PORT}"
    fi
    con="jdbc:postgresql://${host}:${port}/${DB_NAME}?currentSchema=public&timezone=Asia/Shanghai"
    expected="$(printf 'con=%s\nuser=%s\npasswd=%s\ndriver=org.postgresql.Driver\n' \
        "${con}" "${DB_USER}" "${DB_PASSWORD}")"
    if [[ -f "${path}" ]]; then
        current="$(tr -d '\r' < "${path}")"
        [[ "${current}" == "${expected}" ]] || fail '已有 config/db.property 与当前 .env/部署模式不一致，拒绝静默覆盖'
    else
        printf '%s' "${expected}" > "${path}"
        chmod 600 "${path}"
    fi
}

ensure_jks() {
    local mode="$1"
    local ssl_dir="${ROOT}/config/ssl"
    local cert="${ssl_dir}/dc-ems.jks"
    mkdir -p "${ssl_dir}"
    chmod 700 "${ssl_dir}"

    if [[ -f "${cert}" ]]; then
        if [[ "${mode}" == "host-based" ]]; then
            require keytool
            keytool -list -storetype JKS -keystore "${cert}" -storepass "1*@3789HZ-dcems" >/dev/null 2>&1 || fail '已存在 dc-ems.jks 但无法按当前契约读取'
        else
            "${DOCKER}" image inspect "${SERVER_IMAGE}" >/dev/null 2>&1 || fail '容器化部署所需服务端镜像未加载'
            run_app_keytool -list -storetype JKS -keystore /ssl/dc-ems.jks -storepass '1*@3789HZ-dcems' >/dev/null 2>&1 || fail '已存在 dc-ems.jks 但无法按当前契约读取'
        fi
        chmod 600 "${cert}"
        return 0
    fi

    if [[ "${mode}" == "host-based" ]]; then
        require keytool
        keytool -genkeypair -alias dc-ems -keyalg RSA -keysize 2048 -storetype JKS -keystore "${cert}" -storepass '1*@3789HZ-dcems' -keypass '1*@3789HZ-dcems' -dname 'CN=DC-EMS' -ext "SAN=${JKS_SAN}" -validity 365 -noprompt >/dev/null 2>&1
    else
        "${DOCKER}" image inspect "${SERVER_IMAGE}" >/dev/null 2>&1 || fail '容器化部署所需服务端镜像未加载'
        run_app_keytool -genkeypair -alias dc-ems -keyalg RSA -keysize 2048 -storetype JKS -keystore /ssl/dc-ems.jks -storepass '1*@3789HZ-dcems' -keypass '1*@3789HZ-dcems' -dname 'CN=DC-EMS' -ext "SAN=${JKS_SAN}" -validity 365 -noprompt >/dev/null 2>&1
    fi
    chmod 600 "${cert}"
}

ensure_nginx_ssl() {
    local ssl_dir="${ROOT}/config/ssl"
    local cert="${ssl_dir}/dc-ems.crt"
    local key="${ssl_dir}/dc-ems.key"

    if [[ -f "${cert}" && -f "${key}" ]]; then
        # 已有 PEM 证书，验证是否可读
        openssl x509 -in "${cert}" -noout >/dev/null 2>&1 || fail '已存在 dc-ems.crt 但无法解析'
        openssl rsa -in "${key}" -check -noout >/dev/null 2>&1 || fail '已存在 dc-ems.key 但无法解析'
        chmod 600 "${cert}" "${key}"
        return 0
    fi

    require openssl
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout "${key}" -out "${cert}" \
        -days 365 -subj '/CN=DC-EMS' \
        -addext "subjectAltName=${JKS_SAN}" >/dev/null 2>&1
    chmod 600 "${cert}" "${key}"
}

get_machine_code() {
    # 在服务器容器内运行 dmidecode 获取主板 ID，然后通过 Java 生成机器码。
    # 输出：RSA 加密后的机器码字符串，供管理员生成授权码。
    "${DOCKER}" run --rm \
        --entrypoint /opt/java/openjdk/bin/java \
        "${SERVER_IMAGE}" \
        -cp dc-ems.jar cn.hedpy.scada.license.LiscenceUtil machine-code DCEMS
}

ensure_license() {
    # 检查数据库中是否已有授权码；若没有且 .env 中提供了 LICENSE_CODE 则写入，
    # 否则获取机器码并提示用户向管理员申请。
    local license_in_db
    license_in_db="$("${DOCKER}" exec -i dc-ems-db psql -U "${DB_USER}" -d "${DB_NAME}" -t -A -c \
        "SELECT \"val\" FROM ems_config WHERE \"id\" = 'license';" 2>/dev/null || true)"
    license_in_db="$(trim "${license_in_db}")"

    if [[ -n "${license_in_db}" ]]; then
        printf '检测到已有授权码，跳过授权流程。\n'
        return 0
    fi

    if [[ -n "${LICENSE_CODE:-}" ]]; then
        # 将用户提供的授权码写入数据库
        printf '正在写入授权码...\n'
        "${DOCKER}" exec -i dc-ems-db psql -U "${DB_USER}" -d "${DB_NAME}" >/dev/null 2>&1 <<SQL
INSERT INTO ems_config ("id", "val", "remark") VALUES ('license', '${LICENSE_CODE}', 'license code')
ON CONFLICT ("id") DO UPDATE SET "val" = EXCLUDED."val";
SQL
        printf '授权码已写入。\n'
        return 0
    fi

    # 无授权码：获取机器码，提示用户
    local machine_code
    machine_code="$(get_machine_code)" || fail '无法获取机器码，请确认服务器镜像已正确加载且 dmidecode 可用'

    fail "未提供授权码。请将以下机器码发送给管理员获取授权码，\n\
然后将授权码填入 .env 的 LICENSE_CODE 字段后重新执行 deploy.sh：\n\
\n\
------机器码------\n\
${machine_code}\n\
------------------"
}

validate_release() {
    [[ -f "${ROOT}/MANIFEST.sha256" ]] || fail '缺少 MANIFEST.sha256，无法执行安全部署'
    (cd "${ROOT}" && sha256sum -c MANIFEST.sha256 >/dev/null) || fail 'MANIFEST.sha256 校验失败，发布包不完整'
}

load_images() {
    detect_container_runtime
    "${DOCKER}" load -i "${ROOT}/images/dc-ems-server.tar" >/dev/null
    "${DOCKER}" load -i "${ROOT}/images/timescaledb.tar" >/dev/null
    # 按 image ID 导出的镜像加载后无标签；找到悬空镜像并打标签
    if ! "${DOCKER}" image inspect "dc-ems-timescaledb:2.7.0-pg14" >/dev/null 2>&1; then
        local dangling_id
        dangling_id="$("${DOCKER}" images --format '{{.ID}}' --filter 'dangling=true' 2>/dev/null | head -1)"
        if [[ -n "${dangling_id}" ]]; then
            "${DOCKER}" tag "${dangling_id}" "dc-ems-timescaledb:2.7.0-pg14"
        fi
    fi
    "${DOCKER}" image inspect "${SERVER_IMAGE}" >/dev/null 2>&1 || fail 'dc-ems-server 镜像未成功导入'
    "${DOCKER}" image inspect "dc-ems-timescaledb:2.7.0-pg14" >/dev/null 2>&1 || fail 'TimescaleDB 镜像未成功导入'
}

write_sys_config() {
    # 将主备、Redis 等运行参数写入 sys_config 表，server 重启后生效。
    local db_container="${1:-dc-ems-dc-ems-db-1}"
    local max_wait=60 start_time elapsed

    # 等待 sys_config 表就绪（server 启动时异步创建）
    start_time="$(date +%s)"
    while true; do
        if "${DOCKER}" exec -i "${db_container}" psql -U "${DB_USER}" -d "${DB_NAME}" -c 'SELECT 1 FROM sys_config LIMIT 1' >/dev/null 2>&1; then
            break
        fi
        elapsed="$(($(date +%s) - start_time))"
        if [[ "${elapsed}" -ge "${max_wait}" ]]; then
            printf '  ⚠ sys_config 表在 %d 秒内未就绪，跳过运行参数写入\n' "${max_wait}" >&2
            return 0
        fi
        sleep 2
    done

    # HTTPS 监听端口（覆盖 data.json 默认 443）
    printf '写入服务端口配置：web.listenPort=%s\n' "${SERVER_PORT}"
    "${DOCKER}" exec -i "${db_container}" psql -U "${DB_USER}" -d "${DB_NAME}" 2>&1 <<SQL
UPDATE sys_config SET item_value = '${SERVER_PORT}' WHERE group_name = 'web' AND item_name = 'listenPort';
SQL

    # RMI 端口
    printf '写入 RMI 端口配置：rmi.port=%s\n' "${RMI_PORT}"
    "${DOCKER}" exec -i "${db_container}" psql -U "${DB_USER}" -d "${DB_NAME}" 2>&1 <<SQL
INSERT INTO sys_config (group_name, item_name, item_value, descr)
SELECT 'rmi', 'port', '${RMI_PORT}', 'RMI 主备通信端口'
WHERE NOT EXISTS (
    SELECT 1 FROM sys_config WHERE group_name = 'rmi' AND item_name = 'port'
);
SQL

    # 节点角色
    printf '写入节点角色：nodeRole=%s\n' "${NODE_ROLE}"
    "${DOCKER}" exec -i "${db_container}" psql -U "${DB_USER}" -d "${DB_NAME}" 2>&1 <<SQL
INSERT INTO sys_config (group_name, item_name, item_value, descr)
SELECT 'app', 'nodeRole', '${NODE_ROLE}', '节点角色 primary/standby'
WHERE NOT EXISTS (
    SELECT 1 FROM sys_config WHERE group_name = 'app' AND item_name = 'nodeRole'
);
SQL

    if [[ -n "${PEER_HOST:-}" ]]; then
        printf '写入主备配置：rmi_listenHost=%s\n' "${PEER_HOST}"
        "${DOCKER}" exec -i "${db_container}" psql -U "${DB_USER}" -d "${DB_NAME}" 2>&1 <<SQL
INSERT INTO sys_config (group_name, item_name, item_value, descr)
SELECT 'rmi', 'listenHost', '${PEER_HOST}', '主备 RMI 主机 IP'
WHERE NOT EXISTS (
    SELECT 1 FROM sys_config WHERE group_name = 'rmi' AND item_name = 'listenHost'
);
SQL
    fi
    if [[ -n "${REDIS_HOST:-}" ]]; then
        local redis_con="${REDIS_HOST},${REDIS_PORT:-6379},${REDIS_PASSWORD:-},${REDIS_DB:-0}"
        "${DOCKER}" exec -i "${db_container}" psql -U "${DB_USER}" -d "${DB_NAME}" 2>&1 <<SQL
INSERT INTO sys_config (group_name, item_name, item_value, descr)
SELECT 'sync', 'redisCon', '${redis_con}', 'SCADA 数据同步 Redis 连接'
WHERE NOT EXISTS (
    SELECT 1 FROM sys_config WHERE group_name = 'sync' AND item_name = 'redisCon'
);
INSERT INTO sys_config (group_name, item_name, item_value, descr)
SELECT 'watch', 'heartRedisCon', '${redis_con}', '心跳 Redis 连接'
WHERE NOT EXISTS (
    SELECT 1 FROM sys_config WHERE group_name = 'watch' AND item_name = 'heartRedisCon'
);
SQL
    fi

    # 重启 server 使其读取新的 sys_config（端口/主备/Redis 变更需重启生效）
    "${DOCKER}" restart dc-ems-dc-ems-server-1 >/dev/null 2>&1 || true
}

container_deploy() {
    require_compose
    require sha256sum
    ensure_env_file
    parse_env
    prepare_compose_env

    # 已部署：跳过镜像加载、证书生成、容器启动，仅处理授权码更新
    if [[ -f "${ROOT}/.deploy-mode" ]]; then
        local current
        current="$(awk -F= '/^mode=/{print $2; exit}' "${ROOT}/.deploy-mode")"
        [[ "${current}" == "containerized" ]] || fail '已按宿主机模式部署，不可切换为容器化模式'
        "${DOCKER}" image inspect "${SERVER_IMAGE}" >/dev/null 2>&1 || fail '已部署但镜像缺失，请检查'
        run_compose --project-name dc-ems --project-directory "${ROOT}" --env-file /dev/null -f "${ROOT}/runtime/containerized/compose.yaml" ps --status running | grep -q 'dc-ems-db' || fail '已有 .deploy-mode 但容器未运行，请先执行 ./start.sh'
        ensure_license
        printf '授权码检查完成。\n'
        return 0
    fi

    validate_release
    load_images
    write_db_property containerized
    ensure_jks containerized
    run_compose --project-name dc-ems --project-directory "${ROOT}" --env-file /dev/null -f "${ROOT}/runtime/containerized/compose.yaml" config >/dev/null
    run_compose --project-name dc-ems --project-directory "${ROOT}" --env-file /dev/null -f "${ROOT}/runtime/containerized/compose.yaml" up -d --no-build --pull never >/dev/null
    _health_check 'dc-ems-db' 60 || fail '容器化部署后未发现 dc-ems-db'
    _health_check 'dc-ems-server' 120 || fail '容器化部署后未发现 dc-ems-server'
    write_sys_config
    ensure_license
    printf 'mode=containerized\n' > "${ROOT}/.deploy-mode"
}

_health_check() {
    local name="$1" max_wait="${2:-60}" elapsed
    local compose_args=(
        --project-name dc-ems
        --project-directory "${ROOT}"
        --env-file /dev/null
        -f "${ROOT}/runtime/containerized/compose.yaml"
    )
    local start_time
    start_time="$(date +%s)"
    while true; do
        if run_compose "${compose_args[@]}" ps --status running 2>/dev/null | grep -q "${name}"; then
            printf '  ✓ %s 已运行\n' "${name}"
            return 0
        fi
        elapsed="$(($(date +%s) - start_time))"
        if [[ "${elapsed}" -ge "${max_wait}" ]]; then
            printf '  ✗ %s 在 %d 秒内未进入 running 状态，容器日志：\n' "${name}" "${max_wait}" >&2
            local cid
            cid="$("${DOCKER}" ps -a --filter "name=${name}" --format '{{.ID}}' 2>/dev/null | head -1)"
            if [[ -n "${cid}" ]]; then
                "${DOCKER}" logs --tail 80 "${cid}" 2>&1 || true
            fi
            return 1
        fi
        sleep 2
    done
}

host_deploy() {
    [[ "${EUID}" -eq 0 ]] || fail '宿主机部署需要 root，请使用 sudo'
    require java
    require keytool
    require psql
    require systemctl
    require sha256sum
    require useradd
    require id
    require chown
    validate_release
    ensure_env_file
    parse_env
    write_db_property host-based
    ensure_jks host-based
    local java_bin unit_content
    java_bin="$(command -v java)"
    if ! id -u dc-ems >/dev/null 2>&1; then
        useradd --system --user-group --home-dir "${INSTALL_ROOT}" --no-create-home --shell /usr/sbin/nologin dc-ems
    fi
    install -d -m 0755 "${INSTALL_ROOT}"
    install -d -m 0755 "${INSTALL_ROOT}/html"
    install -d -m 0755 "${INSTALL_ROOT}/files" "${INSTALL_ROOT}/logs" "${INSTALL_ROOT}/cache" "${INSTALL_ROOT}/models"
    cp -- "${ROOT}/app/server/dc-ems.jar" "${INSTALL_ROOT}/dc-ems.jar"
    cp -R -- "${ROOT}/app/web/." "${INSTALL_ROOT}/html/"
    cp -- "${ROOT}/app/server/data.json" "${INSTALL_ROOT}/data.json"
    cp -- "${ROOT}/config/log4j.properties" "${INSTALL_ROOT}/log4j.properties"
    cp -- "${ROOT}/config/db.property" "${INSTALL_ROOT}/db.property"
    install -d -m 0700 "${INSTALL_ROOT}/ssl"
    cp -- "${ROOT}/config/ssl/dc-ems.jks" "${INSTALL_ROOT}/ssl/dc-ems.jks"
    chown -R dc-ems:dc-ems "${INSTALL_ROOT}"
    chmod 600 "${INSTALL_ROOT}/db.property" "${INSTALL_ROOT}/ssl/dc-ems.jks"
    unit_content="$(sed -e "s|^WorkingDirectory=.*$|WorkingDirectory=${INSTALL_ROOT}|" \
        -e "s|^ExecStart=/usr/bin/java -jar dc-ems.jar$|ExecStart=${java_bin} -jar dc-ems.jar|" \
        "${ROOT}/runtime/host-based/dc-ems.service")"
    printf '%s\n' "${unit_content}" > "${SYSTEMD_UNIT}"
    systemctl daemon-reload
    systemctl enable dc-ems >/dev/null
    systemctl restart dc-ems >/dev/null
    systemctl is-active --quiet dc-ems || fail 'dc-ems 服务启动失败'
    printf 'mode=host-based\n' > "${ROOT}/.deploy-mode"
}

usage() {
    printf '用法：./deploy.sh -c|-h\n' >&2
    printf '      ./deploy.sh containerized|host-based\n' >&2
    printf '      ./deploy.sh --mode containerized|host-based  (兼容旧写法)\n' >&2
    exit 1
}

main() {
    local mode=""
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            -c)
                [[ -z "${mode}" ]] || usage
                mode="containerized"
                shift
                ;;
            -h)
                [[ -z "${mode}" ]] || usage
                mode="host-based"
                shift
                ;;
            --mode|-m)
                [[ "$#" -ge 2 ]] || usage
                [[ -z "${mode}" ]] || usage
                mode="$2"
                shift 2
                ;;
            containerized|host-based)
                [[ -z "${mode}" ]] || usage
                mode="$1"
                shift
                ;;
            *)
                usage
                ;;
        esac
    done
    [[ -n "${mode}" ]] || usage
    case "${mode}" in
        containerized|host-based)
            ;;
        *)
            usage
            ;;
    esac

    if [[ "${mode}" == "containerized" ]]; then
        container_deploy
    else
        host_deploy
    fi
}

main "$@"