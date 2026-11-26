"""严格 CLI 独立进程 E2E：独立鉴权、双采集与四路 Sink 输出。"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import nats
import pytest
import yaml

from tests.cli.e2e_util import last_json, run_cli
from tests.commander.grpc_harness import build_pki
from tests.data_generator.conftest import _real_tree, simulation_document, write_service_yaml
from tests.fixtures.servers.modbus_server import ModbusMockServer, _holding_registers
from tests.fixtures.services import r5_stack
from tests.sink_service._stage1_fixtures import DELIVERIES_INSTANCE, SINKS_INSTANCE
from tests.sink_service._stage1_fixtures import SERVICE_FILE as SINK_SERVICE_FILE
from tests.sink_service.system.conftest import SinkSystemHarness
from tests.support.collector_config_tree import SERVICE_NAME, write_collector_modbus_tree
from tests.support.commander_mtls import write_commander_service
from tests.support.gateway import ServerSpec, reserve_port, write_config_tree, write_mtls_pki

pytestmark = [
    pytest.mark.system,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.slow,
    pytest.mark.modbus,
    pytest.mark.redis,
    pytest.mark.kafka,
]

SRC = Path(__file__).resolve().parents[2] / "src"
#: N-CLI-1：Collector 管理/读/诊断 RPC 的管理员令牌；服务进程与 CLI 进程须一致。
ADMIN_TOKEN = "strict-cli-admin-token"
SERVICES = (
    "gateway",
    "data-generator",
    "collector_realtime",
    "collector_reliable",
    "sink",
    "commander",
)


def _dump(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _write_cli(
    path: Path,
    *,
    collector: str,
    endpoints: dict[str, str],
    processes: dict[str, dict[str, Any]],
    collector_pki: dict[str, object],
) -> Path:
    clients = cast(dict[str, tuple[Path, Path]], collector_pki["clients"])
    collector_cert, collector_key = clients["cli"]
    profiles = {
        "collector": {
            "ca_cert": str(collector_pki["ca"]),
            "client_cert": str(collector_cert),
            "client_key": str(collector_key),
        },
    }
    token_envs = {
        "gateway": "WIND_HUB_TEST_GATEWAY_CLI_TOKEN",
        "data-generator": "WIND_HUB_TEST_DG_CLI_TOKEN",
        "commander": "WIND_HUB_TEST_COMMANDER_OPS_TOKEN",
        "sink": "WIND_HUB_SINK_CLI_TOKEN",
    }
    services: dict[str, dict[str, Any]] = {}
    for name in SERVICES:
        row: dict[str, Any] = {
            "grpc_endpoint": endpoints[name],
            "manager": "process",
            "target": name,
            "process": processes[name],
        }
        if name.startswith("collector_"):
            row["tls_profile"] = "collector"
        else:
            row["token_env"] = token_envs[name]
        services[name] = row
    services["collector"] = {
        "grpc_endpoint": endpoints[collector],
        "tls_profile": "collector",
    }
    _dump(
        path,
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "cli": {
                "timeout_seconds": 10,
                "output_format": "json",
                "prompt": "wind-hub> ",
                "poll_interval_ms": 200,
            },
            "services": services,
            "tls_profiles": profiles,
        },
    )
    path.chmod(0o600)
    return path


def _scalar_number(raw: object) -> float | None:
    """CLI JSON 输出中的 ScalarValue 以 kind 字典渲染；解包数值。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, dict):
        for key in ("double_value", "float_value", "int_value", "uint_value"):
            if isinstance(raw.get(key), int | float) and not isinstance(raw[key], bool):
                return float(raw[key])
    return None


class StrictCliE2E:
    """Run every business process through the production CLI."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.log = root / "cli-calls.jsonl"
        self.processes: dict[str, dict[str, Any]] = {}
        self.cli_realtime = root / "cli-realtime.yaml"
        self.cli_reliable = root / "cli-reliable.yaml"
        self.sink_token: str | None = None
        self.auth_tokens: dict[str, str] = {}

    def register_process(
        self, name: str, argv: list[str], *, cwd: Path, env: dict[str, str] | None = None
    ) -> None:
        self.processes[name] = {
            "argv": argv,
            "cwd": str(cwd),
            "env": {"PYTHONPATH": str(SRC), **(env or {})},
            "state_dir": str(self.root / "process-state"),
            "log_file": str(self.root / f"{name}.log"),
        }

    def cli_env(self, *, token: str | None = ADMIN_TOKEN) -> dict[str, str]:
        """CLI 子进程环境：默认注入管理令牌；None 表示删除（fail-closed 路径）。"""
        environment = {**os.environ, **self.auth_tokens}
        if token is None:
            environment.pop("WIND_HUB_COLLECTOR_ADMIN_TOKEN", None)
        else:
            environment["WIND_HUB_COLLECTOR_ADMIN_TOKEN"] = token
        if self.sink_token is not None:
            environment["WIND_HUB_SINK_CLI_TOKEN"] = self.sink_token
        return environment

    async def cli(
        self,
        config: Path,
        *args: str,
        timeout: float = 45,
        check: bool = True,
        token: str | None = ADMIN_TOKEN,
    ) -> dict[str, Any]:
        def invoke() -> subprocess.CompletedProcess[str]:
            return run_cli(config, *args, timeout=timeout, env=self.cli_env(token=token))

        result = await asyncio.to_thread(invoke)
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "time": time.time(),
                        "config": str(config),
                        "args": args,
                        "returncode": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        if check and result.returncode != 0:
            raise AssertionError(
                f"CLI {' '.join(args)} rc={result.returncode}\n"
                f"stdout={result.stdout}\nstderr={result.stderr}\nlogs={self.root}"
            )
        return last_json(result.stdout) if "{" in result.stdout else {}

    async def poll_cli(
        self,
        config: Path,
        *args: str,
        predicate: Callable[[Any], bool],
        timeout: float = 60,
    ) -> Any:
        deadline = time.monotonic() + timeout
        latest: dict[str, Any] = {}
        while time.monotonic() < deadline:
            latest = await self.cli(config, *args, check=False)
            data = latest.get("data")
            if (
                latest.get("status") == "SUCCESS"
                and isinstance(data, dict | list)
                and predicate(data)
            ):
                return data
            await asyncio.sleep(0.4)
        raise AssertionError(
            f"CLI polling timeout: {' '.join(args)}; last={latest}; logs={self.root}"
        )

    async def service(self, config: Path, operation: str, name: str) -> dict[str, Any]:
        result = await self.cli(config, "service", operation, name, "--yes", check=False)
        if operation == "stop":
            if result.get("status") != "SUCCESS":
                raise AssertionError(f"service stop {name} failed: {result}")
            return result
        data = result.get("data")
        process = data.get("process", {}) if isinstance(data, dict) else {}
        if not process.get("confirmed", False):
            raise AssertionError(f"service {operation} {name} not confirmed: {result}")
        await self.poll_cli(
            config,
            "service",
            "status",
            name,
            predicate=lambda _data: True,
            timeout=75,
        )
        return result

    async def stop_all(self, config: Path) -> None:
        for name in reversed(SERVICES):
            if name in self.processes:
                await self.cli(config, "service", "stop", name, "--yes", timeout=20, check=False)


async def _provision_commander_audit(url: str, stream: str, subject: str) -> None:
    from nats.js.api import RetentionPolicy, StorageType, StreamConfig

    client = await nats.connect(servers=url, connect_timeout=10)
    try:
        await client.jetstream().add_stream(
            StreamConfig(
                name=stream,
                subjects=[subject],
                storage=StorageType.FILE,
                retention=RetentionPolicy.LIMITS,
                max_age=3600,
            )
        )
    finally:
        await client.drain()


async def _wait_new_redis(
    sink: SinkSystemHarness, key: str, previous_id: str, timeout: float = 60
) -> dict[str, Any]:
    return await sink.wait_redis_value(
        key, timeout=timeout, match=lambda value: value.get("sample_id") != previous_id
    )


async def _read_modbus(port: int, address: int, count: int) -> list[int]:
    """通过独立客户端连接读取被测 Modbus Server，不读取 Sink 内部状态。"""
    from pymodbus.client import AsyncModbusTcpClient

    client = AsyncModbusTcpClient("127.0.0.1", port=port)
    await client.connect()
    try:
        response = await client.read_holding_registers(address, count=count, device_id=1)
        if response.isError():
            raise RuntimeError(f"Modbus read failed: {response}")
        return list(response.registers)
    finally:
        client.close()


async def _wait_sink_modbus(port: int, value: float) -> None:
    deadline = time.monotonic() + 60
    latest: Exception | None = None
    while time.monotonic() < deadline:
        try:
            registers = await _read_modbus(port, 1000, 2)
            decoded = struct.unpack(">f", struct.pack(">HH", *registers))[0]
            quality = (await _read_modbus(port, 2000, 1))[0]
            if decoded == pytest.approx(value) and quality == 0:
                return
        except (OSError, RuntimeError) as error:
            latest = error
        await asyncio.sleep(0.4)
    raise AssertionError(f"Sink Modbus output not observed: port={port}, error={latest}")


@pytest.mark.asyncio
async def test_strict_cli_five_service_data_command_restart_e2e(tmp_path: Path) -> None:
    """DG 起源双采集链与独立 Commander 写链均只用真实 CLI 操作。"""
    token = uuid4().hex[:8]
    farm = "farm-a"
    rt_collector = f"collector-rt-{token}"
    reliable_collector = f"collector-hist-{token}"
    device = "modbus-1"
    point = "rotor_speed"
    scenario = f"strict-cli-{token}"
    value = 42.25
    stack: r5_stack.R5Stack | None = None
    # 采集链源头寄存器预置本用例期望值：rotor_speed=42.25（modbus-1）。
    modbus = ModbusMockServer(port=reserve_port(), holding=_holding_registers(value, 800.0))
    harness = StrictCliE2E(tmp_path)
    sink: SinkSystemHarness | None = None
    generation_id: str | None = None

    try:
        stack = await asyncio.to_thread(r5_stack.up_stack)
        await modbus.start()

        ports = {name: reserve_port() for name in SERVICES}
        endpoints = {name: f"127.0.0.1:{port}" for name, port in ports.items()}

        gateway_root = tmp_path / "gateway"
        pki = write_mtls_pki(gateway_root, ("cli", "generator", "sink"))
        # D01 的上游是本用例的真实 ModbusMockServer；Gateway 自身的下游
        # Modbus 侦听必须独立端口，否则与设备端口冲突（启动 fail-closed）。
        gateway_listen_port = reserve_port()
        gateway_service = write_config_tree(
            gateway_root,
            [
                ServerSpec(
                    server_id="gw-strict",
                    device_id="D01",
                    modbus_port=modbus.port,
                    listen_port=gateway_listen_port,
                    control_enabled=False,
                )
            ],
            commander_endpoint="127.0.0.1:1",
            grpc_port=ports["gateway"],
            permissions={
                "cli": ["read", "lifecycle", "inject", "diagnose"],
                "generator": ["read", "inject"],
            },
            point_table={
                "fb_float": {
                    "data_type": "float32",
                    "address": {
                        "register_type": "holding",
                        "address": 100,
                        "word_order": "big_endian",
                    },
                },
                "sink_value": {
                    "data_type": "float32",
                    "address": {
                        "register_type": "holding",
                        "address": 102,
                        "word_order": "big_endian",
                    },
                },
            },
            mtls_enabled=False,
        )

        dg_root = tmp_path / "dg-config"
        _real_tree(dg_root)
        _dump(
            dg_root / "instances" / "device_farm-a_v1.6.0.yaml",
            {
                "schema_version": 1,
                "file_version": "1.6.0",
                "devices": {
                    "D01": {
                        "enabled": True,
                        "model": {"id": "wtg_tz2mw", "version": "1.4.0"},
                        "endpoint": {"host": "127.0.0.1", "port": modbus.port},
                        "overrides": {},
                    }
                },
            },
        )
        point_table_path = (
            dg_root / "templates" / "point_tables" / "point_table_modbus_wtg_tz2mw_v1.2.0.yaml"
        )
        point_table_doc = yaml.safe_load(point_table_path.read_text(encoding="utf-8"))
        point_table_doc["points"]["generator_speed"]["data_type"] = "float32"
        _dump(point_table_path, point_table_doc)
        business_path = dg_root / "templates" / "business_points_standard_v1.5.0.yaml"
        business_doc = yaml.safe_load(business_path.read_text(encoding="utf-8"))
        business_doc["business_points"]["tz2mw_generator_speed_raw"]["data_type"] = "float32"
        _dump(business_path, business_doc)
        scenario_doc = simulation_document(
            {"gw-strict": endpoints["gateway"]},
            [
                (
                    "D01",
                    "generator_speed",
                    {
                        "kind": "random",
                        "distribution": "uniform",
                        "minimum": value,
                        "maximum": value,
                    },
                    [("gw-strict", "fb_float")],
                )
            ],
            scenario_id=scenario,
            interval=0.2,
        )
        dg_service = write_service_yaml(
            dg_root,
            bind=endpoints["data-generator"],
            scenarios=scenario_doc["scenarios"],
        )
        dg_doc = yaml.safe_load(dg_service.read_text(encoding="utf-8"))
        # Service YAML already carries the current Bearer security contract.
        # Generator output uses WIND_HUB_TEST_DG_GATEWAY_TOKEN.
        assert dg_doc["output"]["gateway_token_env"] == "WIND_HUB_TEST_DG_GATEWAY_TOKEN"
        _dump(dg_service, dg_doc)

        collector_roots: dict[str, Path] = {}
        for name, delivery in (
            ("collector_realtime", "realtime"),
            ("collector_reliable", "reliable"),
        ):
            root = write_collector_modbus_tree(
                tmp_path / name,
                modbus.port,
                device_id=device,
                site_id=farm,
                nats_url=stack.nats,
            )
            protocol_source = root / "templates" / "protocols" / "protocol_modbus_tcp_v1.1.0.yaml"
            protocol_doc = yaml.safe_load(protocol_source.read_text(encoding="utf-8"))
            protocol_doc["file_version"] = "1.0.0"
            _dump(protocol_source.with_name("protocol_modbus_tcp_v1.0.0.yaml"), protocol_doc)
            service_path = root / "services" / SERVICE_NAME
            document = yaml.safe_load(service_path.read_text(encoding="utf-8"))
            document["messaging"]["delivery"] = delivery
            if delivery == "reliable":
                document["messaging"].pop("max_staleness_ms", None)
            _dump(service_path, document)
            collector_roots[name] = root

        (tmp_path / "commander-pki").mkdir(parents=True)
        commander_pki = build_pki(tmp_path / "commander-pki")
        commander_tree = write_collector_modbus_tree(
            tmp_path / "commander" / "config-tree",
            modbus.port,
            device_id="WT01",
            site_id=farm,
            nats_url=stack.nats,
        )
        commander_protocol = (
            commander_tree / "templates" / "protocols" / "protocol_modbus_tcp_v1.1.0.yaml"
        )
        commander_protocol_doc = yaml.safe_load(commander_protocol.read_text(encoding="utf-8"))
        commander_protocol_doc["file_version"] = "1.0.0"
        _dump(
            commander_protocol.with_name("protocol_modbus_tcp_v1.0.0.yaml"),
            commander_protocol_doc,
        )
        commander_service = write_commander_service(
            tmp_path / "commander",
            commander_tree,
            commander_pki,
            grpc_port=ports["commander"],
        )
        commander_doc = yaml.safe_load(commander_service.read_text(encoding="utf-8"))
        audit_stream = f"CMD_STRICT_{token.upper()}"
        audit_subject = f"commander.strict.{token}"
        commander_doc["audit"]["nats_servers"] = [stack.nats]
        commander_doc["audit"]["stream"] = audit_stream
        commander_doc["audit"]["subject"] = audit_subject
        _dump(commander_service, commander_doc)
        await _provision_commander_audit(stack.nats, audit_stream, audit_subject)

        gateway_sink_port = reserve_port()
        gateway_sink_token = os.urandom(32).hex()
        gateway_service_doc = yaml.safe_load(gateway_service.read_text(encoding="utf-8"))
        gateway_service_doc["grpc"]["sink_listener_port"] = gateway_sink_port
        gateway_service_doc["security"]["callers"].append(
            {
                "caller_id": "sink",
                "token_env": "WIND_HUB_GATEWAY_SINK_TOKEN",
                "permissions": ["inject"],
            }
        )
        _dump(gateway_service, gateway_service_doc)
        sink = SinkSystemHarness(
            root=tmp_path / "sink",
            stack=stack,
            grpc_port=ports["sink"],
            namespace=f"strict-cli-{token}",
            kafka_topic=f"strict-cli-{token}",
            history_stream=f"WH_HISTORY_STRICT_{token.upper()}",
            dlq_stream=f"WH_DLQ_STRICT_{token.upper()}",
            dlq_subject=f"wh.strict.dlq.{token}",
            token=token,
        )
        sink.write_dual_channel_root()
        harness.sink_token = sink.auth_tokens["admin"]
        deliveries_path = sink.root / DELIVERIES_INSTANCE
        deliveries_doc = yaml.safe_load(deliveries_path.read_text(encoding="utf-8"))
        deliveries_doc["deliveries"]["ems_power"]["source"]["collector_id"] = rt_collector
        deliveries_doc["deliveries"]["power_history"]["source"]["collector_id"] = reliable_collector
        bounded_source = {
            "wind_farm_id": farm,
            "collector_id": rt_collector,
            "devices": [device],
            "points": [point],
        }
        deliveries_doc["deliveries"]["gateway_output"] = {
            "template_id": "realtime_power",
            "template_version": "1.0.0",
            "sink_id": "gateway_sink",
            "desired_state": "START",
            "source": bounded_source,
            "target_mappings": [
                {
                    "source_device_id": device,
                    "source_point_id": point,
                    "variable_id": "sink_value",
                }
            ],
            "overrides": {},
        }
        deliveries_doc["deliveries"]["modbus_output"] = {
            "template_id": "realtime_power",
            "template_version": "1.0.0",
            "sink_id": "modbus_sink",
            "desired_state": "START",
            "source": bounded_source,
            "overrides": {},
        }
        _dump(deliveries_path, deliveries_doc)

        sinks_path = sink.root / SINKS_INSTANCE
        sinks_doc = yaml.safe_load(sinks_path.read_text(encoding="utf-8"))
        sink_modbus_port = reserve_port()
        sinks_doc["sinks"]["gateway_sink"] = {
            "template_id": "gateway_realtime",
            "template_version": "1.1.0",
            "connection": {
                "endpoint": f"127.0.0.1:{gateway_sink_port}",
                "gateway_id": "gw-strict",
            },
            "overrides": {},
        }
        sinks_doc["sinks"]["modbus_sink"] = {
            "template_id": "modbus_registers",
            "template_version": "1.0.0",
            "connection": {"listen_host": "127.0.0.1", "port": sink_modbus_port},
            "overrides": {},
        }
        _dump(sinks_path, sinks_doc)
        for template_name, version in (
            ("gateway_realtime", "1.1.0"),
            ("modbus_registers", "1.0.0"),
        ):
            relative = Path("configs/templates/sinks") / f"sink_{template_name}_v{version}.yaml"
            source = SRC.parent / relative
            target = sink.root / relative
            _dump(target, yaml.safe_load(source.read_text(encoding="utf-8")))
        await sink.provision_streams()

        python = sys.executable
        common_env = {
            "WIND_HUB_COLLECTOR_TLS_CERT": str(pki["server_crt"]),
            "WIND_HUB_COLLECTOR_TLS_KEY": str(pki["server_key"]),
            "WIND_HUB_COLLECTOR_TLS_CA": str(pki["ca"]),
            # N-CLI-1：Collector 受保护 RPC 的服务端令牌，与 CLI 子进程一致。
            "WIND_HUB_COLLECTOR_ADMIN_TOKEN": ADMIN_TOKEN,
        }
        gateway_cli_token = os.urandom(32).hex()
        generator_gateway_token = os.urandom(32).hex()
        gateway_sink_management_token = os.urandom(32).hex()
        dg_cli_token = os.urandom(32).hex()
        commander_ops_token = os.urandom(32).hex()
        harness.auth_tokens = {
            "WIND_HUB_TEST_GATEWAY_CLI_TOKEN": gateway_cli_token,
            "WIND_HUB_TEST_DG_CLI_TOKEN": dg_cli_token,
            "WIND_HUB_TEST_COMMANDER_OPS_TOKEN": commander_ops_token,
        }
        harness.register_process(
            "gateway",
            [
                python,
                "-m",
                "industrial_gateway.main",
                "--config",
                str(gateway_service),
                "--project-root",
                str(gateway_root),
            ],
            cwd=SRC,
            env={
                "WIND_HUB_GATEWAY_SINK_TOKEN": gateway_sink_token,
                "WIND_HUB_TEST_GATEWAY_COMMANDER_TARGET_TOKEN": os.urandom(32).hex(),
                "WIND_HUB_TEST_GATEWAY_CLI_TOKEN": gateway_cli_token,
                "WIND_HUB_TEST_GATEWAY_GENERATOR_TOKEN": generator_gateway_token,
                "WIND_HUB_TEST_GATEWAY_SINK_TOKEN": gateway_sink_management_token,
            },
        )
        harness.register_process(
            "data-generator",
            [python, "-m", "data_generator.main", "--config", str(dg_service)],
            cwd=SRC,
            env={
                "WIND_HUB_TEST_DG_GATEWAY_TOKEN": generator_gateway_token,
                "WIND_HUB_TEST_DG_CLI_TOKEN": dg_cli_token,
                "WIND_HUB_TEST_DG_CALLER_TOKEN": os.urandom(32).hex(),
            },
        )
        for name, collector_id in (
            ("collector_realtime", rt_collector),
            ("collector_reliable", reliable_collector),
        ):
            harness.register_process(
                name,
                [
                    python,
                    "-m",
                    "collector.main",
                    "--config",
                    str(collector_roots[name] / "services" / SERVICE_NAME),
                    "--state-directory",
                    str(tmp_path / name / "state"),
                    "--collector-id",
                    collector_id,
                    "--grpc-host",
                    "127.0.0.1",
                    "--grpc-port",
                    str(ports[name]),
                    "--shutdown-timeout",
                    "10",
                ],
                cwd=SRC,
                env=common_env,
            )
        harness.register_process(
            "sink",
            [
                python,
                "-m",
                "sink_service.main",
                "--config-root",
                str(sink.root),
                "--service-file",
                SINK_SERVICE_FILE,
            ],
            cwd=sink.root,
            env={
                "WIND_HUB_GATEWAY_SINK_TOKEN": gateway_sink_token,
                "WIND_HUB_SINK_CLI_TOKEN": sink.auth_tokens["admin"],
            },
        )
        harness.register_process(
            "commander",
            [python, "-m", "commander", "--config", str(commander_service)],
            cwd=SRC,
            env={"WIND_HUB_TEST_COMMANDER_OPS_TOKEN": commander_ops_token},
        )
        _write_cli(
            harness.cli_realtime,
            collector="collector_realtime",
            endpoints=endpoints,
            processes=harness.processes,
            collector_pki=pki,
        )
        _write_cli(
            harness.cli_reliable,
            collector="collector_reliable",
            endpoints=endpoints,
            processes=harness.processes,
            collector_pki=pki,
        )

        for name in SERVICES:
            await harness.service(harness.cli_realtime, "start", name)

        await harness.poll_cli(
            harness.cli_realtime,
            "gateway",
            "list",
            predicate=lambda data: any(
                item.get("gateway_id") == "gw-strict" for item in data.get("gateways", [])
            ),
        )
        for config in (harness.cli_realtime, harness.cli_reliable):
            tasks = await harness.poll_cli(
                config,
                "collector",
                "tasks",
                predicate=lambda data: any(
                    item.get("task_id") == "turbine_all" for item in data.get("tasks", [])
                ),
            )
            assert len(tasks["tasks"]) == 1
            # N-CLI-1：错误令牌与缺失令牌必须被拒绝，且令牌不出现在输出。
            wrong = await harness.cli(
                config,
                "collector",
                "start",
                "turbine_all",
                "--yes",
                check=False,
                token="wrong-token",
            )
            assert wrong["status"] == "FAILURE", wrong
            assert "denied" in (wrong.get("message") or "").lower(), wrong
            missing = await harness.cli(
                config, "collector", "start", "turbine_all", "--yes", check=False, token=None
            )
            assert missing["status"] == "FAILURE", missing
            assert "admin credentials" in (missing.get("message") or ""), missing
            assert ADMIN_TOKEN not in json.dumps(wrong) + json.dumps(missing)
            await harness.cli(config, "collector", "start", "turbine_all", "--yes")
        await harness.poll_cli(
            harness.cli_realtime,
            "sink",
            "deliveries",
            predicate=lambda data: bool(data.get("deliveries"))
            and all(item.get("actual_state") == "RUNNING" for item in data["deliveries"]),
            timeout=90,
        )

        started = await harness.cli(
            harness.cli_realtime,
            "data-generator",
            "start",
            scenario,
            "--scenario-version",
            "1.0.0",
            "--yes",
        )
        generation_id = started["data"]["generation_id"]
        assert started["data"]["accepted"] is True

        gateway_value = await harness.poll_cli(
            harness.cli_realtime,
            "gateway",
            "read",
            "gw-strict",
            "fb_float",
            predicate=lambda data: data.get("values")
            and _scalar_number(data["values"][0].get("value")) == value,
        )
        assert gateway_value["values"][0]["quality"] == "GOOD"
        for config in (harness.cli_realtime, harness.cli_reliable):
            latest = await harness.poll_cli(
                config,
                "collector",
                "latest",
                device,
                point,
                predicate=lambda data: any(
                    sample.get("point_id") == point and _scalar_number(sample.get("value")) == value
                    for sample in data.get("samples", [])
                ),
            )
            assert latest["device_id"] == device

        redis_key = ":".join((sink.namespace, farm, rt_collector, device, point))
        first_redis = await sink.wait_redis_value(redis_key, timeout=60)
        assert first_redis["value"] == pytest.approx(value)
        assert first_redis["wind_farm_id"] == farm
        assert first_redis["collector_id"] == rt_collector
        assert first_redis["device_id"] == device
        assert first_redis["point_id"] == point
        assert await sink.redis_ttl(redis_key) > 0

        records = await sink.wait_kafka_messages(min_messages=4, timeout=75)
        payloads = [json.loads(record.value.decode("utf-8")) for record in records]
        matching = [
            item
            for item in payloads
            if isinstance(item.get("points"), dict) and point in item["points"]
        ]
        assert len(matching) >= 1, payloads
        # power_history 是此 Reliable Collector 的唯一 Kafka 输出；v3 行契约
        # 不再携带逐点 sample_id 或冗余 collector_id。
        assert all(item["schema_version"] == 3 for item in matching)
        assert {item["wind_farm_id"] for item in matching} == {farm}
        assert {item["device_id"] for item in matching} == {device}
        assert {item["device_config_id"] for item in matching} == {"device_system_v1.0.0"}
        assert any(
            item["points"][point][0] == pytest.approx(value) and item["points"][point][1] == 1
            for item in matching
        )
        assert len({(item["device_config_id"], item["sample_time"]) for item in matching}) == len(
            matching
        )
        assert (await sink.stream_pending())["consumer_pending"] == 0

        await harness.poll_cli(
            harness.cli_realtime,
            "gateway",
            "read",
            "gw-strict",
            "sink_value",
            predicate=lambda data: data.get("values")
            and _scalar_number(data["values"][0].get("value")) == pytest.approx(value)
            and data["values"][0].get("quality") == "GOOD",
            timeout=60,
        )
        # Modbus TCP 交付路由：sink 自身的 Modbus 从站（EMS 可读），
        # 不是设备回写——UpdateVariables 只入网关变量存储，设备写入是
        # Commander 控制路径（本用例 control_enabled=False）。
        await _wait_sink_modbus(sink_modbus_port, value)

        registers = await modbus.read_holding(1, 100, 2)
        assert struct.unpack(">f", struct.pack(">HH", *registers))[0] == pytest.approx(value)

        command = await harness.cli(
            harness.cli_realtime, "commander", "write", "WT01", "setpoint_power", "12.5", "--yes"
        )
        command_id = command["data"]["command_id"]
        assert command["data"]["accepted"] is True
        result = await harness.poll_cli(
            harness.cli_realtime,
            "commander",
            "result",
            command_id,
            predicate=lambda data: data.get("state")
            in {"SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED"},
        )
        assert result["state"] == "SUCCEEDED"
        assert result["outcome"] == "CONFIRMED_SUCCESS"
        assert result["io_settled"] is True
        registers = await modbus.read_holding(1, 200, 2)
        assert struct.unpack(">f", struct.pack(">HH", *registers))[0] == pytest.approx(12.5)

        # DG 运行时 generation 不跨进程 restart 存活：restart 前用真实 CLI
        # 停止本用例的生成（停止语义须在注册表仍在时验证）。
        await harness.cli(harness.cli_realtime, "data-generator", "stop", generation_id, "--yes")
        generation_id = None

        # 六个独立身份都执行真实 CLI restart；恢复后要求采集产生新样本。
        for name in SERVICES:
            await harness.service(harness.cli_realtime, "restart", name)
        await _wait_new_redis(sink, redis_key, first_redis["sample_id"])
        later_records = await sink.wait_kafka_messages(min_messages=8, timeout=60)
        assert len(later_records) >= 8
        persisted = await harness.cli(harness.cli_realtime, "commander", "result", command_id)
        assert persisted["data"]["state"] == "SUCCEEDED"

        for config in (harness.cli_realtime, harness.cli_reliable):
            await harness.cli(config, "collector", "stop", "turbine_all", "--yes")
        for delivery in ("ems_power", "gateway_output", "modbus_output", "power_history"):
            current = await harness.cli(harness.cli_realtime, "sink", "deliveries")
            revision = next(
                item["revision"]
                for item in current["data"]["deliveries"]
                if item["delivery_id"] == delivery
            )
            await harness.cli(
                harness.cli_realtime,
                "sink",
                "stop",
                delivery,
                "--expected-revision",
                revision,
                "--yes",
            )
        for name in reversed(SERVICES):
            await harness.service(harness.cli_realtime, "stop", name)

        # N-CLI-1：令牌不得出现在任何 CLI 输出记录与服务日志中。
        audit_text = harness.log.read_text(encoding="utf-8")
        assert ADMIN_TOKEN not in audit_text, "admin token leaked into CLI call audit log"
        for log_file in harness.root.glob("*.log"):
            content = log_file.read_text(encoding="utf-8", errors="replace")
            assert ADMIN_TOKEN not in content, f"admin token leaked into service log {log_file}"
    finally:
        if generation_id is not None and harness.cli_realtime.exists():
            await harness.cli(
                harness.cli_realtime,
                "data-generator",
                "stop",
                generation_id,
                "--yes",
                timeout=15,
                check=False,
            )
        if harness.cli_realtime.exists():
            await harness.stop_all(harness.cli_realtime)
        await modbus.stop()
        if sink is not None:
            await sink.delete_streams()
        if stack is not None:
            await asyncio.to_thread(r5_stack.down_stack, stack)
