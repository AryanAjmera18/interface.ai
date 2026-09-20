#!/usr/bin/env python3
"""Fail a public-repository scan on unreviewed credential-like content."""

from __future__ import annotations

import base64
import hashlib
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field


class AllowEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    matched_text_b64: str
    reason: str = Field(min_length=1)

    @property
    def matched_text(self) -> str:
        return base64.b64decode(self.matched_text_b64).decode("utf-8")


class AllowFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: tuple[AllowEntry, ...]


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    text: str
    pattern: str
    scopes: frozenset[str]


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _patterns() -> tuple[tuple[str, str, bool], ...]:
    key = dotenv_values(".env").get("OPENAI_API_KEY")
    values: list[tuple[str, str, bool]] = []
    if key:
        values.append(("OPENAI_API_KEY value", key, True))
    values.extend(
        (
            ("key prefix", "s" + "k-", True),
            ("lowercase API key label", "api" + "_key", False),
            ("authorization header", "Authorization" + ":", False),
            ("bearer credential", "Bearer" + " ", False),
        )
    )
    return tuple(values)


def _matches(path: str, text: str, scope: str) -> Iterable[Finding]:
    for label, needle, forbidden in _patterns():
        if needle in text:
            yield Finding(
                path=path,
                text=text.strip(),
                pattern=("forbidden:" if forbidden else "reviewable:") + label,
                scopes=frozenset({scope}),
            )


def _history_findings() -> Iterable[Finding]:
    history = _git(
        "log",
        "--all",
        "-p",
        "--no-color",
        "--no-ext-diff",
        "--format=commit %H",
    )
    old_path = "<unknown>"
    new_path = "<unknown>"
    for line in history.splitlines():
        if line.startswith("--- a/"):
            old_path = line[6:]
            continue
        if line.startswith("+++ b/"):
            new_path = line[6:]
            continue
        if line.startswith("--- /dev/null"):
            old_path = "<none>"
            continue
        if line.startswith("+++ /dev/null"):
            new_path = "<none>"
            continue
        if line.startswith("+") and not line.startswith("+++"):
            yield from _matches(new_path, line[1:], "history")
        elif line.startswith("-") and not line.startswith("---"):
            yield from _matches(old_path, line[1:], "history")


def _tracked_findings() -> Iterable[Finding]:
    for name in _git("ls-files", "-z").split("\0"):
        if not name:
            continue
        path = Path(name)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            yield from _matches(name.replace("\\", "/"), line, "tracked")


def _merge(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    merged: dict[tuple[str, str, str], set[str]] = {}
    for finding in findings:
        key = (finding.path, finding.text, finding.pattern)
        merged.setdefault(key, set()).update(finding.scopes)
    return tuple(
        Finding(path=path, text=text, pattern=pattern, scopes=frozenset(scopes))
        for (path, text, pattern), scopes in sorted(merged.items())
    )


def _display(finding: Finding) -> str:
    if finding.pattern.startswith("forbidden:"):
        digest = hashlib.sha256(finding.text.encode()).hexdigest()[:12]
        return f"{finding.path} pattern={finding.pattern} content_sha256={digest}"
    return f"{finding.path} text={finding.text!r}"


def _emit(message: str) -> None:
    sys.stdout.write(message + "\n")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    allow = AllowFile.model_validate_json(
        (root / "scripts/secret_scan_allowlist.json").read_text(encoding="utf-8")
    )
    allowed = {(entry.path, entry.matched_text): entry for entry in allow.entries}
    for entry in allow.entries:
        if any(
            needle in entry.matched_text for _label, needle, forbidden in _patterns() if forbidden
        ):
            _emit(f"FAIL invalid allowlist entry for {entry.path}: forbidden pattern")
            return 1

    findings = _merge((*_history_findings(), *_tracked_findings()))
    allowlisted: list[tuple[Finding, AllowEntry]] = []
    failures: list[Finding] = []
    for finding in findings:
        entry = allowed.get((finding.path, finding.text))
        if entry is not None and not finding.pattern.startswith("forbidden:"):
            allowlisted.append((finding, entry))
        else:
            failures.append(finding)

    used = {(finding.path, finding.text) for finding, _entry in allowlisted}
    stale = sorted(set(allowed) - used)
    for finding, entry in allowlisted:
        scopes = ",".join(sorted(finding.scopes))
        _emit(f"ALLOWLISTED [{scopes}] {_display(finding)} reason={entry.reason}")
    for finding in failures:
        scopes = ",".join(sorted(finding.scopes))
        _emit(f"FAIL [{scopes}] {_display(finding)}")
    for path, text in stale:
        _emit(f"FAIL stale allowlist path={path} text={text!r}")

    failure_count = len(failures) + len(stale)
    _emit(f"SUMMARY allowlisted={len(allowlisted)} failures={failure_count}")
    return 1 if failure_count else 0


if __name__ == "__main__":
    sys.exit(main())
