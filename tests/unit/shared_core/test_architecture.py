from __future__ import annotations

import ast
from pathlib import Path

_CORE = Path(__file__).parents[3] / "src" / "core"
_FORBIDDEN_PACKAGES = {
    "wind_hub_core",
    "wind_hub_collector",
    "wind_hub_commander",
    "wind_hub_server",
    "wind_hub_ctl",
}


def _python_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.rglob("*.py")))


def _import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_new_core_does_not_depend_on_legacy_or_process_packages() -> None:
    violations: list[str] = []
    for path in _python_files(_CORE):
        forbidden = _import_roots(path) & _FORBIDDEN_PACKAGES
        if forbidden:
            relative = path.relative_to(_CORE)
            violations.append(f"{relative}: {sorted(forbidden)}")

    assert violations == []


def test_application_port_modules_contain_interfaces_only() -> None:
    port_dir = _CORE / "application" / "port"
    violations: list[str] = []

    for path in _python_files(port_dir):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            base_names = {
                base.id
                for base in node.bases
                if isinstance(base, ast.Name)
            }
            if "Protocol" not in base_names:
                violations.append(
                    f"{path.name}:{node.lineno} class {node.name} "
                    "is not a typing.Protocol"
                )

    assert violations == []
