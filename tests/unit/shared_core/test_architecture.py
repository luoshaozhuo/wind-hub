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



def test_domain_does_not_depend_on_application_or_infrastructure() -> None:
    violations: list[str] = []

    for path in _python_files(_CORE / "domain"):
        tree = ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            if node.module.startswith(
                ("core.application", "core.infrastructure")
            ):
                violations.append(
                    f"{path.name}:{node.lineno} imports {node.module}"
                )

    assert violations == []


def test_domain_protocol_models_do_not_embed_adapter_options() -> None:
    forbidden_tokens = {
        "protocol_options",
        "register_type",
        "word_order",
        "target_net_id",
        "index_group",
        "index_offset",
        "common_addr",
        "ioa",
    }
    violations: list[str] = []

    for path in _python_files(_CORE / "domain"):
        source = path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            if token in source:
                violations.append(f"{path.name}: contains '{token}'")

    assert violations == []

def test_infrastructure_imports_domain_types_from_domain() -> None:
    """Infrastructure 不得通过 application.config 间接导入 Domain 类型。"""
    domain_names = {
        "BusinessPoint",
        "BusinessPointId",
        "ConnectionEndpoint",
        "Device",
        "DeviceGroup",
        "DeviceGroupId",
        "DeviceId",
        "DeviceModel",
        "DeviceModelId",
        "DeviceType",
        "DeviceTypeId",
        "PointAccess",
        "PointTable",
        "PointTableId",
        "Protocol",
        "PointDefinition",
        "RawDataType",
        "ValueType",
    }
    violations: list[str] = []

    for path in _python_files(_CORE / "infrastructure"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "core.application.config":
                continue
            imported = {alias.name for alias in node.names}
            leaked = imported & domain_names
            if leaked:
                relative = path.relative_to(_CORE)
                violations.append(
                    f"{relative}:{node.lineno} imports Domain types "
                    f"from application.config: {sorted(leaked)}"
                )

    assert violations == []

