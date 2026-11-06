"""通用只读协议诊断：调用方移交专用 Driver，Core 独占其生命周期。"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence

from core.application.diagnostic import DiagnosticFinding, DiagnosticResult, DiagnosticStatus
from core.application.port.protocol import ProtocolPort
from core.application.protocol_contract import ProtocolCapability
from core.infrastructure.diagnostic.benchmark import benchmark


def _finding(code: str, status: DiagnosticStatus, message: str) -> DiagnosticFinding:
    return DiagnosticFinding(code=code, status=status, message=message)


def _validate_timeout(timeout: float) -> None:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be a finite positive number")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number")


def _result_with_close(
    result: DiagnosticResult,
    close_error: Exception | None,
) -> DiagnosticResult:
    if close_error is None:
        return result
    return DiagnosticResult(
        category=result.category,
        target=result.target,
        status=DiagnosticStatus.UNKNOWN,
        findings=(
            *result.findings,
            _finding(
                "CLOSE_FAILED",
                DiagnosticStatus.UNKNOWN,
                f"Driver release could not be confirmed: {type(close_error).__name__}",
            ),
        ),
        measurements=result.measurements,
        evidence=result.evidence,
    )


async def _close_owned(driver: ProtocolPort, deadline: float) -> Exception | None:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        return TimeoutError("diagnostic deadline exhausted before close")
    try:
        async with asyncio.timeout(remaining):
            await driver.close()
    except Exception as exc:
        return exc
    return None


async def check_protocol(
    driver: ProtocolPort,
    *,
    point_id: str | None = None,
    timeout: float = 5.0,
) -> DiagnosticResult:
    """拥有专用 Driver 的一次连接、健康检查和可选只读点读取。"""
    _validate_timeout(timeout)
    target = type(driver).__name__
    deadline = asyncio.get_running_loop().time() + timeout
    evidence: dict[str, object] = {}
    findings: list[DiagnosticFinding] = []
    status = DiagnosticStatus.PASS
    try:
        async with asyncio.timeout_at(deadline):
            await driver.connect()
            evidence["connected"] = driver.is_open()
            health = await driver.health()
            evidence["healthy"] = health.healthy
            if not health.healthy:
                status = DiagnosticStatus.FAIL
                findings.append(_finding(
                    "UNHEALTHY", status, health.message or "Protocol health check failed"
                ))
            elif point_id is not None:
                if ProtocolCapability.READ not in driver.capabilities():
                    status = DiagnosticStatus.SKIPPED
                    findings.append(_finding(
                        "READ_UNSUPPORTED", status, "Driver does not support active reads"
                    ))
                else:
                    sample = await driver.read_one(point_id)
                    evidence["point"] = {
                        "point_id": sample.point_id,
                        "quality": sample.quality.value,
                        "timestamp": sample.timestamp.isoformat(),
                    }
                    findings.append(_finding(
                        "PROTOCOL_OK", DiagnosticStatus.PASS, "Read-only probe succeeded"
                    ))
            else:
                findings.append(_finding(
                    "PROTOCOL_OK", DiagnosticStatus.PASS, "Protocol health verified"
                ))
    except TimeoutError:
        status = DiagnosticStatus.FAIL
        findings.append(_finding(
            "DIAGNOSTIC_TIMEOUT", status, "Protocol diagnostic deadline expired"
        ))
    except Exception as exc:
        status = DiagnosticStatus.FAIL
        findings.append(_finding(
            "PROTOCOL_DIAGNOSTIC_FAILED", status,
            f"Protocol probe failed: {type(exc).__name__}"
        ))
    # Close belongs to the diagnostic and consumes the remaining global deadline.
    result = DiagnosticResult(
        category="protocol", target=target, status=status,
        findings=tuple(findings), evidence=evidence,
    )
    return _result_with_close(result, await _close_owned(driver, deadline))


async def benchmark_protocol(
    driver: ProtocolPort,
    point_ids: Sequence[str],
    *,
    samples: int = 10,
    interval: float = 0.2,
    timeout: float = 5.0,
) -> DiagnosticResult:
    """拥有专用 Driver 的有界只读基准；不进行写入、订阅或召唤。"""
    if not point_ids or len(point_ids) > 200 or len(set(point_ids)) != len(point_ids):
        raise ValueError("benchmark requires 1..200 unique point IDs")
    _validate_timeout(timeout)
    target = type(driver).__name__
    deadline = asyncio.get_running_loop().time() + timeout
    result: DiagnosticResult
    try:
        async with asyncio.timeout_at(deadline):
            await driver.connect()
            caps = driver.capabilities()
            if len(point_ids) == 1 and ProtocolCapability.READ not in caps:
                raise ValueError("Driver lacks single-point read capability")
            if len(point_ids) > 1 and ProtocolCapability.READ_MANY not in caps:
                raise ValueError("Driver lacks native batch read capability")

            async def read() -> int:
                if len(point_ids) == 1:
                    await driver.read_one(point_ids[0])
                    return 1
                return len(await driver.read_many(point_ids))

            result = await benchmark(
                target, read, samples=samples, interval=interval
            )
    except TimeoutError:
        result = DiagnosticResult(
            category="benchmark", target=target, status=DiagnosticStatus.FAIL,
            findings=(_finding(
                "DIAGNOSTIC_TIMEOUT", DiagnosticStatus.FAIL,
                "Protocol benchmark deadline expired",
            ),),
        )
    except Exception as exc:
        result = DiagnosticResult(
            category="benchmark", target=target, status=DiagnosticStatus.FAIL,
            findings=(_finding(
                "BENCHMARK_FAILED", DiagnosticStatus.FAIL,
                f"Read-only benchmark failed: {type(exc).__name__}",
            ),),
        )
    return _result_with_close(result, await _close_owned(driver, deadline))
