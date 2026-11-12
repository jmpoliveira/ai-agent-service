"""Enforces the dependency rule: source code may only depend inwards.

    interface ──► application ──► domain ◄── infrastructure
                       ▲                            │
                       └────────────────────────────┘

``config`` and ``main.py`` sit outside the layers. ``main.py`` is the only module allowed
to import everything.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STDLIB = set(sys.stdlib_module_names)

# layer -> third-party top-level packages it may use, and project packages it may import
RULES: dict[str, dict[str, set[str]]] = {
    "domain": {"third_party": set(), "project": {"src.domain"}},
    "application": {
        # opentelemetry-api is a documented exception (docs/adr/0001-architecture.md)
        "third_party": {"opentelemetry"},
        "project": {"src.domain", "src.application"},
    },
    "infrastructure": {
        "third_party": {"*"},
        "project": {"src.domain", "src.application", "src.infrastructure"},
    },
    "interface": {
        "third_party": {"fastapi", "pydantic", "starlette"},
        "project": {"src.domain", "src.application", "src.interface"},
    },
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def _violations(layer: str) -> list[str]:
    rule = RULES[layer]
    problems: list[str] = []
    for path in sorted((ROOT / "src" / layer).rglob("*.py")):
        for module in _imports(path):
            top = module.split(".")[0]
            relative = path.relative_to(ROOT)
            if top in STDLIB or top == "__future__":
                continue
            if top in {"src", "config", "main", "tests"}:
                if top != "src" or not any(
                    module == p or module.startswith(p + ".") for p in rule["project"]
                ):
                    problems.append(f"{relative}: imports {module}")
            elif "*" not in rule["third_party"] and top not in rule["third_party"]:
                problems.append(f"{relative}: imports third-party {module}")
    return problems


@pytest.mark.parametrize("layer", list(RULES))
def test_layer_only_depends_inwards(layer: str):
    assert _violations(layer) == []


def test_config_does_not_depend_on_the_application_layers():
    offenders = [
        f"{p.relative_to(ROOT)}: {m}"
        for p in sorted((ROOT / "config").rglob("*.py"))
        for m in _imports(p)
        if m.split(".")[0] == "src"
    ]
    assert offenders == []


def test_the_guard_actually_detects_a_violation(tmp_path: Path):
    # Arrange: prove the checker is not vacuously green
    bad = tmp_path / "bad.py"
    bad.write_text("import fastapi\nfrom src.infrastructure.mcp import client\n")

    # Act
    found = _imports(bad)

    # Assert
    assert {"fastapi", "src.infrastructure.mcp"} <= found
