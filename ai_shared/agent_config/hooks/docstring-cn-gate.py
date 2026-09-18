#!/usr/bin/env python3
"""兼容旧 hook 名称；实际规则已升级为通用注释与文档注释检查。"""

from __future__ import annotations

import runpy
from pathlib import Path

target = Path(__file__).with_name("comment-doc-gate.py")
runpy.run_path(str(target), run_name="__main__")
