"""Verify exact and scope-limited secret-scan allowlisting."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _scanner() -> ModuleType:
    path = Path("scripts/secret_scan.py")
    spec = importlib.util.spec_from_file_location("secret_scan", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
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


def test_history_only_entry_does_not_excuse_tracked_match() -> None:
    scanner = _scanner()
    entry = scanner.AllowEntry(
        path="fixture.py",
        matched_text_b64="Zml4dHVyZSBwZXJzb24=",
        scope="history_only",
        reason="historical synthetic fixture",
    )
    historical = scanner.Finding(
        path="fixture.py",
        text="fixture person",
        pattern="reviewable:member name",
        scopes=frozenset({"history"}),
    )
    tracked = historical.model_copy(update={"scopes": frozenset({"tracked"})})
    assert scanner._entry_allows(historical, entry)
    assert not scanner._entry_allows(tracked, entry)
