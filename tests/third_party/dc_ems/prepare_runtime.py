"""派生隔离的 DCEMS Docker 配置，禁止复用原始数据库名称及配置卷。"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RELEASE = ROOT / "third_party" / "dc-ems"
DATABASE = "dcems_smoke"
ALLOWED = {
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "DB_HOST",
    "DB_PORT",
    "HTTP_PORT",
    "HTTPS_PORT",
    "SERVER_PORT",
    "RMI_PORT",
    "NODE_ROLE",
    "JKS_SAN",
    "PEER_HOST",
    "LICENSE_CODE",
}


def read_env(path: Path) -> dict[str, str]:
    """读取字面量 KEY=value，不执行代码、不插入 Bash 表达式。"""
    result: dict[str, str] = {}
    for source in path.read_text(encoding="utf-8").splitlines():
        line = source.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name = name.strip()
        if name.startswith("REDIS_") and sep:
            continue
        if not sep or name not in ALLOWED or name in result or not value:
            if name in {"PEER_HOST", "LICENSE_CODE"} and sep and name not in result:
                result[name] = value
                continue
            raise ValueError(f"invalid or unknown environment item: {name!r}")
        if "\n" in value or "\r" in value or "${" in value:
            raise ValueError("environment values must be literal")
        result[name] = value
    if not result.get("DB_USER") or not result.get("DB_PASSWORD"):
        raise ValueError("database credentials are required")
    return result


def prepare(source: Path, destination: Path) -> None:
    """创建独立数据库配置，不修改 third_party 和生产配置根。"""
    if destination.exists():
        raise FileExistsError(destination)
    environment = read_env(source / ".env")
    environment.update(
        DB_NAME=DATABASE,
        DB_HOST="dc-ems-db",
        DB_PORT="5432",
        HTTP_PORT="18080",
        HTTPS_PORT="18443",
        SERVER_PORT="443",
        RMI_PORT="19444",
        NODE_ROLE="primary",
        PEER_HOST="",
    )
    config = source / "config"
    jks = config / "ssl" / "dc-ems.jks"
    log = config / "log4j.properties"
    if not jks.is_file() or not log.is_file():
        raise FileNotFoundError("DCEMS release SSL and log configuration required")
    destination.mkdir(parents=True, mode=0o700)
    (destination / "config" / "ssl").mkdir(parents=True, mode=0o700)
    shutil.copyfile(jks, destination / "config" / "ssl" / "dc-ems.jks")
    shutil.copyfile(log, destination / "config" / "log4j.properties")
    con = (
        "jdbc:postgresql://dc-ems-db:5432/"
        + DATABASE
        + "?currentSchema=public&timezone=Asia/Shanghai"
    )
    dbfile = destination / "config" / "db.property"
    dbfile.write_text(
        f"con={con}\nuser={environment['DB_USER']}\n"
        f"passwd={environment['DB_PASSWORD']}\n"
        "driver=org.postgresql.Driver\n",
        encoding="utf-8",
    )
    envfile = destination / "dcems.env"
    envfile.write_text(
        "".join(f"{key}={value}\n" for key, value in environment.items()),
        encoding="utf-8",
    )
    machine_id = destination / "machine-id"
    machine_id.write_text(secrets.token_hex(16) + "\n", encoding="utf-8")
    machine_id.chmod(0o444)
    for path in [envfile, dbfile, destination / "config" / "ssl" / "dc-ems.jks"]:
        os.chmod(path, 0o600)


def main() -> None:
    """仅创建一次，避免在已有运行库上静默替换访问参数。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=RELEASE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.source.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
