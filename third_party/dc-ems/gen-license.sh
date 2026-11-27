#!/usr/bin/env bash
# 管理员工具：根据机器码生成授权码。
# 此脚本不在发布包内，仅供管理员在开发/管理机上使用。
#
# 用法：
#   ./gen-license.sh <机器码> [有效年限，默认20]
#
# 示例：
#   # 从 deploy.sh 输出中复制机器码，生成 20 年授权码：
#   ./gen-license.sh "MMEewvCHoKzm/XY0c8fQ7QAiaAmIvHNru92sAFsOsfKj4Cpuvit52X8SgjZB1IdFvyv6b38sLWBI83mIflGx1HSxYT+2XNq96EJ8+so2PFQ/JnfdPbSu2TizvmYVs7al1EiTDPW5Erc7CxBLycIGVGG0mmIJZmBrETgAmWTqLQo="
#
#   # 生成 5 年授权码：
#   ./gen-license.sh "MMEewvCHoKzm..." 5
#
# 前置条件：
#   - Java 运行环境（java 命令可用）
#   - liscense-1.0.jar（优先脚本同目录，其次 server/liscense/target/）
set -e
set -o pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR

JAR=""
# 优先从脚本同目录查找，其次从 Maven target 目录查找
if [[ -f "${SCRIPT_DIR}/liscense-1.0.jar" ]]; then
    JAR="${SCRIPT_DIR}/liscense-1.0.jar"
elif [[ -f "${SCRIPT_DIR}/../../server/liscense/target/liscense-1.0.jar" ]]; then
    JAR="${SCRIPT_DIR}/../../server/liscense/target/liscense-1.0.jar"
fi

if [[ -z "${JAR}" ]]; then
    printf '错误：找不到 liscense-1.0.jar\n\n' >&2
    printf '请执行以下任一操作后重试：\n' >&2
    printf '  1. 将 liscense-1.0.jar 放在此脚本同目录\n' >&2
    printf '  2. 在仓库根执行: cd server/liscense && mvn package -DskipTests\n' >&2
    exit 1
fi

usage() {
    printf '用法：%s <机器码> [有效年限，默认20]\n' "$(basename "$0")" >&2
    printf '示例：%s "MMEewvCHoKzm/XY0c8fQ7Q..."\n' "$(basename "$0")" >&2
    printf '      %s "MMEewvCHoKzm/XY0c8fQ7Q..." 5\n' "$(basename "$0")" >&2
    exit 1
}

[[ "$#" -ge 1 ]] || usage

MACHINE_CODE="$1"
YEARS="${2:-20}"

command -v java >/dev/null 2>&1 || { printf '错误：需要 Java 运行环境\n' >&2; exit 1; }

java -jar "${JAR}" "${MACHINE_CODE}" "${YEARS}"