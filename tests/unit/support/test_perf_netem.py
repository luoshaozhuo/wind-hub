"""Unit tests for ``tests/perf/netem.py`` — tc netem 封装。

``_run`` 与 ``os.geteuid`` 全部 monkeypatch，不执行真实 ``tc`` 命令。
"""

from __future__ import annotations

import subprocess

import pytest

from tests.performance import netem as netem_mod
from tests.performance.netem import NetemController, NetemScenario


class _FakeTc:
    """记录调用并按需失败的假 ``tc`` 执行器。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail_on: str | None = None

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if self.fail_on and self.fail_on in " ".join(args):
            return subprocess.CompletedProcess(args, 1, "", "RTNETLINK error")
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def fake_tc(monkeypatch: pytest.MonkeyPatch) -> _FakeTc:
    fake = _FakeTc()
    monkeypatch.setattr(netem_mod, "_run", fake)
    monkeypatch.setattr(netem_mod.os, "geteuid", lambda: 0)
    return fake


def _joined(fake: _FakeTc) -> list[str]:
    return [" ".join(c) for c in fake.calls]


def _assert_tokens_split(fake: _FakeTc) -> None:
    """tc 以 argv 逐 token 解析；任何含空格的单个参数都会被 tc 拒绝。"""
    for call in fake.calls:
        for token in call:
            assert " " not in token, f"unsplit tc token: {token!r} in {call}"


# ---------------------------------------------------------------------------
# NetemScenario
# ---------------------------------------------------------------------------


def test_scenario_defaults_are_ideal() -> None:
    s = NetemScenario(name="ideal")
    assert s.is_ideal is True
    assert s.delay_ms == 0.0 and s.jitter_ms == 0.0 and s.loss_pct == 0.0


def test_scenario_not_ideal_with_any_impairment() -> None:
    assert NetemScenario(name="d", delay_ms=1).is_ideal is False
    assert NetemScenario(name="l", loss_pct=0.1).is_ideal is False
    assert NetemScenario(name="o", outage_duration_s=5).is_ideal is False


# ---------------------------------------------------------------------------
# apply / clear
# ---------------------------------------------------------------------------


def test_apply_delay_and_jitter(fake_tc: _FakeTc) -> None:
    NetemController("veth-ws").apply(NetemScenario(name="j", delay_ms=10, jitter_ms=5))
    assert "tc qdisc replace dev veth-ws root netem delay 10ms 5ms" in _joined(fake_tc)
    _assert_tokens_split(fake_tc)


def test_apply_loss(fake_tc: _FakeTc) -> None:
    NetemController("veth-ws").apply(NetemScenario(name="l", loss_pct=1))
    assert "tc qdisc replace dev veth-ws root netem loss 1%" in _joined(fake_tc)
    _assert_tokens_split(fake_tc)


def test_apply_delay_and_loss_combined(fake_tc: _FakeTc) -> None:
    NetemController("veth-ws").apply(NetemScenario(name="dl", delay_ms=1, loss_pct=0.1))
    assert "tc qdisc replace dev veth-ws root netem delay 1ms loss 0.1%" in _joined(fake_tc)
    _assert_tokens_split(fake_tc)


def test_apply_ideal_is_clear(fake_tc: _FakeTc) -> None:
    NetemController("veth-ws").apply(NetemScenario(name="ideal"))
    # 理想场景不挂任何规则；且此前无 active 规则时连 del 都不发
    assert fake_tc.calls == []


def test_apply_requires_root(monkeypatch: pytest.MonkeyPatch, fake_tc: _FakeTc) -> None:
    monkeypatch.setattr(netem_mod.os, "geteuid", lambda: 1000)
    with pytest.raises(PermissionError, match="root"):
        NetemController("veth-ws").apply(NetemScenario(name="d", delay_ms=1))
    assert NetemController("veth-ws").check_permission() is False


def test_apply_tc_failure_raises(fake_tc: _FakeTc) -> None:
    fake_tc.fail_on = "qdisc replace"
    with pytest.raises(RuntimeError, match="应用 netem 失败"):
        NetemController("veth-ws").apply(NetemScenario(name="d", delay_ms=1))


def test_clear_is_idempotent(fake_tc: _FakeTc) -> None:
    c = NetemController("veth-ws")
    c.clear()  # 未 apply：不发命令
    assert fake_tc.calls == []
    c.apply(NetemScenario(name="d", delay_ms=1))
    c.clear()
    c.clear()  # 第二次 clear 不再发命令
    assert _joined(fake_tc).count("tc qdisc del dev veth-ws root") == 1


def test_clear_tolerates_tc_failure(fake_tc: _FakeTc) -> None:
    c = NetemController("veth-ws")
    c.apply(NetemScenario(name="d", delay_ms=1))
    fake_tc.fail_on = "qdisc del"
    c.clear()  # 接口已消失等失败语义上等同已清除，不抛错
    c.clear()
    assert len([c_ for c_ in fake_tc.calls if "del" in c_]) == 1


# ---------------------------------------------------------------------------
# 上下文管理器与中断
# ---------------------------------------------------------------------------


async def test_scenario_context_applies_and_clears(fake_tc: _FakeTc) -> None:
    c = NetemController("veth-ws")
    async with c.scenario(NetemScenario(name="d", delay_ms=50)):
        assert any("delay 50ms" in j for j in _joined(fake_tc))
    assert "tc qdisc del dev veth-ws root" in _joined(fake_tc)


async def test_scenario_context_clears_on_exception(fake_tc: _FakeTc) -> None:
    c = NetemController("veth-ws")
    with pytest.raises(RuntimeError, match="boom"):
        async with c.scenario(NetemScenario(name="d", delay_ms=50)):
            raise RuntimeError("boom")
    assert "tc qdisc del dev veth-ws root" in _joined(fake_tc)


async def test_simulate_outage_injects_full_loss_and_recovers(fake_tc: _FakeTc) -> None:
    c = NetemController("veth-ws")
    await c.simulate_outage(0.01)
    joined = _joined(fake_tc)
    assert "tc qdisc replace dev veth-ws root netem loss 100%" in joined
    _assert_tokens_split(fake_tc)
    # 恢复 = 删除规则
    assert "tc qdisc del dev veth-ws root" in joined


async def test_simulate_outage_requires_root(
    monkeypatch: pytest.MonkeyPatch, fake_tc: _FakeTc
) -> None:
    monkeypatch.setattr(netem_mod.os, "geteuid", lambda: 1000)
    with pytest.raises(PermissionError, match="root"):
        await NetemController("veth-ws").simulate_outage(0.01)
