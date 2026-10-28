"""防火墙只读诊断 firewall.py 契约。

- 工具存在/缺失：至少一个成功 → PASS；全部失败 → UNKNOWN。
- nftables JSON：基链与候选规则解析；规则无法判定时永远 UNKNOWN。
- 截断/畸形 JSON 不产生结论。
- 参数边界、超时、取消时子进程回收。
- 全部命令为 list-ruleset/save/status 只读，不出现 add/insert/delete。
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from core.application.diagnostic import DiagnosticStatus
from core.infrastructure.diagnostic import firewall

_READONLY_TOKENS = ("list", "save", "status", "state")
_MUTATING_TOKENS = ("add", "insert", "delete", "replace", "flush")

_NFT_RULESET = """
{"nftables":[
  {"table":{"family":"ip","name":"filter"}},
  {"chain":{"family":"ip","table":"filter","name":"input","hook":"input","type":"filter"}},
  {"rule":{"family":"ip","table":"filter","chain":"input","handle":5,
    "expr":[{"match":{"op":"==","left":{"payload":{"protocol":"tcp","field":"dport"}},"right":502}},
            {"accept":null}]}},
  {"rule":{"family":"ip","table":"filter","chain":"input","handle":9,
    "expr":[{"match":{"op":"==","left":{"payload":{"protocol":"udp","field":"dport"}},"right":503}},
            {"drop":null}]}}
]}
"""


@pytest.fixture
def fake_read(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, ...]] = []
    answers: dict[str, tuple[int, str, str]] = {}
    default = (127, "", "not available")

    async def _fake(*argv: str) -> tuple[int, str, str]:
        calls.append(argv)
        return answers.get(argv[0], default)

    monkeypatch.setattr(firewall, "_read", _fake)
    return calls, answers


def test_all_tools_missing_is_unknown(fake_read) -> None:
    result = asyncio.run(firewall.firewall_status())

    assert result.status is DiagnosticStatus.UNKNOWN
    assert result.findings[0].code == "FIREWALL_QUERY_UNAVAILABLE"


async def test_one_available_tool_is_pass_and_commands_are_readonly(fake_read) -> None:
    calls, answers = fake_read
    answers["ufw"] = (0, "Status: active\n", "")

    result = await firewall.firewall_status()

    assert result.status is DiagnosticStatus.PASS
    assert result.findings[0].code == "FIREWALL_RULES_READ"
    assert result.evidence["ufw"]["exit_code"] == 0
    assert result.evidence["nftables"]["exit_code"] == 127
    assert len(calls) == 5
    for argv in calls:
        assert any(token in arg for arg in argv for token in _READONLY_TOKENS)
        assert not any(arg in _MUTATING_TOKENS for arg in argv)


async def test_large_stdout_is_truncated_and_flagged(fake_read) -> None:
    _, answers = fake_read
    answers["nft"] = (0, "x" * 200_001, "")

    result = await firewall.firewall_status()
    nft = result.evidence["nftables"]

    assert nft["truncated"] is True
    assert len(nft["stdout"]) == 200_000


async def test_firewall_check_extracts_chains_and_candidates_but_unknown(fake_read) -> None:
    _, answers = fake_read
    answers["nft"] = (0, _NFT_RULESET, "")

    result = await firewall.firewall_check(502, direction="inbound", protocol="tcp")

    assert result.status is DiagnosticStatus.UNKNOWN
    assert result.findings[0].code == "FIREWALL_DECISION_UNDETERMINED"
    assert result.evidence["decision"] == "UNKNOWN"
    chains = result.evidence["base_chains"]
    assert any(chain["hook"] == "input" for chain in chains)
    candidates = result.evidence["candidate_rules"]
    assert len(candidates) == 1
    assert candidates[0]["handle"] == 5
    assert "nftables" in result.evidence["sources_available"]


async def test_firewall_check_outbound_chain_selection(fake_read) -> None:
    ruleset = """
    {"nftables":[
      {"chain":{"family":"ip","table":"filter","name":"output","hook":"output"}},
      {"rule":{"family":"ip","table":"filter","chain":"output","handle":3,
        "expr":[{"match":{"left":{"payload":{"protocol":"udp","field":"dport"}},"right":53}},
                {"accept":null}]}}
    ]}
    """
    _, answers = fake_read
    answers["nft"] = (0, ruleset, "")

    result = await firewall.firewall_check(53, direction="outbound", protocol="udp")

    assert result.status is DiagnosticStatus.UNKNOWN
    assert result.evidence["base_chains"][0]["hook"] == "output"
    assert result.evidence["candidate_rules"][0]["handle"] == 3


async def test_truncated_nft_output_yields_no_candidates(fake_read) -> None:
    _, answers = fake_read
    answers["nft"] = (0, _NFT_RULESET, "")
    # 手动制造截断标记。
    original = firewall.firewall_status

    async def truncated_status() -> object:
        snapshot = await original()
        snapshot.evidence["nftables"]["truncated"] = True
        return snapshot

    import core.infrastructure.diagnostic.firewall as module

    module.firewall_status = truncated_status  # type: ignore[method-assign]
    try:
        result = await firewall.firewall_check(502)
    finally:
        module.firewall_status = original  # type: ignore[method-assign]

    assert result.evidence["candidate_rules"] == []


async def test_malformed_nft_json_is_swallowed_not_crashed(fake_read) -> None:
    _, answers = fake_read
    answers["nft"] = (0, "{not json", "")

    result = await firewall.firewall_check(502)

    assert result.status is DiagnosticStatus.UNKNOWN
    assert result.evidence["candidate_rules"] == []


async def test_no_nft_available_still_returns_unknown(fake_read) -> None:
    result = await firewall.firewall_check(502)

    assert result.status is DiagnosticStatus.UNKNOWN
    assert result.evidence["candidate_rules"] == []
    assert result.evidence["sources_available"] == []


@pytest.mark.parametrize(
    ("port", "direction", "protocol"),
    [
        (0, "inbound", "tcp"),
        (65536, "inbound", "tcp"),
        (502, "sideways", "tcp"),
        (502, "inbound", "icmp"),
    ],
)
async def test_firewall_check_parameter_bounds(
    fake_read, port: int, direction: str, protocol: str
) -> None:
    with pytest.raises(ValueError):
        await firewall.firewall_check(port, direction=direction, protocol=protocol)


# --------------------------------------------------------------------------
# 真实子进程超时/取消回收（firewall._read 固定 5s 超时，这里用取消验证）
# --------------------------------------------------------------------------

_MARKER = "windhubfwmarker_2b9d"
_SLEEPER_CODE = (
    "import time,sys; " f"sys.stdout.write('{_MARKER}'); sys.stdout.flush(); time.sleep(30)"
)


async def _sleeper_pids() -> list[int]:
    proc = await asyncio.create_subprocess_exec(
        "pgrep", "-f", _MARKER, stdout=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    return [int(line) for line in out.split()]


async def test_read_cancellation_reaps_child() -> None:
    task = asyncio.create_task(firewall._read(sys.executable, "-c", _SLEEPER_CODE))
    for _ in range(20):
        if await _sleeper_pids():
            break
        await asyncio.sleep(0.02)
    assert await _sleeper_pids()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not await _sleeper_pids()


async def test_read_missing_binary_returns_127() -> None:
    code, _, _ = await firewall._read("/nonexistent/wind-hub-fw-bin")
    assert code == 127


async def test_nft_non_dict_elements_and_rules_are_skipped(fake_read) -> None:
    ruleset = """
    {"nftables":[
      "not-a-dict",
      {"chain":{"family":"ip","table":"filter","name":"input","hook":"input"}},
      {"rule":"not-a-dict"},
      {"rule":{"family":"ip","table":"filter","chain":"input","handle":7,
        "expr":[{"match":{"left":{"payload":{"protocol":"tcp","field":"dport"}},"right":502}},
                {"accept":null}]}}
    ]}
    """
    _, answers = fake_read
    answers["nft"] = (0, ruleset, "")

    result = await firewall.firewall_check(502)

    assert result.status is DiagnosticStatus.UNKNOWN
    assert [c["handle"] for c in result.evidence["candidate_rules"]] == [7]


async def test_read_timeout_returns_124(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProcess:
        returncode = None

        def __init__(self) -> None:
            self._calls = 0

        async def communicate(self):
            self._calls += 1
            if self._calls == 1:
                raise TimeoutError
            return b"", b""

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*argv, **kwargs):
        return proc

    proc = FakeProcess()

    monkeypatch.setattr(firewall.asyncio, "create_subprocess_exec", fake_exec)

    code, _, err = await firewall._read("whatever")

    assert code == 124
    assert err == "timed out"
