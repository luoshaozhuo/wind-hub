"""契约测试收集前的只读生成物一致性检查。

pb2/grpc/pyi 是签入仓库的生成物，与源 Proto 必须字节一致。测试收集只
执行 ``scripts/generate_rpc.py --check`` 的临时目录比对，绝不就地重写
生产绑定；发现过期时明确报错并要求先提交重新生成的产物。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _ensure_generated() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/generate_rpc.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "stale contract bindings; regenerate and commit via "
            "scripts/generate_rpc.py:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )


_ensure_generated()
