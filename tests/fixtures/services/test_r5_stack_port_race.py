"""R5 Docker 栈宿主端口竞态整改的针对性验证。

覆盖：五端口互异、非端口错误立即失败、端口冲突换新栈重试（≤3 次）、
每次失败清理本 project、成功栈不被误清理、不触碰其他项目、真实并发启动。
"""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path

import pytest

import tests.fixtures.services.r5_stack as rs

pytestmark = [pytest.mark.integration]


class _ComposeScript:
    """按序回放 docker compose up 的结果，并记录调用。"""

    def __init__(self, results: list[tuple[int, str]]) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, project: str, env: dict, *args: str, timeout: float = 240.0):
        self.calls.append({"project": project, "env": dict(env), "args": list(args)})
        import subprocess

        if "down" in args:
            # 清理动作本身默认成功；失败回放只针对 up。
            rc, err = 0, ""
        else:
            rc, err = self.results.pop(0) if self.results else (0, "")
        return subprocess.CompletedProcess(
            args=["docker", "compose"], returncode=rc, stdout="", stderr=err
        )


@pytest.fixture()
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs, "docker_available", lambda: True)
    monkeypatch.setattr(rs, "_wait_tcp", lambda *a, **k: None)
    monkeypatch.setattr(rs, "_wait_tdengine_ready", lambda stack: None)
    monkeypatch.setattr(rs.R5Stack, "tdengine_sql", lambda self, *a, **k: {"code": 0})


def _port_sequence(monkeypatch: pytest.MonkeyPatch, values: list[int]) -> None:
    iterator = iter(values)
    monkeypatch.setattr(rs, "free_port", lambda: next(iterator))


def test_five_ports_are_unique(isolated: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """一次分配的五个端口互不相同（即使 free_port 返回重复候选）。"""
    script = _ComposeScript([(0, "")])
    monkeypatch.setattr(rs, "_compose", script)
    # 故意注入重复候选，验证去重循环
    _port_sequence(monkeypatch, [7000, 7000, 7000, 7001, 7001, 7002, 7003, 7004, 7005, 7006])
    stack = rs.up_stack()
    env = script.calls[0]["env"]
    ports = [
        env["R5_NATS_PORT"],
        env["R5_NATS_MON_PORT"],
        env["R5_REDIS_PORT"],
        env["R5_TDENGINE_PORT"],
        env["R5_KAFKA_PORT"],
    ]
    assert len(ports) == len(set(ports)) == 5
    assert stack.nats.endswith(ports[0])
    rs.down_project(stack.project, {"R5_PROJECT": stack.project})


def test_non_port_error_fails_immediately(isolated: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """非端口类错误不重试：一次 compose、一次清理、直接抛出。"""
    script = _ComposeScript([(1, "cannot connect to the Docker daemon")])
    monkeypatch.setattr(rs, "_compose", script)
    _port_sequence(monkeypatch, list(range(7100, 7110)))
    with pytest.raises(rs.R5StackError, match="Docker daemon"):
        rs.up_stack()
    up_calls = [c for c in script.calls if "up" in c["args"]]
    down_calls = [c for c in script.calls if "down" in c["args"]]
    assert len(up_calls) == 1
    assert len(down_calls) == 1
    assert down_calls[0]["project"] == up_calls[0]["project"]


def test_port_conflict_retries_with_fresh_project(
    isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端口占用触发独立新栈重试；失败 project 逐个清理；成功栈保留。"""
    script = _ComposeScript(
        [
            (1, "Bind for 127.0.0.1:7101 failed: port is already allocated"),
            (1, "Bind for 127.0.0.1:7102 failed: port is already allocated"),
            (0, ""),
        ]
    )
    monkeypatch.setattr(rs, "_compose", script)
    _port_sequence(monkeypatch, list(range(7200, 7220)))
    stack = rs.up_stack()
    up_calls = [c for c in script.calls if "up" in c["args"]]
    down_calls = [c for c in script.calls if "down" in c["args"]]
    assert len(up_calls) == 3
    projects = [c["project"] for c in up_calls]
    assert len(set(projects)) == 3, "每次重试必须是独立 project"
    # 失败的两个 project 各清理一次；成功 project 不清理
    assert [c["project"] for c in down_calls] == projects[:2]
    assert stack.project == projects[2]
    rs.down_project(stack.project, {"R5_PROJECT": stack.project})


def test_port_conflict_caps_at_three_attempts(
    isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端口冲突最多三次尝试，不得无限循环。"""
    conflict = (1, "port is already allocated")
    script = _ComposeScript([conflict, conflict, conflict, conflict, conflict])
    monkeypatch.setattr(rs, "_compose", script)
    _port_sequence(monkeypatch, list(range(7300, 7340)))
    with pytest.raises(rs.R5StackError, match="already allocated"):
        rs.up_stack()
    up_calls = [c for c in script.calls if "up" in c["args"]]
    down_calls = [c for c in script.calls if "down" in c["args"]]
    assert len(up_calls) == 3
    assert len(down_calls) == 3


def test_no_global_docker_prune_in_module() -> None:
    """模块源码不得出现全局 prune（只允许 project 级 down）。"""
    text = Path(rs.__file__).read_text(encoding="utf-8")
    assert "system prune" not in text
    assert "docker system" not in text


def test_real_concurrent_stacks_do_not_cross_clean() -> None:
    """真实 Docker：两个并发栈端口/project 互异，互不清理对方容器。"""
    if not rs.docker_available():
        pytest.skip("docker unavailable")
    results: list[rs.R5Stack | BaseException] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(rs.up_stack())
        except BaseException as exc:  # noqa: BLE001 - 汇总并发结果
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=300)
    try:
        assert not errors, errors
        assert len(results) == 2
        first, second = results
        assert first.project != second.project
        assert first.nats != second.nats
        assert first.kafka != second.kafka
        # 双方容器同时存在（互不清理）
        for stack in (first, second):
            probe = stack.docker(
                "ps", "--filter", f"label=com.docker.compose.project={stack.project}"
            )
            assert probe.returncode == 0 and "-1" in probe.stdout, probe.stdout
            assert f"{stack.project}-nats-1" in probe.stdout
        # 仅清理各自 project
        for stack in (first, second):
            rs.down_stack(stack)
            probe = stack.docker(
                "ps", "-a", "--filter", f"label=com.docker.compose.project={stack.project}"
            )
            assert f"{stack.project}-nats-1" not in probe.stdout
    finally:
        for stack in results:
            if isinstance(stack, rs.R5Stack):
                with contextlib.suppress(Exception):
                    rs.down_stack(stack)
