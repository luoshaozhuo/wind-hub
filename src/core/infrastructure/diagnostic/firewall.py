"""Linux 本机防火墙只读检查。

规则可见不等于流量实际可达。复杂规则及未确认的命中路径返回 UNKNOWN。
"""

from __future__ import annotations

import asyncio
import json

from core.application.diagnostic import DiagnosticFinding, DiagnosticResult, DiagnosticStatus

_MAX_OUTPUT = 200_000


async def _read(*argv: str) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, "", "timed out"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _result(
    status: DiagnosticStatus,
    code: str,
    message: str,
    evidence: dict[str, object],
) -> DiagnosticResult:
    return DiagnosticResult(
        category="firewall",
        target="localhost",
        status=status,
        findings=(DiagnosticFinding(code=code, status=status, message=message),),
        evidence=evidence,
    )


async def firewall_status() -> DiagnosticResult:
    """列出规则系统的读取结果和管理工具状态；不修改规则。"""
    commands = {
        "nftables": ("nft", "-j", "list", "ruleset"),
        "iptables_ipv4": ("iptables-save",),
        "iptables_ipv6": ("ip6tables-save",),
        "ufw": ("ufw", "status"),
        "firewalld": ("firewall-cmd", "--state"),
    }
    observations: dict[str, object] = {}
    available = False
    for name, argv in commands.items():
        code, stdout, stderr = await _read(*argv)
        if code == 0:
            available = True
        observations[name] = {
            "exit_code": code,
            "stdout": stdout[:_MAX_OUTPUT],
            "stderr": stderr[:1000],
            "truncated": len(stdout) > _MAX_OUTPUT,
        }
    status = DiagnosticStatus.PASS if available else DiagnosticStatus.UNKNOWN
    return _result(
        status,
        "FIREWALL_RULES_READ" if available else "FIREWALL_QUERY_UNAVAILABLE",
        "Firewall configuration snapshot; not proof of end-to-end reachability",
        observations,
    )


async def firewall_check(
    port: int,
    *,
    direction: str = "inbound",
    protocol: str = "tcp",
) -> DiagnosticResult:
    """返回可核查的规则证据，不凭静态字符串匹配武断判断 ALLOW/DENY。"""
    if not 1 <= port <= 65535:
        raise ValueError("invalid port")
    if direction not in {"inbound", "outbound"} or protocol not in {"tcp", "udp"}:
        raise ValueError("invalid direction or protocol")
    snapshot = await firewall_status()
    nft = snapshot.evidence.get("nftables")
    chains: list[dict[str, object]] = []
    candidate_rules: list[dict[str, object]] = []
    if isinstance(nft, dict) and nft.get("exit_code") == 0 and not nft.get("truncated"):
        try:
            document = json.loads(str(nft["stdout"]))
            matched_tables: set[tuple[str, str]] = set()
            for element in document.get("nftables", []):
                if not isinstance(element, dict):
                    continue
                chain = element.get("chain")
                if isinstance(chain, dict) and chain.get("hook") == (
                    "input" if direction == "inbound" else "output"
                ):
                    chains.append(chain)
                    matched_tables.add((str(chain.get("family")), str(chain.get("table"))))
            for element in document.get("nftables", []):
                rule = element.get("rule") if isinstance(element, dict) else None
                if not isinstance(rule, dict):
                    continue
                if (str(rule.get("family")), str(rule.get("table"))) not in matched_tables:
                    continue
                if '"dport"' not in json.dumps(rule.get("expr", [])):
                    continue
                expression = json.dumps(rule.get("expr", []))
                if str(port) in expression and protocol in expression:
                    candidate_rules.append(
                        {
                            "chain": rule.get("chain"),
                            "handle": rule.get("handle"),
                            "expressions": rule.get("expr", []),
                        }
                    )
        except (ValueError, KeyError, TypeError):
            pass
    return _result(
        DiagnosticStatus.UNKNOWN,
        "FIREWALL_DECISION_UNDETERMINED",
        "Static rules cannot prove packet verdict without full tuple and runtime path; "
        "verify from peer and inspect packet counters/trace when authorized",
        {
            "port": port,
            "direction": direction,
            "protocol": protocol,
            "decision": "UNKNOWN",
            "base_chains": chains,
            "candidate_rules": candidate_rules,
            "candidate_warning": "Candidates are not a complete ordered firewall verdict",
            "sources_available": sorted(
                key
                for key, value in snapshot.evidence.items()
                if isinstance(value, dict) and value.get("exit_code") == 0
            ),
        },
    )
