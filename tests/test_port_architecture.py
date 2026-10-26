"""Keep native mechanisms inside each product's independent port boundary."""

import ast
import sys
from functools import cache
from importlib.util import resolve_name
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PRODUCTS = (ROOT / "src/physiclaw", ROOT / "hardware")
NATIVE_MODULES = {"ctypes", "fcntl", "msvcrt", "termios", "tty", "_winapi"}


@cache
def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(path: Path) -> set[str]:
    modules = set()
    source_root = ROOT / "src" if path.is_relative_to(ROOT / "src") else ROOT
    package = ".".join(path.parent.relative_to(source_root).parts)
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = resolve_name("." * node.level + base, package)
            modules.update(f"{base}.{alias.name}" for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            modules.add(node.args[0].value)
    return modules


@cache
def _workflows(product: Path) -> list[Path]:
    return [
        path
        for path in product.rglob("*.py")
        if "port" not in path.relative_to(product).parts
    ]


@pytest.mark.parametrize("product", PRODUCTS, ids=["application", "hardware"])
def test_port_depends_only_on_itself_and_its_runtime_libraries(product):
    prefix = f"{product.name}.port"
    libraries = {"cv2", "numpy"} if product.name == "physiclaw" else set()

    violations = [
        f"{path.relative_to(ROOT)} -> {module}"
        for path in (product / "port").rglob("*.py")
        for module in _imports(path)
        if not module.startswith(prefix + ".")
        and module.split(".")[0]
        not in sys.stdlib_module_names | libraries | {"__future__"}
    ]

    assert not violations, "port must remain an independent leaf:\n" + "\n".join(
        violations
    )


@pytest.mark.parametrize("product", PRODUCTS, ids=["application", "hardware"])
def test_workflows_use_public_port_api_and_no_native_imports(product):
    private = (f"{product.name}.port.backends", f"{product.name}.port._")

    violations = [
        f"{path.relative_to(ROOT)} -> {module}"
        for path in _workflows(product)
        for module in _imports(path)
        if module.startswith(private) or module.split(".")[0] in NATIVE_MODULES
    ]

    assert not violations, "workflow bypasses its port:\n" + "\n".join(violations)


def _selects_platform(node: ast.AST) -> bool:
    return any(
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and (value.value.id, value.attr)
        in {("sys", "platform"), ("os", "name"), ("platform", "system")}
        for value in ast.walk(node)
    )


@pytest.mark.parametrize("product", PRODUCTS, ids=["application", "hardware"])
def test_workflows_do_not_select_os_implementations(product):
    violations = [
        f"{path.relative_to(ROOT)}:{node.lineno}"
        for path in _workflows(product)
        for node in ast.walk(_tree(path))
        if isinstance(node, (ast.If, ast.IfExp, ast.Compare))
        and _selects_platform(
            node.test if isinstance(node, (ast.If, ast.IfExp)) else node
        )
    ]

    assert not violations, "OS selection belongs in port:\n" + "\n".join(violations)
