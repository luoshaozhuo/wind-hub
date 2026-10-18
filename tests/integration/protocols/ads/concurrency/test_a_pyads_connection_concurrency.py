"""A 组：pyads 3.6.0 / AdsLib 共享 Connection 的并发能力矩阵。

每个测试 cell 在**独立子进程**中运行真实 pyads/AdsLib（子进程自托管
受控 ADS 内存服务器，走真实 AMS/TCP 帧），父进程施加硬超时与退出码
检查：

- 无锁并发的段错误/挂死不会影响 pytest 进程；
- 无锁 cell 的 ADSError/崩溃是**被测现象**，作为证据落盘，测试不要求
  它必须成功（否则等于用假设充当结论）；
- ``locked`` / ``sequential`` cell 是硬不变量：零失败、零内容错配、
  零挂死、零崩溃——串行化必须可靠；
- 所有模式都硬断言"读取内容错配 = 0"：一旦出现响应错配，是比
  ADSError(-1) 更严重的真实数据串扰，必须直接失败。

结果 JSON 写入 ``$WIND_HUB_ADS_BASELINE_DIR``（默认
``artifacts/ads-concurrency-baseline/a_matrix``）供报告引用。

标记 ``slow``：每 cell 0.5-5s，不进入 Fast Gate，按集成测试显式运行：
``pytest tests/integration/protocols/ads/concurrency -m ads``。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.ads, pytest.mark.real_service, pytest.mark.slow]

_REPO_ROOT = Path(__file__).resolve().parents[5]
_RESULTS_DIR = Path(
    os.environ.get(
        "WIND_HUB_ADS_BASELINE_DIR",
        str(_REPO_ROOT / "artifacts" / "ads-concurrency-baseline" / "a_matrix"),
    )
)
_CHILD_TIMEOUT_S = 40.0

# (threads, iters, delay_ms) —— 延迟放大在途窗口，保证真实重叠。
_MATRIX: dict[str, tuple[int, int, int]] = {
    "A01": (8, 3, 100),
    "A02": (8, 3, 100),
    "A03": (8, 3, 100),
    "A04": (8, 3, 100),
    "A05": (8, 3, 100),
    "A06": (6, 2, 100),
    "A07": (6, 2, 100),
    # 回调在整个实验期间持续投递（10ms × 4 路），高频低延迟。
    "A08": (8, 30, 10),
    "A09": (8, 3, 100),
    # 每线程一条"独立" Connection（是否真正独立本身是被测问题）。
    "A10": (6, 8, 20),
}
_MODES = ("unlocked", "locked", "sequential")

# 代表性故障注入：(场景, 故障, 模式)。
_FAULT_CELLS = [
    ("A03", "timeout", "unlocked"),
    ("A03", "timeout", "locked"),
    ("A03", "reject", "unlocked"),
    ("A03", "reject", "locked"),
    ("A03", "disconnect", "unlocked"),
    ("A03", "disconnect", "locked"),
    ("A05", "timeout", "unlocked"),
    ("A05", "timeout", "locked"),
]


def _build_cells() -> list[tuple[str, dict[str, object]]]:
    cells: list[tuple[str, dict[str, object]]] = []
    for scenario, (threads, iters, delay) in _MATRIX.items():
        for mode in _MODES:
            cells.append(
                (
                    f"{scenario}-{mode}",
                    {
                        "scenario": scenario,
                        "mode": mode,
                        "threads": threads,
                        "iters": iters,
                        "delay_ms": delay,
                        "fault": "none",
                    },
                )
            )
    for scenario, fault, mode in _FAULT_CELLS:
        threads, iters, delay = _MATRIX[scenario]
        cells.append(
            (
                f"{scenario}-{mode}-{fault}",
                {
                    "scenario": scenario,
                    "mode": mode,
                    "threads": threads,
                    "iters": iters,
                    "delay_ms": 0 if fault == "timeout" else delay,
                    "fault": fault,
                    "timeout_ms": 800,
                },
            )
        )
    return cells


_CELLS = _build_cells()


def _cell_ip(cell_index: int) -> str:
    """每个 cell 独立 loopback 地址，避免 bind/连接复用互相干扰。"""
    return f"127.30.{cell_index // 250}.{1 + cell_index % 250}"


def _run_child(spec: dict[str, object], ip: str) -> dict[str, object]:
    spec = dict(spec, ip=ip)
    proc = subprocess.run(
        [sys.executable, "-m", "tests.support.ads_concurrency_child", json.dumps(spec)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_CHILD_TIMEOUT_S,
        check=False,
    )
    evidence: dict[str, object] = {
        "spec": spec,
        "returncode": proc.returncode,
    }
    stdout = proc.stdout.strip().splitlines()
    if stdout:
        try:
            evidence["result"] = json.loads(stdout[-1])
        except json.JSONDecodeError:
            evidence["stdout_tail"] = "\n".join(stdout[-5:])
    if proc.returncode not in {0, 4}:
        # 段错误（-11）等致命崩溃：子进程无 JSON，这本身是核心证据。
        evidence["crashed"] = True
        evidence["stderr_tail"] = proc.stderr[-2000:]
    return evidence


def _save(cell_id: str, evidence: dict[str, object]) -> None:
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    spec = evidence["spec"]
    assert isinstance(spec, dict)
    name = f"{cell_id}_{spec.get('fault', 'none')}.json"
    (_RESULTS_DIR / name).write_text(json.dumps(evidence, indent=1, ensure_ascii=False))


@pytest.mark.parametrize(
    "cell_id,spec",
    _CELLS,
    ids=[cell_id for cell_id, _ in _CELLS],
)
def test_pyads_connection_concurrency_cell(cell_id: str, spec: dict[str, object]) -> None:
    """单 cell：证据落盘 + 按模式施加硬不变量。"""
    index = next(i for i, (cid, _) in enumerate(_CELLS) if cid == cell_id)
    evidence = _run_child(spec, _cell_ip(index + 1))
    _save(cell_id, evidence)

    rc = int(evidence["returncode"])
    result = evidence.get("result")
    crashed = bool(evidence.get("crashed"))

    # 实验装配错误（参数/工具缺陷）必须直接失败，与并发安全性结论无关。
    assert rc != 2, f"child harness error: {evidence.get('stderr_tail', '')[:500]}"
    # 父级已设 40s 硬超时：若子进程报告挂死，只允许在 unlocked 探索中作为
    # 证据存在；串行模式挂死在下方判失败。
    assert rc in {0, 4, -11, -6} or result is not None, f"unexpected child exit {rc}: {evidence}"

    # 数据完整性是任何模式都不可越过的红线：错配即真实帧串扰。
    if result is not None:
        assert result.get("mismatch") == 0, f"response content mismatch: {result}"

    mode = str(spec["mode"])
    fault = str(spec.get("fault", "none"))
    scenario = str(spec["scenario"])
    # A10 检验"独立 Connection 是否真正独立"：实测 AdsLib 按远端 IP 复用
    # 单条 TCP，即便客户端串行仍可能 1861——三种模式都属于探索性证据。
    hard_invariant = mode in {"locked", "sequential"} and scenario != "A10"
    if hard_invariant:
        # 串行化硬不变量：不允许崩溃、挂死。
        assert not crashed, f"{mode} mode crashed: {evidence.get('stderr_tail')}"
        assert result is not None, f"{mode} mode produced no result (rc={rc})"
        assert result.get("hung") is False, f"{mode} mode hung: {result}"
        if fault == "none":
            assert result.get("failure") == 0, f"{mode} mode had failures: {result}"
        elif fault == "reject":
            hist = result.get("error_hist", {})
            # 串行下拒绝必须精确呈现为 ADS 1808，而非 -1/1861/崩溃。
            assert all(code.endswith(":1808") for code in hist), hist
        elif fault == "timeout" and scenario != "A05":
            hist = result.get("error_hist", {})
            # 串行下不应出现并发竞争型 -1；只允许同步超时 1861。
            # A05 超时窗口内 Sum Write 的错误形态单独评估，不在此约束。
            assert "ADSError:-1" not in hist, hist
        # disconnect：链路已死，-1/1861 都是合理表现，只要有界返回。
        if scenario in {"A06", "A07"}:
            assert result.get("residual_notifications") == 0, result

    # unlocked / A10 cell 不设成功断言：安全性结论由证据汇总回答。
