"""生成 .claude/settings.local.json（本地工具权限配置，不进仓库）。

背景：wind-hub conda 环境里的 mypy/ruff/pytest/lint-imports 绝对路径
不在项目 .claude/settings.json 的 allow 列表里；auto 权限模式下未命中
allow 的命令会走 AI 安全分类器，分类器不可用时会被整体拦截。把绝对路径
规则写入本地 settings（已被 .gitignore 忽略），即可走精确匹配放行。

用法::

    python scripts/write_claude_settings_local.py   # 幂等写入
    pytest --noconftest scripts/write_claude_settings_local.py  # 自校验

脚本只写 .claude/settings.local.json 这一个文件；已存在且内容一致时不改动。
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = REPO_ROOT / ".claude" / "settings.local.json"

# 与方案 1 授权内容保持一致：wind-hub 环境绝对路径放行。
SETTINGS = {
    "permissions": {
        "allow": [
            "Bash(/home/luo/miniconda3/envs/wind-hub/bin/mypy *)",
            "Bash(/home/luo/miniconda3/envs/wind-hub/bin/ruff *)",
            "Bash(/home/luo/miniconda3/envs/wind-hub/bin/pytest *)",
            "Bash(/home/luo/miniconda3/envs/wind-hub/bin/lint-imports *)",
            "Bash(/home/luo/miniconda3/envs/wind-hub/bin/python -c *)",
            "Bash(/home/luo/miniconda3/envs/wind-hub/bin/python -m pytest *)",
            "Bash(/home/luo/miniconda3/envs/wind-hub/bin/wind-hub *)",
        ]
    }
}


def main() -> Path:
    """幂等写入 settings.local.json，返回目标路径。"""
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(SETTINGS, indent=2, ensure_ascii=False) + "\n"
    if TARGET.exists() and TARGET.read_text(encoding="utf-8") == content:
        print(f"已是最新: {TARGET}")
        return TARGET
    TARGET.write_text(content, encoding="utf-8")
    print(f"已写入: {TARGET}")
    return TARGET


def test_write_settings_local() -> None:
    """自校验：main() 写出的文件可被 json 解析且含 mypy 放行规则。"""
    path = main()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permissions"]["allow"][0].endswith("/bin/mypy *)")


if __name__ == "__main__":
    main()
