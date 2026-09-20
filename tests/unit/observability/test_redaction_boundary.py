"""Keep all redaction and masking definitions in the observability choke point."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_only_observability_defines_redaction_or_masking_functions() -> None:
    offenders: list[str] = []
    for path in (ROOT / "src/cua").rglob("*.py"):
        if "observability" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                ("redact", "mask", "sanitize")
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}:{node.name}")
    assert offenders == [], "Redaction must be defined in cua.observability only: " + ", ".join(
        offenders
    )
