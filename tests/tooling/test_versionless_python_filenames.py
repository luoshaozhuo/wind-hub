"""Code module filenames are stable; versions live in data and wire contracts."""

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[2]
_VERSIONED_PYTHON = re.compile(r"_v[0-9]+(?:_[0-9]+)*\.py$")


@pytest.mark.parametrize("directory", ["src", "tests", "scripts"])
def test_python_source_filenames_have_no_embedded_version(directory: str) -> None:
    names = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / directory).rglob("*.py")
        if _VERSIONED_PYTHON.search(path.name)
    ]
    assert not names, f"versioned Python source files: {sorted(names)}"
