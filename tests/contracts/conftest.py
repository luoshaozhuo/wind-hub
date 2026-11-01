"""契约测试收集前先运行权威生成器。

pb2/grpc/pyi 是生成物且不入库；任何收集本包的测试运行都必须先通过
``scripts/generate_rpc.py`` 生成全部 6 份 Proto 的 Python 产物，保证
契约测试验证的是当前 Proto 源文件对应的生成物，而非遗留旧产物。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _ensure_generated() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/generate_rpc.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "scripts/generate_rpc.py failed:\n" f"stdout={result.stdout}\nstderr={result.stderr}"
        )


_ensure_generated()
