"""独立进程 HTTPS/mTLS fencing 证明方及其受控模拟状态机。

该进程只证明 Commander 的外部权威集成逻辑。状态迁移不代表任何真实 PLC、
部署控制器或现场 I/O 已完成物理隔离；原始 attestation 不写入状态或日志。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import multiprocessing
import os
import socket
import ssl
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


class _State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.scenario_path = path.with_name("scenario.json")
        self.lock = threading.Lock()
        self.value: dict[str, Any] = {"devices": {}, "requests": []}
        _write_json(path, self.value)

    def scenario(self) -> dict[str, Any]:
        return json.loads(self.scenario_path.read_text(encoding="utf-8"))

    def decide(self, request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with self.lock:
            evidence = base64.b64decode(request["attestation"], validate=True)
            self.value["requests"].append(
                {
                    "device_id": request.get("device_id"),
                    "config_revision": request.get("config_revision"),
                    "caller_id": request.get("caller_id"),
                    "request_id": request.get("request_id"),
                    "new_instance": request.get("new_instance"),
                    "evidence_sha256": hashlib.sha256(evidence).hexdigest(),
                }
            )
            device_id = str(request["device_id"])
            previous = self.value["devices"].get(
                device_id,
                {"epoch": 0, "owner": "old-instance", "old_fenced": False, "tokens": []},
            )
            overrides = dict(self.scenario().get("overrides", {}))
            token_id = str(overrides.get("token_id", f"token-{request['request_id']}"))
            epoch = int(overrides.get("fencing_epoch", previous["epoch"] + 1))
            isolated = bool(overrides.get("isolated", True))
            enforced = bool(overrides.get("fencing_enforced", True))
            fenced = isolated and enforced
            current = {
                "epoch": epoch,
                "owner": overrides.get(
                    "owner", request["new_instance"] if fenced else previous["owner"]
                ),
                "old_fenced": fenced,
                "tokens": [*previous["tokens"], token_id],
            }
            self.value["devices"][device_id] = current
            _write_json(self.path, self.value)
            response = {
                "device_id": request["device_id"],
                "config_revision": request["config_revision"],
                "caller_id": request["caller_id"],
                "request_id": request["request_id"],
                "new_instance": request["new_instance"],
                "old_instance": "old-instance",
                "token_id": token_id,
                "fencing_epoch": epoch,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
                "isolated": isolated,
                "fencing_enforced": enforced,
                "revoked": False,
            }
            response.update(overrides)
            for key in self.scenario().get("omit", []):
                response.pop(key, None)
            return int(self.scenario().get("status", 200)), response


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: _State) -> None:
        self.state = state
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: object) -> None:
        return

    def _do_post(self) -> None:
        scenario = self.server.state.scenario()
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            self.send_error(413)
            return
        raw = self.rfile.read(length)
        try:
            request = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error(400)
            return
        delay = float(scenario.get("delay_seconds", 0))
        if delay:
            time.sleep(delay)
        mode = str(scenario.get("mode", "success"))
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "https://127.0.0.1:1/redirected")
            self.end_headers()
            return
        status, response = self.server.state.decide(request)
        if mode == "non-json":
            payload = b"not-json"
        else:
            if mode == "oversized":
                response["padding"] = "x" * 100_000
            payload = json.dumps(response).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def __getattr__(self, name: str) -> Any:
        if name == "do_POST":
            return self._do_post
        raise AttributeError(name)


def _serve(args: argparse.Namespace) -> None:
    scenario_path = Path(args.scenario)
    json.loads(scenario_path.read_text(encoding="utf-8"))
    state = _State(Path(args.state))
    server = _Server(("127.0.0.1", args.port), state)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.server_cert, args.server_key)
    context.load_verify_locations(args.ca_cert)
    context.verify_mode = ssl.CERT_REQUIRED
    server.socket = context.wrap_socket(server.socket, server_side=True)
    _write_json(Path(args.ready), {"port": server.server_address[1]})
    server.serve_forever()


class FencingAuthorityProcess:
    """管理真实独立 OS 进程；scenario 在进程启动时固定且可审计。"""

    def __init__(
        self,
        root: Path,
        *,
        ca_cert: Path,
        server_cert: Path,
        server_key: Path,
        scenario: dict[str, Any] | None = None,
    ) -> None:
        self.root = root / f"authority-{uuid4().hex[:10]}"
        self.root.mkdir(parents=True)
        self.state_path = self.root / "state.json"
        self.scenario_path = self.root / "scenario.json"
        _write_json(self.scenario_path, scenario or {})
        self.port = _free_port()
        self.url = f"https://127.0.0.1:{self.port}/verify-and-fence"
        ready = self.root / "ready.json"
        args = argparse.Namespace(
            port=self.port,
            ca_cert=ca_cert,
            server_cert=server_cert,
            server_key=server_key,
            scenario=str(self.scenario_path),
            state=str(self.state_path),
            ready=str(ready),
        )
        self.process = multiprocessing.get_context("spawn").Process(
            target=_serve,
            args=(args,),
            name=f"fencing-authority-{self.port}",
        )
        self.process.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if ready.is_file():
                return
            if self.process.exitcode is not None:
                raise RuntimeError("fencing authority process exited during startup")
            time.sleep(0.01)
        self.close()
        raise RuntimeError("fencing authority process did not become ready")

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def configure(self, scenario: dict[str, Any]) -> None:
        _write_json(self.scenario_path, scenario)

    def close(self) -> None:
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=5)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=5)
        self.process.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--ca-cert", type=Path, required=True)
    parser.add_argument("--server-cert", type=Path, required=True)
    parser.add_argument("--server-key", type=Path, required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--ready", required=True)
    _serve(parser.parse_args())


if __name__ == "__main__":
    main()
