"""Verify exact secret-scan allowlisting without weakening forbidden patterns."""

import importlib.util
from pathlib import Path
from types import ModuleType


def _scanner() -> ModuleType:
    path = Path("scripts/secret_scan.py")
    spec = importlib.util.spec_from_file_location("secret_scan", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exact_allowlist_text_does_not_cover_a_new_match() -> None:
    scanner = _scanner()
    marker = "Bearer" + " "
    allowed = (
        "src/cua/discovery/openai.py",
        'headers={"Authorization": f"' + marker + '{key}"},',
    )
    changed = next(
        iter(
            scanner._matches(
                "src/cua/discovery/openai.py",
                'headers={"Authorization": f"' + marker + '{other_key}"},',
                "tracked",
            )
        )
    )
    assert (changed.path, changed.text) != allowed


def test_key_prefix_is_permanently_forbidden() -> None:
    scanner = _scanner()
    finding = next(iter(scanner._matches("sample.txt", "s" + "k-example", "tracked")))
    assert finding.pattern.startswith("forbidden:")
