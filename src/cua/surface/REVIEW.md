# Part A and Part B review

Part A passed before Part B implementation: 165 passed, 2 skipped, 98.52% coverage. The final local gate enables Chromium smoke tests and passes 194 tests with 97.10% coverage. CI provisioning remains pending the requested scope approval; the workflow is unchanged.

## docs/schema/part-a-axtarget.patch

```diff
--- a/src/cua/domain/predicates.py
+++ b/src/cua/domain/predicates.py
@@ -15,21 +15,54 @@
 from cua.domain.observation import AxNode, AxState, Observation, walk_ax
 
 
+class AmbiguousTargetError(ValueError):
+    """More than one semantic match; refine the container or explicitly choose a scoped nth."""
+
+
 class AxTarget(DomainModel):
+    """Position is acceptable inside a semantic container, never as a substitute for one.
+
+    Depth counts targets including this leaf (at most three). Ancestors must themselves
+    resolve uniquely; an ambiguous container cannot silently widen the search.
+    """
+
     role: NonEmpty
     name_matcher: NameMatcher
     frame_path: tuple[str, ...] | None = None
     node_path: tuple[int, ...] | None = None
+    within: "AxTarget | None" = None
+    nth: int | None = Field(default=None, ge=0)
+
+    @model_validator(mode="after")
+    def bounded_scope(self) -> Self:
+        if self.nth is not None and self.within is None:
+            raise ValueError("nth requires a semantically identified within container")
+        depth, parent = 1, self.within
+        while parent is not None:
+            depth, parent = depth + 1, parent.within
+        if depth > 3:
+            raise ValueError("AxTarget recursion depth must not exceed 3 targets")
+        return self
 
     def select(self, observation: Observation) -> tuple[AxNode, ...]:
-        return tuple(
+        roots = self.within.select(observation) if self.within else (observation.ax_root,)
+        matches = tuple(
             node
-            for node in walk_ax(observation.ax_root)
+            for root in roots
+            for node in (walk_ax(root)[1:] if self.within else walk_ax(root))
             if node.role == self.role
             and self.name_matcher.matches(node.name)
             and (self.frame_path is None or node.frame_path == self.frame_path)
             and (self.node_path is None or node.node_path == self.node_path)
         )
+        if self.nth is not None:
+            return matches[self.nth : self.nth + 1]
+        if len(matches) > 1:
+            raise AmbiguousTargetError(
+                f"Expected one {self.role} with {self.name_matcher.describe()}; "
+                f"matched {len(matches)}. Add a semantic within scope or scoped nth."
+            )
+        return matches
 
 
 class AxNodeExists(DomainModel):
@@ -178,12 +211,13 @@
             + "; ".join(child.explanation for child in children),
         )
     if isinstance(predicate, AxNodeExists):
-        target = AxTarget(
-            role=predicate.role,
-            name_matcher=predicate.name_matcher,
-            frame_path=predicate.frame_path,
-        )
-        count = len(target.select(observation))
+        # A cardinality assertion intentionally counts all matches; it is not target resolution.
+        count = sum(
+            node.role == predicate.role
+            and predicate.name_matcher.matches(node.name)
+            and (predicate.frame_path is None or node.frame_path == predicate.frame_path)
+            for node in walk_ax(observation.ax_root)
+        )
         upper = predicate.max_count if predicate.max_count is not None else "unbounded"
         return PredicateResult(
             satisfied=count >= predicate.min_count
```

## tests/unit/domain/test_relative_scope.py

```python
"""Guard scoped targeting and the v2 migration; forbid external services."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from cua.domain.capability import Capability, ParamSpec
from cua.domain.migrations import load_capability
from cua.domain.names import NameMatcher
from cua.domain.observation import AxNode, Observation
from cua.domain.predicates import AmbiguousTargetError, AxTarget
from cua.domain.schemas import artifact_json
from cua.domain.steps import Step
from tests.unit.domain.demo_sample import demo_capability
from tests.unit.domain.samples import FixedClock, SequenceIds, capability, observation

ROOT = Path(__file__).resolve().parents[3]


def rows() -> Observation:
    root = AxNode(
        role="table",
        name="Accounts",
        children=tuple(
            AxNode(
                role="row",
                name=name,
                node_path=(i,),
                children=tuple(
                    AxNode(role="cell", name=value, node_path=(i, j))
                    for j, value in enumerate((name, "$123.45"))
                ),
            )
            for i, name in enumerate(("Checking", "Savings"))
        ),
    )
    return Observation.model_validate(
        {**observation().model_dump(exclude={"hash"}), "ax_root": root}
    )


def test_ambiguity_fails_closed_but_semantic_scope_reads_savings() -> None:
    target = AxTarget(role="cell", name_matcher=NameMatcher(value="$123.45"))
    with pytest.raises(AmbiguousTargetError, match=r"matched 2.*within"):
        target.select(rows())
    scoped = AxTarget(
        **target.model_dump(exclude={"within"}),
        within=AxTarget(role="row", name_matcher=NameMatcher(value="Savings")),
    )
    assert scoped.select(rows())[0].node_path == (1, 1)
    assert (
        scoped.model_copy(
            update={"within": AxTarget(role="row", name_matcher=NameMatcher(value="Missing"))}
        ).select(rows())
        == ()
    )
    ambiguous_parent = AxTarget(role="row", name_matcher=NameMatcher(mode="regex", value=".*"))
    with pytest.raises(AmbiguousTargetError):
        scoped.model_copy(update={"within": ambiguous_parent}).select(rows())


def test_nth_requires_scope_and_depth_is_bounded() -> None:
    data = {"role": "cell", "name_matcher": NameMatcher(mode="regex", value=".*")}
    with pytest.raises(ValidationError, match="nth requires"):
        AxTarget(**data, nth=0)
    row = AxTarget(role="row", name_matcher=NameMatcher(value="Savings"))
    assert AxTarget(**data, within=row, nth=1).select(rows())[0].name == "$123.45"
    assert AxTarget(**data, within=row, nth=8).select(rows()) == ()
    nested = AxTarget(**data, within=AxTarget(**data, within=row))
    with pytest.raises(ValidationError, match="depth"):
        AxTarget(**data, within=nested)


def test_tuple_order_is_identical_on_wire_and_in_memory() -> None:
    step = demo_capability().steps[0]
    parsed = Step.model_validate({**step.model_dump(), "on_outcome": step.on_outcome[::-1]})
    assert isinstance(parsed.on_outcome, tuple)
    codes = [entry.code for entry in parsed.on_outcome]
    assert (
        codes
        == sorted(codes)
        == [entry["code"] for entry in parsed.model_dump(mode="json")["on_outcome"]]
    )
    assert parsed.outcome_map is parsed.outcome_map
    with pytest.raises(TypeError):
        parsed.outcome_map["new"] = parsed.on_outcome[0].handling


@pytest.mark.parametrize("sensitivity", ["secret", "pii"])
@pytest.mark.parametrize("field", ["default", "example"])
def test_sensitive_literals_prohibited(sensitivity: str, field: str) -> None:
    data = capability().inputs[0].model_dump()
    data.update(required=False, sensitivity=sensitivity, example=None)
    data[field] = {"kind": "string", "value": "synthetic"}
    with pytest.raises(ValidationError, match="Sensitive"):
        ParamSpec.model_validate(data)


def test_required_default_and_null_parameter_prohibited() -> None:
    data = capability().inputs[0].model_dump()
    with pytest.raises(ValidationError, match="Required"):
        ParamSpec.model_validate({**data, "default": data["example"]})
    with pytest.raises(ValidationError):
        ParamSpec.model_validate({**data, "json_type": "null", "example": None})


@pytest.mark.parametrize("approved", [False, True])
def test_rich_golden_roundtrip(approved: bool, golden_file: Callable[[Path, bytes], bytes]) -> None:
    name = "capability.approved.v2.json" if approved else "capability.v2.json"
    cap = demo_capability(approved=approved)
    recorded = golden_file(ROOT / "tests/golden" / name, artifact_json(cap).encode())
    assert artifact_json(Capability.model_validate_json(recorded)).encode() == recorded
    assert len(cap.steps) >= 4 and len(cap.outcomes) == 3
    assert cap.outputs[0].extractor.target.within is not None
    if approved:
        with pytest.raises(ValidationError, match="approval"):
            Capability.model_validate({**cap.model_dump(), "provenance": capability().provenance})


def test_v1_migration_and_frozen_schemas() -> None:
    text = (ROOT / "tests/golden/capability.legacy-v1.json").read_text()
    migrated = load_capability(text, clock=FixedClock(), ids=SequenceIds())
    assert migrated.capability.schema_version == 2
    assert [(r.from_version, r.to_version) for r in migrated.migrations] == [(1, 2), (2, 2)]
    assert migrated.migrations[0].before_hash != migrated.migrations[0].after_hash
    old = json.loads(text)
    old["status"] = "approved"
    old["provenance"]["approval"] = demo_capability(approved=True).provenance.approval.model_dump(
        mode="json"
    )
    with pytest.raises(ValidationError, match="changed after approval"):
        load_capability(json.dumps(old), clock=FixedClock(), ids=SequenceIds())
    old["inputs"][0]["sensitivity"] = "pii"
    with pytest.raises(ValidationError, match="Sensitive"):
        load_capability(json.dumps(old), clock=FixedClock(), ids=SequenceIds())
    manifest = json.loads((ROOT / "tests/golden/schema-digests.v1.json").read_text())
    for name, expected in manifest.items():
        assert hashlib.sha256((ROOT / "docs/schema" / name).read_bytes()).hexdigest() == expected
```

## tests/unit/domain/test_approval.py

```python
"""Bind approval to content and reject silent migration authority; forbid external services."""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from cua.domain.capability import Capability, StaleApprovalError, capability_content_digest
from cua.domain.migrations import ArtifactDocument, v1_to_v2
from tests.unit.domain.demo_sample import demo_capability
from tests.unit.domain.samples import capability


def test_approval_binds_this_content() -> None:
    cap = demo_capability(approved=True)
    assert cap.provenance.approval is not None
    assert cap.provenance.approval.reviewed_digest == capability_content_digest(cap)
    assert Capability.model_validate_json(cap.model_dump_json()) == cap
    stale = cap.model_copy(update={"description": "Changed content"})
    with pytest.raises(StaleApprovalError, match=r"changed after approval.*re-reviewed"):
        stale.assert_current_approval()


def content_mutations(
    value: Any, path: tuple[str | int, ...] = ()
) -> list[tuple[tuple[str | int, ...], Any]]:
    """Change every scalar leaf independently; invalid shape also correctly rejects approval."""
    if isinstance(value, dict):
        return [
            item for key, child in value.items() for item in content_mutations(child, (*path, key))
        ]
    if isinstance(value, list):
        return [
            item for i, child in enumerate(value) for item in content_mutations(child, (*path, i))
        ]
    if path == ("status",) or path[:2] == ("provenance", "approval"):
        return []
    replacement = (
        not value
        if isinstance(value, bool)
        else (
            value + 1
            if isinstance(value, (int, float))
            else (value + " changed" if isinstance(value, str) else "changed")
        )
    )
    return [(path, replacement)]


def test_every_content_leaf_changes_digest_and_rejects_approved_artifact() -> None:
    cap = demo_capability(approved=True)
    source = cap.model_dump(mode="json")
    for path, replacement in content_mutations(source):
        changed = json.loads(json.dumps(source))
        parent = changed
        for part in path[:-1]:
            parent = parent[part]
        parent[path[-1]] = replacement
        with pytest.raises(ValidationError):
            Capability.model_validate(changed)


def test_exclusions_do_not_change_content_digest() -> None:
    cap = demo_capability(approved=True)
    original = capability_content_digest(cap)
    for status in ("draft", "candidate", "approved"):
        changed = Capability.model_validate({**cap.model_dump(), "status": status})
        assert capability_content_digest(changed) == original
    data = cap.model_dump(mode="json")
    data["provenance"]["approval"].update(
        actor="different-reviewer", reason="Updated review note", at="2026-02-01T00:00:00Z"
    )
    assert capability_content_digest(Capability.model_validate(data)) == original
    data["provenance"]["approval"]["reviewed_digest"] = "f" * 64
    with pytest.raises(ValidationError, match="changed after approval"):
        Capability.model_validate(data)
    draft = Capability.model_validate({**capability().model_dump(), "status": "draft"})
    assert draft.provenance.approval is None


def test_approved_migration_cannot_pass_vacuously() -> None:
    source = demo_capability(approved=True).model_dump(mode="json")
    source["schema_version"] = 1
    migrated = v1_to_v2(ArtifactDocument(payload=source))
    assert isinstance(migrated.payload, dict)
    assert migrated.payload["status"] == "approved"
    with pytest.raises(ValidationError, match=r"changed after approval.*re-reviewed"):
        Capability.model_validate(migrated.payload)
```

## tests/golden/capability.v2.json

```json
{
  "capability_id": "00000000000000000000000002",
  "description": "Hand-authored synthetic demo; assumes an authenticated session.",
  "inputs": [
    {
      "default": null,
      "description": "Synthetic member identifier.",
      "example": {
        "kind": "string",
        "value": "10001"
      },
      "json_type": "string",
      "name": "member_id",
      "required": true,
      "sensitivity": "internal",
      "validation": {
        "kind": "regex",
        "pattern": "^\\d{5}$"
      }
    }
  ],
  "name": "look_up_member_savings_balance",
  "outcomes": [
    {
      "caller_message": "No matching member exists.",
      "classification": "business",
      "code": "member_not_found",
      "detect": {
        "frame_path": null,
        "kind": "ax_node_exists",
        "max_count": null,
        "min_count": 1,
        "name_matcher": {
          "alternatives": [],
          "mode": "normalized",
          "value": "No matching member was found. This is a business outcome."
        },
        "role": "status"
      },
      "recovery": null
    },
    {
      "caller_message": "Session expired; reauthentication is required.",
      "classification": "recoverable",
      "code": "session_expired",
      "detect": {
        "frame_path": null,
        "kind": "ax_node_exists",
        "max_count": null,
        "min_count": 1,
        "name_matcher": {
          "alternatives": [],
          "mode": "normalized",
          "value": "Session expired"
        },
        "role": "status"
      },
      "recovery": {
        "kind": "reauthenticate",
        "max_attempts": 1
      }
    },
    {
      "caller_message": "The banking console could not complete the request.",
      "classification": "hard",
      "code": "app_error",
      "detect": {
        "frame_path": null,
        "kind": "ax_node_exists",
        "max_count": null,
        "min_count": 1,
        "name_matcher": {
          "alternatives": [],
          "mode": "normalized",
          "value": "System error"
        },
        "role": "heading"
      },
      "recovery": null
    }
  ],
  "outputs": [
    {
      "description": "Formatted synthetic Savings balance.",
      "extractor": {
        "attribute": "name",
        "regex": null,
        "target": {
          "frame_path": null,
          "name_matcher": {
            "alternatives": [],
            "mode": "regex",
            "value": "^\\$[0-9,.]+$"
          },
          "node_path": null,
          "nth": null,
          "role": "cell",
          "within": {
            "frame_path": null,
            "name_matcher": {
              "alternatives": [],
              "mode": "regex",
              "value": "\\bSavings\\b"
            },
            "node_path": null,
            "nth": null,
            "role": "row",
            "within": null
          }
        }
      },
      "json_type": "string",
      "name": "savings_balance",
      "nullable": false,
      "produced_by_step_id": "read-savings",
      "sensitivity": "internal"
    }
  ],
  "provenance": {
    "approval": null,
    "compiler_version": "1.0.0",
    "derived_from_run_id": "00000000000000000000000001",
    "lineage": [],
    "models_used": [
      {
        "api_flavor": "offline",
        "kind": "model",
        "model_id": "deterministic-v1",
        "provider": "fake",
        "structured_output_mode": "json_schema"
      }
    ],
    "parent_capability_id": null,
    "recorded_at": "2026-01-15T00:00:00Z",
    "recorder_version": "1.0.0",
    "tool_schema_hash": "805cecdc958c764453297810c689dd992fc67ec92f568c9162ec20e853e94696"
  },
  "schema_version": 2,
  "status": "candidate",
  "steps": [
    {
      "action": {
        "kind": "navigate",
        "url": "http://127.0.0.1:8099/t/alpha/shell"
      },
      "checkpoint": null,
      "intent": "Synthetic demo: navigate.",
      "on_outcome": [
        {
          "code": "app_error",
          "handling": {
            "kind": "fail"
          }
        },
        {
          "code": "member_not_found",
          "handling": {
            "kind": "return"
          }
        },
        {
          "code": "session_expired",
          "handling": {
            "kind": "recover"
          }
        }
      ],
      "ordinal": 1,
      "preconditions": [],
      "provenance": {
        "compiler_rule_id": null,
        "created_at": "2026-01-15T00:00:00Z",
        "decided_by": {
          "actor": "synthetic-fixture-author",
          "kind": "human"
        },
        "decision_id": null,
        "discovery_run_id": "00000000000000000000000001",
        "edits": [
          {
            "actor": "synthetic-fixture-author",
            "after_hash": "5c3374f812f8f9f8bafd73f7f850a76a16b736e52390d80e4aa7a7436b93e3e1",
            "at": "2026-01-15T00:00:00Z",
            "before_hash": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b",
            "field_path": "/steps/0",
            "reason": "Hand-authored synthetic test fixture, not verified run evidence."
          }
        ],
        "observation_hash": "6cd00c100e724bd0fca81fdc8b169d38fbe5ed49403d2d0b1be381d876a39aa0",
        "observation_id": "00000000000000000000000001",
        "prompt_hash": "ebb24e6420c6df769702a68173dbe69d464ec8516f343dd4fc3998324496f7c5",
        "prompt_template_id": "member-entry.v1",
        "rationale_digest": "7dbd08c7920f1d8706f24e8b000a217c4e9063799fcd985ec9741ed12e8e8e22",
        "tool_call_id": "fake-call-1"
      },
      "risk": "safe",
      "step_id": "navigate",
      "target": null,
      "timing": {
        "poll_interval_ms": 50,
        "retry": {
          "delay_ms": 0,
          "max_attempts": 1
        },
        "settle_strategy": "ax_stable",
        "stability_window_ms": 100,
        "timeout_ms": 2000
      }
    },
    {
      "action": {
        "kind": "type_text",
        "value_ref": {
          "kind": "param_ref",
          "name": "member_id"
        }
      },
      "checkpoint": null,
      "intent": "Synthetic demo: enter member.",
      "on_outcome": [
        {
          "code": "app_error",
          "handling": {
            "kind": "fail"
          }
        },
        {
          "code": "member_not_found",
          "handling": {
            "kind": "return"
          }
        },
        {
          "code": "session_expired",
          "handling": {
            "kind": "recover"
          }
        }
      ],
      "ordinal": 2,
      "preconditions": [],
      "provenance": {
        "compiler_rule_id": null,
        "created_at": "2026-01-15T00:00:00Z",
        "decided_by": {
          "actor": "synthetic-fixture-author",
          "kind": "human"
        },
        "decision_id": null,
        "discovery_run_id": "00000000000000000000000001",
        "edits": [
          {
            "actor": "synthetic-fixture-author",
            "after_hash": "bb7e012914eca0abb0d04b2707c0cb369dbee39ac6c2b96b82f253e1d89aef0b",
            "at": "2026-01-15T00:00:00Z",
            "before_hash": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b",
            "field_path": "/steps/1",
            "reason": "Hand-authored synthetic test fixture, not verified run evidence."
          }
        ],
        "observation_hash": "6cd00c100e724bd0fca81fdc8b169d38fbe5ed49403d2d0b1be381d876a39aa0",
        "observation_id": "00000000000000000000000001",
        "prompt_hash": "ebb24e6420c6df769702a68173dbe69d464ec8516f343dd4fc3998324496f7c5",
        "prompt_template_id": "member-entry.v1",
        "rationale_digest": "7dbd08c7920f1d8706f24e8b000a217c4e9063799fcd985ec9741ed12e8e8e22",
        "tool_call_id": "fake-call-1"
      },
      "risk": "reversible",
      "step_id": "enter-member",
      "target": {
        "candidates": [
          {
            "confidence": 1.0,
            "evidence_ref": {
              "content_hash": "8b8111443c1cf9ff3b0cfbb42e9c35617c1ecc5795e6c4ace3f56db24f4ab4e9",
              "evidence_id": "00000000000000000000000001",
              "media_type": "application/json"
            },
            "frame_path": [
              "Content",
              "Member workspace"
            ],
            "observed_at": "2026-01-15T00:00:00Z",
            "rationale": "Unique named textbox in the recorded member workspace.",
            "source": "ax_tree",
            "strategy": "ax_role_name",
            "uniqueness_at_record": 1,
            "value": {
              "name_matcher": {
                "alternatives": [],
                "mode": "normalized",
                "value": "Member ID"
              },
              "role": "textbox"
            }
          }
        ],
        "match_policy": "require_unique",
        "scope_hints": {
          "ancestor": null,
          "description": ""
        }
      },
      "timing": {
        "poll_interval_ms": 50,
        "retry": {
          "delay_ms": 0,
          "max_attempts": 1
        },
        "settle_strategy": "ax_stable",
        "stability_window_ms": 100,
        "timeout_ms": 2000
      }
    },
    {
      "action": {
        "kind": "click"
      },
      "checkpoint": null,
      "intent": "Synthetic demo: submit search.",
      "on_outcome": [
        {
          "code": "app_error",
          "handling": {
            "kind": "fail"
          }
        },
        {
          "code": "member_not_found",
          "handling": {
            "kind": "return"
          }
        },
        {
          "code": "session_expired",
          "handling": {
            "kind": "recover"
          }
        }
      ],
      "ordinal": 3,
      "preconditions": [],
      "provenance": {
        "compiler_rule_id": null,
        "created_at": "2026-01-15T00:00:00Z",
        "decided_by": {
          "actor": "synthetic-fixture-author",
          "kind": "human"
        },
        "decision_id": null,
        "discovery_run_id": "00000000000000000000000001",
        "edits": [
          {
            "actor": "synthetic-fixture-author",
            "after_hash": "47ca4466ddc2de4d4a5a4798bb2d404b41e76b8e5c11d7e3ad98f90f50abdb07",
            "at": "2026-01-15T00:00:00Z",
            "before_hash": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b",
            "field_path": "/steps/2",
            "reason": "Hand-authored synthetic test fixture, not verified run evidence."
          }
        ],
        "observation_hash": "6cd00c100e724bd0fca81fdc8b169d38fbe5ed49403d2d0b1be381d876a39aa0",
        "observation_id": "00000000000000000000000001",
        "prompt_hash": "ebb24e6420c6df769702a68173dbe69d464ec8516f343dd4fc3998324496f7c5",
        "prompt_template_id": "member-entry.v1",
        "rationale_digest": "7dbd08c7920f1d8706f24e8b000a217c4e9063799fcd985ec9741ed12e8e8e22",
        "tool_call_id": "fake-call-1"
      },
      "risk": "safe",
      "step_id": "submit-search",
      "target": {
        "candidates": [
          {
            "confidence": 1.0,
            "evidence_ref": {
              "content_hash": "8b8111443c1cf9ff3b0cfbb42e9c35617c1ecc5795e6c4ace3f56db24f4ab4e9",
              "evidence_id": "00000000000000000000000001",
              "media_type": "application/json"
            },
            "frame_path": [
              "Content",
              "Member workspace"
            ],
            "observed_at": "2026-01-15T00:00:00Z",
            "rationale": "Synthetic hand-authored fixture; verify against live evidence later.",
            "source": "ax_tree",
            "strategy": "ax_role_name",
            "uniqueness_at_record": 1,
            "value": {
              "name_matcher": {
                "alternatives": [],
                "mode": "normalized",
                "value": "Search members"
              },
              "role": "button"
            }
          },
          {
            "confidence": 1.0,
            "evidence_ref": {
              "content_hash": "8b8111443c1cf9ff3b0cfbb42e9c35617c1ecc5795e6c4ace3f56db24f4ab4e9",
              "evidence_id": "00000000000000000000000001",
              "media_type": "application/json"
            },
            "frame_path": [
              "Content",
              "Member workspace"
            ],
            "observed_at": "2026-01-15T00:00:00Z",
            "rationale": "Synthetic hand-authored fixture; verify against live evidence later.",
            "source": "ax_tree",
            "strategy": "visible_text",
            "uniqueness_at_record": 1,
            "value": {
              "matcher": {
                "alternatives": [],
                "mode": "normalized",
                "value": "Search members"
              }
            }
          },
          {
            "confidence": 1.0,
            "evidence_ref": {
              "content_hash": "8b8111443c1cf9ff3b0cfbb42e9c35617c1ecc5795e6c4ace3f56db24f4ab4e9",
              "evidence_id": "00000000000000000000000001",
              "media_type": "application/json"
            },
            "frame_path": [
              "Content",
              "Member workspace"
            ],
            "observed_at": "2026-01-15T00:00:00Z",
            "rationale": "Synthetic hand-authored fixture; verify against live evidence later.",
            "source": "ax_tree",
            "strategy": "coordinates",
            "uniqueness_at_record": 1,
            "value": {
              "x": 160.0,
              "y": 80.0
            }
          }
        ],
        "match_policy": "require_unique",
        "scope_hints": {
          "ancestor": null,
          "description": ""
        }
      },
      "timing": {
        "poll_interval_ms": 50,
        "retry": {
          "delay_ms": 0,
          "max_attempts": 1
        },
        "settle_strategy": "ax_stable",
        "stability_window_ms": 100,
        "timeout_ms": 2000
      }
    },
    {
      "action": {
        "kind": "click"
      },
      "checkpoint": {
        "frame_path": null,
        "kind": "ax_node_exists",
        "max_count": null,
        "min_count": 1,
        "name_matcher": {
          "alternatives": [],
          "mode": "normalized",
          "value": "Member detail"
        },
        "role": "heading"
      },
      "intent": "Synthetic demo: member detail.",
      "on_outcome": [
        {
          "code": "app_error",
          "handling": {
            "kind": "fail"
          }
        },
        {
          "code": "member_not_found",
          "handling": {
            "kind": "return"
          }
        },
        {
          "code": "session_expired",
          "handling": {
            "kind": "recover"
          }
        }
      ],
      "ordinal": 4,
      "preconditions": [],
      "provenance": {
        "compiler_rule_id": null,
        "created_at": "2026-01-15T00:00:00Z",
        "decided_by": {
          "actor": "synthetic-fixture-author",
          "kind": "human"
        },
        "decision_id": null,
        "discovery_run_id": "00000000000000000000000001",
        "edits": [
          {
            "actor": "synthetic-fixture-author",
            "after_hash": "51afc7ef4d1d9dd4e1cf0ea2496280e80cbcf313ba83b76ddc2300fbc8b9fc28",
            "at": "2026-01-15T00:00:00Z",
            "before_hash": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b",
            "field_path": "/steps/3",
            "reason": "Hand-authored synthetic test fixture, not verified run evidence."
          }
        ],
        "observation_hash": "6cd00c100e724bd0fca81fdc8b169d38fbe5ed49403d2d0b1be381d876a39aa0",
        "observation_id": "00000000000000000000000001",
        "prompt_hash": "ebb24e6420c6df769702a68173dbe69d464ec8516f343dd4fc3998324496f7c5",
        "prompt_template_id": "member-entry.v1",
        "rationale_digest": "7dbd08c7920f1d8706f24e8b000a217c4e9063799fcd985ec9741ed12e8e8e22",
        "tool_call_id": "fake-call-1"
      },
      "risk": "safe",
      "step_id": "member-detail",
      "target": {
        "candidates": [
          {
            "confidence": 1.0,
            "evidence_ref": {
              "content_hash": "8b8111443c1cf9ff3b0cfbb42e9c35617c1ecc5795e6c4ace3f56db24f4ab4e9",
              "evidence_id": "00000000000000000000000001",
              "media_type": "application/json"
            },
            "frame_path": [
              "Content",
              "Member workspace"
            ],
            "observed_at": "2026-01-15T00:00:00Z",
            "rationale": "Synthetic hand-authored fixture; verify against live evidence later.",
            "source": "ax_tree",
            "strategy": "ax_role_name",
            "uniqueness_at_record": 1,
            "value": {
              "name_matcher": {
                "alternatives": [],
                "mode": "normalized",
                "value": "View member 10001"
              },
              "role": "link"
            }
          }
        ],
        "match_policy": "require_unique",
        "scope_hints": {
          "ancestor": null,
          "description": ""
        }
      },
      "timing": {
        "poll_interval_ms": 50,
        "retry": {
          "delay_ms": 0,
          "max_attempts": 1
        },
        "settle_strategy": "ax_stable",
        "stability_window_ms": 100,
        "timeout_ms": 2000
      }
    },
    {
      "action": {
        "attribute": "name",
        "kind": "read_value",
        "output_name": "savings_balance"
      },
      "checkpoint": null,
      "intent": "Synthetic demo: read savings.",
      "on_outcome": [
        {
          "code": "app_error",
          "handling": {
            "kind": "fail"
          }
        },
        {
          "code": "member_not_found",
          "handling": {
            "kind": "return"
          }
        },
        {
          "code": "session_expired",
          "handling": {
            "kind": "recover"
          }
        }
      ],
      "ordinal": 5,
      "preconditions": [],
      "provenance": {
        "compiler_rule_id": null,
        "created_at": "2026-01-15T00:00:00Z",
        "decided_by": {
          "actor": "synthetic-fixture-author",
          "kind": "human"
        },
        "decision_id": null,
        "discovery_run_id": "00000000000000000000000001",
        "edits": [
          {
            "actor": "synthetic-fixture-author",
            "after_hash": "d58d2af6da579dfc901acbc28a7b165291ddda30f273660447033202a13afef7",
            "at": "2026-01-15T00:00:00Z",
            "before_hash": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b",
            "field_path": "/steps/4",
            "reason": "Hand-authored synthetic test fixture, not verified run evidence."
          }
        ],
        "observation_hash": "6cd00c100e724bd0fca81fdc8b169d38fbe5ed49403d2d0b1be381d876a39aa0",
        "observation_id": "00000000000000000000000001",
        "prompt_hash": "ebb24e6420c6df769702a68173dbe69d464ec8516f343dd4fc3998324496f7c5",
        "prompt_template_id": "member-entry.v1",
        "rationale_digest": "7dbd08c7920f1d8706f24e8b000a217c4e9063799fcd985ec9741ed12e8e8e22",
        "tool_call_id": "fake-call-1"
      },
      "risk": "safe",
      "step_id": "read-savings",
      "target": {
        "candidates": [
          {
            "confidence": 1.0,
            "evidence_ref": {
              "content_hash": "8b8111443c1cf9ff3b0cfbb42e9c35617c1ecc5795e6c4ace3f56db24f4ab4e9",
              "evidence_id": "00000000000000000000000001",
              "media_type": "application/json"
            },
            "frame_path": [
              "Content",
              "Member workspace"
            ],
            "observed_at": "2026-01-15T00:00:00Z",
            "rationale": "Synthetic hand-authored fixture; verify against live evidence later.",
            "source": "ax_tree",
            "strategy": "ax_role_name_scoped",
            "uniqueness_at_record": 1,
            "value": {
              "ancestor": {
                "frame_path": null,
                "name_matcher": {
                  "alternatives": [],
                  "mode": "regex",
                  "value": "\\bSavings\\b"
                },
                "node_path": null,
                "nth": null,
                "role": "row",
                "within": null
              },
              "name_matcher": {
                "alternatives": [],
                "mode": "regex",
                "value": "^\\$[0-9,.]+$"
              },
              "role": "cell"
            }
          }
        ],
        "match_policy": "require_unique",
        "scope_hints": {
          "ancestor": null,
          "description": ""
        }
      },
      "timing": {
        "poll_interval_ms": 50,
        "retry": {
          "delay_ms": 0,
          "max_attempts": 1
        },
        "settle_strategy": "ax_stable",
        "stability_window_ms": 100,
        "timeout_ms": 2000
      }
    }
  ],
  "target": {
    "allowlist_ref": "local-demo.v1",
    "app_id": "cua-synthetic-bank",
    "entry_point": "http://127.0.0.1:8099/t/alpha/",
    "recorded_fingerprint": {
      "app_id": "cua-synthetic-bank",
      "app_version": "0.1.0",
      "config_hash": "adc957f5621d18bb12b0d08c8865d8d75085ee1eb32a74a9c77e65415e508160",
      "observed_at": "2026-01-15T00:00:00Z",
      "tenant_id": "alpha",
      "ui_revision": "legacy-frames-1"
    },
    "surface_kind": "web",
    "tenant_id": "alpha"
  },
  "version": "1.0.0"
}
```

## tests/golden/capability.approved.v2.json

```json
{
  "capability_id": "00000000000000000000000002",
  "description": "Hand-authored synthetic demo; assumes an authenticated session.",
  "inputs": [
    {
      "default": null,
      "description": "Synthetic member identifier.",
      "example": {
        "kind": "string",
        "value": "10001"
      },
      "json_type": "string",
      "name": "member_id",
      "required": true,
      "sensitivity": "internal",
      "validation": {
        "kind": "regex",
        "pattern": "^\\d{5}$"
      }
    }
  ],
  "name": "look_up_member_savings_balance",
  "outcomes": [
    {
      "caller_message": "No matching member exists.",
      "classification": "business",
      "code": "member_not_found",
      "detect": {
        "frame_path": null,
        "kind": "ax_node_exists",
        "max_count": null,
        "min_count": 1,
        "name_matcher": {
          "alternatives": [],
          "mode": "normalized",
          "value": "No matching member was found. This is a business outcome."
        },
        "role": "status"
      },
      "recovery": null
    },
    {
      "caller_message": "Session expired; reauthentication is required.",
      "classification": "recoverable",
      "code": "session_expired",
      "detect": {
        "frame_path": null,
        "kind": "ax_node_exists",
        "max_count": null,
        "min_count": 1,
        "name_matcher": {
          "alternatives": [],
          "mode": "normalized",
          "value": "Session expired"
        },
        "role": "status"
      },
      "recovery": {
        "kind": "reauthenticate",
        "max_attempts": 1
      }
    },
    {
      "caller_message": "The banking console could not complete the request.",
      "classification": "hard",
      "code": "app_error",
      "detect": {
        "frame_path": null,
        "kind": "ax_node_exists",
        "max_count": null,
        "min_count": 1,
        "name_matcher": {
          "alternatives": [],
          "mode": "normalized",
          "value": "System error"
        },
        "role": "heading"
      },
      "recovery": null
    }
  ],
  "outputs": [
    {
      "description": "Formatted synthetic Savings balance.",
      "extractor": {
        "attribute": "name",
        "regex": null,
        "target": {
          "frame_path": null,
          "name_matcher": {
            "alternatives": [],
            "mode": "regex",
            "value": "^\\$[0-9,.]+$"
          },
          "node_path": null,
          "nth": null,
          "role": "cell",
          "within": {
            "frame_path": null,
            "name_matcher": {
              "alternatives": [],
              "mode": "regex",
              "value": "\\bSavings\\b"
            },
            "node_path": null,
            "nth": null,
            "role": "row",
            "within": null
          }
        }
      },
      "json_type": "string",
      "name": "savings_balance",
      "nullable": false,
      "produced_by_step_id": "read-savings",
      "sensitivity": "internal"
    }
  ],
  "provenance": {
    "approval": {
      "actor": "synthetic-reviewer",
      "at": "2026-01-15T00:00:00Z",
      "reason": "Synthetic approval fixture; not deployment authority.",
      "reviewed_digest": "d3f7eafe61a8726d53fa1f43c66bc626e0dd1d13c4712c45549438cf3bd6b714"
    },
    "compiler_version": "1.0.0",
    "derived_from_run_id": "00000000000000000000000001",
    "lineage": [],
    "models_used": [
      {
        "api_flavor": "offline",
        "kind": "model",
        "model_id": "deterministic-v1",
        "provider": "fake",
        "structured_output_mode": "json_schema"
      }
    ],
    "parent_capability_id": null,
    "recorded_at": "2026-01-15T00:00:00Z",
    "recorder_version": "1.0.0",
    "tool_schema_hash": "805cecdc958c764453297810c689dd992fc67ec92f568c9162ec20e853e94696"
  },
  "schema_version": 2,
  "status": "approved",
  "steps": [
    {
      "action": {
        "kind": "navigate",
        "url": "http://127.0.0.1:8099/t/alpha/shell"
      },
      "checkpoint": null,
      "intent": "Synthetic demo: navigate.",
      "on_outcome": [
        {
          "code": "app_error",
          "handling": {
            "kind": "fail"
          }
        },
        {
          "code": "member_not_found",
          "handling": {
            "kind": "return"
          }
        },
        {
          "code": "session_expired",
          "handling": {
            "kind": "recover"
          }
        }
      ],
      "ordinal": 1,
      "preconditions": [],
      "provenance": {
        "compiler_rule_id": null,
        "created_at": "2026-01-15T00:00:00Z",
        "decided_by": {
          "actor": "synthetic-fixture-author",
          "kind": "human"
        },
        "decision_id": null,
        "discovery_run_id": "00000000000000000000000001",
        "edits": [
          {
            "actor": "synthetic-fixture-author",
            "after_hash": "5c3374f812f8f9f8bafd73f7f850a76a16b736e52390d80e4aa7a7436b93e3e1",
            "at": "2026-01-15T00:00:00Z",
            "before_hash": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b",
            "field_path": "/steps/0",
            "reason": "Hand-authored synthetic test fixture, not verified run evidence."
          }
        ],
        "observation_hash": "6cd00c100e724bd0fca81fdc8b169d38fbe5ed49403d2d0b1be381d876a39aa0",
        "observation_id": "00000000000000000000000001",
        "prompt_hash": "ebb24e6420c6df769702a68173dbe69d464ec8516f343dd4fc3998324496f7c5",
        "prompt_template_id": "member-entry.v1",
        "rationale_digest": "7dbd08c7920f1d8706f24e8b000a217c4e9063799fcd985ec9741ed12e8e8e22",
        "tool_call_id": "fake-call-1"
      },
      "risk": "safe",
      "step_id": "navigate",
      "target": null,
      "timing": {
        "poll_interval_ms": 50,
        "retry": {
          "delay_ms": 0,
          "max_attempts": 1
        },
        "settle_strategy": "ax_stable",
        "stability_window_ms": 100,
        "timeout_ms": 2000
      }
    },
    {
      "action": {
        "kind": "type_text",
        "value_ref": {
          "kind": "param_ref",
          "name": "member_id"
        }
      },
      "checkpoint": null,
      "intent": "Synthetic demo: enter member.",
      "on_outcome": [
        {
          "code": "app_error",
          "handling": {
            "kind": "fail"
          }
        },
        {
          "code": "member_not_found",
          "handling": {
            "kind": "return"
          }
        },
        {
          "code": "session_expired",
          "handling": {
            "kind": "recover"
          }
        }
      ],
      "ordinal": 2,
      "preconditions": [],
      "provenance": {
        "compiler_rule_id": null,
        "created_at": "2026-01-15T00:00:00Z",
        "decided_by": {
          "actor": "synthetic-fixture-author",
          "kind": "human"
        },
        "decision_id": null,
        "discovery_run_id": "00000000000000000000000001",
        "edits": [
          {
            "actor": "synthetic-fixture-author",
            "after_hash": "bb7e012914eca0abb0d04b2707c0cb369dbee39ac6c2b96b82f253e1d89aef0b",
            "at": "2026-01-15T00:00:00Z",
            "before_hash": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b",
            "field_path": "/steps/1",
            "reason": "Hand-authored synthetic test fixture, not verified run evidence."
          }
        ],
        "observation_hash": "6cd00c100e724bd0fca81fdc8b169d38fbe5ed49403d2d0b1be381d876a39aa0",
        "observation_id": "00000000000000000000000001",
        "prompt_hash": "ebb24e6420c6df769702a68173dbe69d464ec8516f343dd4fc3998324496f7c5",
        "prompt_template_id": "member-entry.v1",
        "rationale_digest": "7dbd08c7920f1d8706f24e8b000a217c4e9063799fcd985ec9741ed12e8e8e22",
        "tool_call_id": "fake-call-1"
      },
      "risk": "reversible",
      "step_id": "enter-member",
      "target": {
        "candidates": [
          {
            "confidence": 1.0,
            "evidence_ref": {
              "content_hash": "8b8111443c1cf9ff3b0cfbb42e9c35617c1ecc5795e6c4ace3f56db24f4ab4e9",
              "evidence_id": "00000000000000000000000001",
              "media_type": "application/json"
            },
            "frame_path": [
              "Content",
              "Member workspace"
            ],
            "observed_at": "2026-01-15T00:00:00Z",
            "rationale": "Unique named textbox in the recorded member workspace.",
            "source": "ax_tree",
            "strategy": "ax_role_name",
            "uniqueness_at_record": 1,
            "value": {
              "name_matcher": {
                "alternatives": [],
                "mode": "normalized",
                "value": "Member ID"
              },
              "role": "textbox"
            }
          }
        ],
        "match_policy": "require_unique",
        "scope_hints": {
          "ancestor": null,
          "description": ""
        }
      },
      "timing": {
        "poll_interval_ms": 50,
        "retry": {
          "delay_ms": 0,
          "max_attempts": 1
        },
        "settle_strategy": "ax_stable",
        "stability_window_ms": 100,
        "timeout_ms": 2000
      }
    },
    {
      "action": {
        "kind": "click"
      },
      "checkpoint": null,
      "intent": "Synthetic demo: submit search.",
      "on_outcome": [
        {
          "code": "app_error",
          "handling": {
            "kind": "fail"
          }
        },
        {
          "code": "member_not_found",
          "handling": {
            "kind": "return"
          }
        },
        {
          "code": "session_expired",
          "handling": {
            "kind": "recover"
          }
        }
      ],
      "ordinal": 3,
      "preconditions": [],
      "provenance": {
        "compiler_rule_id": null,
        "created_at": "2026-01-15T00:00:00Z",
        "decided_by": {
          "actor": "synthetic-fixture-author",
          "kind": "human"
        },
        "decision_id": null,
        "discovery_run_id": "00000000000000000000000001",
        "edits": [
          {
            "actor": "synthetic-fixture-author",
            "after_hash": "47ca4466ddc2de4d4a5a4798bb2d404b41e76b8e5c11d7e3ad98f90f50abdb07",
            "at": "2026-01-15T00:00:00Z",
            "before_hash": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b",
            "field_path": "/steps/2",
            "reason": "Hand-authored synthetic test fixture, not verified run evidence."
          }
        ],
        "observation_hash": "6cd00c100e724bd0fca81fdc8b169d38fbe5ed49403d2d0b1be381d876a39aa0",
        "observation_id": "00000000000000000000000001",
        "prompt_hash": "ebb24e6420c6df769702a68173dbe69d464ec8516f343dd4fc3998324496f7c5",
        "prompt_template_id": "member-entry.v1",
        "rationale_digest": "7dbd08c7920f1d8706f24e8b000a217c4e9063799fcd985ec9741ed12e8e8e22",
        "tool_call_id": "fake-call-1"
      },
      "risk": "safe",
      "step_id": "submit-search",
      "target": {
        "candidates": [
          {
            "confidence": 1.0,
            "evidence_ref": {
              "content_hash": "8b8111443c1cf9ff3b0cfbb42e9c35617c1ecc5795e6c4ace3f56db24f4ab4e9",
              "evidence_id": "00000000000000000000000001",
              "media_type": "application/json"
            },
            "frame_path": [
              "Content",
              "Member workspace"
            ],
            "observed_at": "2026-01-15T00:00:00Z",
            "rationale": "Synthetic hand-authored fixture; verify against live evidence later.",
            "source": "ax_tree",
            "strategy": "ax_role_name",
            "uniqueness_at_record": 1,
            "value": {
              "name_matcher": {
                "alternatives": [],
                "mode": "normalized",
                "value": "Search members"
              },
              "role": "button"
            }
          },
          {
            "confidence": 1.0,
            "evidence_ref": {
              "content_hash": "8b8111443c1cf9ff3b0cfbb42e9c35617c1ecc5795e6c4ace3f56db24f4ab4e9",
              "evidence_id": "00000000000000000000000001",
              "media_type": "application/json"
            },
            "frame_path": [
              "Content",
              "Member workspace"
            ],
            "observed_at": "2026-01-15T00:00:00Z",
            "rationale": "Synthetic hand-authored fixture; verify against live evidence later.",
            "source": "ax_tree",
            "strategy": "visible_text",
            "uniqueness_at_record": 1,
            "value": {
              "matcher": {
                "alternatives": [],
                "mode": "normalized",
                "value": "Search members"
              }
            }
          },
          {
            "confidence": 1.0,
            "evidence_ref": {
              "content_hash": "8b8111443c1cf9ff3b0cfbb42e9c35617c1ecc5795e6c4ace3f56db24f4ab4e9",
              "evidence_id": "00000000000000000000000001",
              "media_type": "application/json"
            },
            "frame_path": [
              "Content",
              "Member workspace"
            ],
            "observed_at": "2026-01-15T00:00:00Z",
            "rationale": "Synthetic hand-authored fixture; verify against live evidence later.",
            "source": "ax_tree",
            "strategy": "coordinates",
            "uniqueness_at_record": 1,
            "value": {
              "x": 160.0,
              "y": 80.0
            }
          }
        ],
        "match_policy": "require_unique",
        "scope_hints": {
          "ancestor": null,
          "description": ""
        }
      },
      "timing": {
        "poll_interval_ms": 50,
        "retry": {
          "delay_ms": 0,
          "max_attempts": 1
        },
        "settle_strategy": "ax_stable",
        "stability_window_ms": 100,
        "timeout_ms": 2000
      }
    },
    {
      "action": {
        "kind": "click"
      },
      "checkpoint": {
        "frame_path": null,
        "kind": "ax_node_exists",
        "max_count": null,
        "min_count": 1,
        "name_matcher": {
          "alternatives": [],
          "mode": "normalized",
          "value": "Member detail"
        },
        "role": "heading"
      },
      "intent": "Synthetic demo: member detail.",
      "on_outcome": [
        {
          "code": "app_error",
          "handling": {
            "kind": "fail"
          }
        },
        {
          "code": "member_not_found",
          "handling": {
            "kind": "return"
          }
        },
        {
          "code": "session_expired",
          "handling": {
            "kind": "recover"
          }
        }
      ],
      "ordinal": 4,
      "preconditions": [],
      "provenance": {
        "compiler_rule_id": null,
        "created_at": "2026-01-15T00:00:00Z",
        "decided_by": {
          "actor": "synthetic-fixture-author",
          "kind": "human"
        },
        "decision_id": null,
        "discovery_run_id": "00000000000000000000000001",
        "edits": [
          {
            "actor": "synthetic-fixture-author",
            "after_hash": "51afc7ef4d1d9dd4e1cf0ea2496280e80cbcf313ba83b76ddc2300fbc8b9fc28",
            "at": "2026-01-15T00:00:00Z",
            "before_hash": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b",
            "field_path": "/steps/3",
            "reason": "Hand-authored synthetic test fixture, not verified run evidence."
          }
        ],
        "observation_hash": "6cd00c100e724bd0fca81fdc8b169d38fbe5ed49403d2d0b1be381d876a39aa0",
        "observation_id": "00000000000000000000000001",
        "prompt_hash": "ebb24e6420c6df769702a68173dbe69d464ec8516f343dd4fc3998324496f7c5",
        "prompt_template_id": "member-entry.v1",
        "rationale_digest": "7dbd08c7920f1d8706f24e8b000a217c4e9063799fcd985ec9741ed12e8e8e22",
        "tool_call_id": "fake-call-1"
      },
      "risk": "safe",
      "step_id": "member-detail",
      "target": {
        "candidates": [
          {
            "confidence": 1.0,
            "evidence_ref": {
              "content_hash": "8b8111443c1cf9ff3b0cfbb42e9c35617c1ecc5795e6c4ace3f56db24f4ab4e9",
              "evidence_id": "00000000000000000000000001",
              "media_type": "application/json"
            },
            "frame_path": [
              "Content",
              "Member workspace"
            ],
            "observed_at": "2026-01-15T00:00:00Z",
            "rationale": "Synthetic hand-authored fixture; verify against live evidence later.",
            "source": "ax_tree",
            "strategy": "ax_role_name",
            "uniqueness_at_record": 1,
            "value": {
              "name_matcher": {
                "alternatives": [],
                "mode": "normalized",
                "value": "View member 10001"
              },
              "role": "link"
            }
          }
        ],
        "match_policy": "require_unique",
        "scope_hints": {
          "ancestor": null,
          "description": ""
        }
      },
      "timing": {
        "poll_interval_ms": 50,
        "retry": {
          "delay_ms": 0,
          "max_attempts": 1
        },
        "settle_strategy": "ax_stable",
        "stability_window_ms": 100,
        "timeout_ms": 2000
      }
    },
    {
      "action": {
        "attribute": "name",
        "kind": "read_value",
        "output_name": "savings_balance"
      },
      "checkpoint": null,
      "intent": "Synthetic demo: read savings.",
      "on_outcome": [
        {
          "code": "app_error",
          "handling": {
            "kind": "fail"
          }
        },
        {
          "code": "member_not_found",
          "handling": {
            "kind": "return"
          }
        },
        {
          "code": "session_expired",
          "handling": {
            "kind": "recover"
          }
        }
      ],
      "ordinal": 5,
      "preconditions": [],
      "provenance": {
        "compiler_rule_id": null,
        "created_at": "2026-01-15T00:00:00Z",
        "decided_by": {
          "actor": "synthetic-fixture-author",
          "kind": "human"
        },
        "decision_id": null,
        "discovery_run_id": "00000000000000000000000001",
        "edits": [
          {
            "actor": "synthetic-fixture-author",
            "after_hash": "d58d2af6da579dfc901acbc28a7b165291ddda30f273660447033202a13afef7",
            "at": "2026-01-15T00:00:00Z",
            "before_hash": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b",
            "field_path": "/steps/4",
            "reason": "Hand-authored synthetic test fixture, not verified run evidence."
          }
        ],
        "observation_hash": "6cd00c100e724bd0fca81fdc8b169d38fbe5ed49403d2d0b1be381d876a39aa0",
        "observation_id": "00000000000000000000000001",
        "prompt_hash": "ebb24e6420c6df769702a68173dbe69d464ec8516f343dd4fc3998324496f7c5",
        "prompt_template_id": "member-entry.v1",
        "rationale_digest": "7dbd08c7920f1d8706f24e8b000a217c4e9063799fcd985ec9741ed12e8e8e22",
        "tool_call_id": "fake-call-1"
      },
      "risk": "safe",
      "step_id": "read-savings",
      "target": {
        "candidates": [
          {
            "confidence": 1.0,
            "evidence_ref": {
              "content_hash": "8b8111443c1cf9ff3b0cfbb42e9c35617c1ecc5795e6c4ace3f56db24f4ab4e9",
              "evidence_id": "00000000000000000000000001",
              "media_type": "application/json"
            },
            "frame_path": [
              "Content",
              "Member workspace"
            ],
            "observed_at": "2026-01-15T00:00:00Z",
            "rationale": "Synthetic hand-authored fixture; verify against live evidence later.",
            "source": "ax_tree",
            "strategy": "ax_role_name_scoped",
            "uniqueness_at_record": 1,
            "value": {
              "ancestor": {
                "frame_path": null,
                "name_matcher": {
                  "alternatives": [],
                  "mode": "regex",
                  "value": "\\bSavings\\b"
                },
                "node_path": null,
                "nth": null,
                "role": "row",
                "within": null
              },
              "name_matcher": {
                "alternatives": [],
                "mode": "regex",
                "value": "^\\$[0-9,.]+$"
              },
              "role": "cell"
            }
          }
        ],
        "match_policy": "require_unique",
        "scope_hints": {
          "ancestor": null,
          "description": ""
        }
      },
      "timing": {
        "poll_interval_ms": 50,
        "retry": {
          "delay_ms": 0,
          "max_attempts": 1
        },
        "settle_strategy": "ax_stable",
        "stability_window_ms": 100,
        "timeout_ms": 2000
      }
    }
  ],
  "target": {
    "allowlist_ref": "local-demo.v1",
    "app_id": "cua-synthetic-bank",
    "entry_point": "http://127.0.0.1:8099/t/alpha/",
    "recorded_fingerprint": {
      "app_id": "cua-synthetic-bank",
      "app_version": "0.1.0",
      "config_hash": "adc957f5621d18bb12b0d08c8865d8d75085ee1eb32a74a9c77e65415e508160",
      "observed_at": "2026-01-15T00:00:00Z",
      "tenant_id": "alpha",
      "ui_revision": "legacy-frames-1"
    },
    "surface_kind": "web",
    "tenant_id": "alpha"
  },
  "version": "1.0.0"
}
```

## docs/schema/part-a-golden-update.txt

```text
uv run --locked pytest --update-goldens
============================= test session starts =============================
platform win32 -- Python 3.12.14, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\interface.ai
configfile: pyproject.toml
testpaths: tests
plugins: anyio-4.15.1, Faker-40.39.0, hypothesis-6.168.0, langsmith-0.12.6, asyncio-1.4.0, cov-7.1.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 167 items

tests\browser\test_target_app_smoke.py ss                                [  1%]
tests\integration\target_app\test_app.py ............................... [ 19%]
.............                                                            [ 27%]
tests\unit\domain\test_approval.py ....                                  [ 29%]
tests\unit\domain\test_contracts.py .................................... [ 51%]
                                                                         [ 51%]
tests\unit\domain\test_invariants.py ................................... [ 72%]
...........                                                              [ 79%]
tests\unit\domain\test_relative_scope.py ........
GOLDEN REWRITTEN: C:\interface.ai\tests\golden\capability.v2.json | 21446 -> 21446 bytes (delta +0)
.
tests\unit\domain\test_relative_scope.py 
GOLDEN REWRITTEN: C:\interface.ai\tests\golden\capability.approved.v2.json | 21696 -> 21686 bytes (delta -10)
.
tests\unit\domain\test_relative_scope.py .                               [ 85%]
tests\unit\domain\test_schemas.py ...........
GOLDEN REWRITTEN: C:\interface.ai\tests\golden\capability.v2.json | 21446 -> 21446 bytes (delta +0)
.
tests\unit\domain\test_schemas.py 
GOLDEN REWRITTEN: C:\interface.ai\tests\golden\schema-digests.v2.json | 566 -> 566 bytes (delta +0)
GOLDEN REWRITTEN: C:\interface.ai\docs\schema\capability.v2.json | 36514 -> 36516 bytes (delta +2)
GOLDEN REWRITTEN: C:\interface.ai\docs\schema\action.v2.json | 11070 -> 11070 bytes (delta +0)
GOLDEN REWRITTEN: C:\interface.ai\docs\schema\decision-result.v2.json | 12750 -> 12750 bytes (delta +0)
.
tests\unit\domain\test_schemas.py ..                                     [ 94%]
tests\unit\test_scaffold.py .                                            [ 95%]
tests\unit\test_target_app_boundary.py ..                                [ 96%]
tests\unit\test_target_app_cli.py ......                                 [100%]

=============================== tests coverage ================================
______________ coverage: platform win32, python 3.12.14-final-0 _______________

Name                                Stmts   Miss Branch BrPart  Cover   Missing
-------------------------------------------------------------------------------
src\cua\__init__.py                     1      0      0      0   100%
src\cua\catalog\__init__.py             0      0      0      0   100%
src\cua\discovery\__init__.py           0      0      0      0   100%
src\cua\domain\__init__.py              0      0      0      0   100%
src\cua\domain\actions.py              34      0      0      0   100%
src\cua\domain\capability.py          185      0     74      1    99%   269->253
src\cua\domain\common.py               43      0      0      0   100%
src\cua\domain\locators.py             72      0      6      0   100%
src\cua\domain\migrations.py           64      1     22      2    97%   52, 69->71
src\cua\domain\models.py               51      0      4      0   100%
src\cua\domain\names.py                38      0     16      1    98%   51->exit
src\cua\domain\observation.py          61      0      2      0   100%
src\cua\domain\ports.py                31      0      0      0   100%
src\cua\domain\predicates.py          130      0     24      0   100%
src\cua\domain\provenance.py           66      0     12      0   100%
src\cua\domain\results.py              45      0      0      0   100%
src\cua\domain\schemas.py              56      1     30      2    97%   39->exit, 51
src\cua\domain\steps.py                53      0      6      0   100%
src\cua\escalation\__init__.py          0      0      0      0   100%
src\cua\observability\__init__.py       0      0      0      0   100%
src\cua\policy\__init__.py              0      0      0      0   100%
src\cua\replay\__init__.py              0      0      0      0   100%
src\cua\surface\__init__.py             0      0      0      0   100%
src\cua\target_app\__init__.py          0      0      0      0   100%
src\cua\target_app\__main__.py          8      1      2      1    80%   23
src\cua\target_app\app.py             125      3     40      3    96%   65, 152, 183
src\cua\target_app\config.py           30      0      0      0   100%
src\cua\target_app\faults.py           29      1      4      1    94%   67
src\cua\target_app\models.py           92      0     10      0   100%
src\cua\target_app\state.py            28      0      6      1    97%   58->60
src\cua\target_app\workflow.py         76      3     42      2    96%   93-94, 114
-------------------------------------------------------------------------------
TOTAL                                1318     10    300     14    99%
Required test coverage of 80% reached. Total coverage: 98.52%
======================= 165 passed, 2 skipped in 11.08s =======================
```

## docs/schema/part-a-checks.txt

```text
uv run --locked ruff check .
All checks passed!
uv run --locked ruff format --check .
63 files already formatted
uv run --locked lint-imports
=============
Import Linter
=============


---------
Contracts
---------

Analyzed 61 files, 134 dependencies.
------------------------------------

Domain is framework-free KEPT
Policy is framework-free KEPT
Replay is framework-free KEPT
Application dependency layers KEPT
Automation cannot import the target application KEPT

Contracts: 5 kept, 0 broken.
uv run --locked mypy --strict src/cua
Success: no issues found in 33 source files
uv run --locked pytest
============================= test session starts =============================
platform win32 -- Python 3.12.14, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\interface.ai
configfile: pyproject.toml
testpaths: tests
plugins: anyio-4.15.1, Faker-40.39.0, hypothesis-6.168.0, langsmith-0.12.6, asyncio-1.4.0, cov-7.1.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 167 items

tests\browser\test_target_app_smoke.py ss                                [  1%]
tests\integration\target_app\test_app.py ............................... [ 19%]
.............                                                            [ 27%]
tests\unit\domain\test_approval.py ....                                  [ 29%]
tests\unit\domain\test_contracts.py .................................... [ 51%]
                                                                         [ 51%]
tests\unit\domain\test_invariants.py ................................... [ 72%]
...........                                                              [ 79%]
tests\unit\domain\test_relative_scope.py ...........                     [ 85%]
tests\unit\domain\test_schemas.py ...............                        [ 94%]
tests\unit\test_scaffold.py .                                            [ 95%]
tests\unit\test_target_app_boundary.py ..                                [ 96%]
tests\unit\test_target_app_cli.py ......                                 [100%]

=============================== tests coverage ================================
______________ coverage: platform win32, python 3.12.14-final-0 _______________

Name                                Stmts   Miss Branch BrPart  Cover   Missing
-------------------------------------------------------------------------------
src\cua\__init__.py                     1      0      0      0   100%
src\cua\catalog\__init__.py             0      0      0      0   100%
src\cua\discovery\__init__.py           0      0      0      0   100%
src\cua\domain\__init__.py              0      0      0      0   100%
src\cua\domain\actions.py              34      0      0      0   100%
src\cua\domain\capability.py          185      0     74      1    99%   269->253
src\cua\domain\common.py               43      0      0      0   100%
src\cua\domain\locators.py             72      0      6      0   100%
src\cua\domain\migrations.py           64      1     22      2    97%   52, 69->71
src\cua\domain\models.py               51      0      4      0   100%
src\cua\domain\names.py                38      0     16      1    98%   51->exit
src\cua\domain\observation.py          61      0      2      0   100%
src\cua\domain\ports.py                31      0      0      0   100%
src\cua\domain\predicates.py          130      0     24      0   100%
src\cua\domain\provenance.py           66      0     12      0   100%
src\cua\domain\results.py              45      0      0      0   100%
src\cua\domain\schemas.py              56      1     30      2    97%   39->exit, 51
src\cua\domain\steps.py                53      0      6      0   100%
src\cua\escalation\__init__.py          0      0      0      0   100%
src\cua\observability\__init__.py       0      0      0      0   100%
src\cua\policy\__init__.py              0      0      0      0   100%
src\cua\replay\__init__.py              0      0      0      0   100%
src\cua\surface\__init__.py             0      0      0      0   100%
src\cua\target_app\__init__.py          0      0      0      0   100%
src\cua\target_app\__main__.py          8      1      2      1    80%   23
src\cua\target_app\app.py             125      3     40      3    96%   65, 152, 183
src\cua\target_app\config.py           30      0      0      0   100%
src\cua\target_app\faults.py           29      1      4      1    94%   67
src\cua\target_app\models.py           92      0     10      0   100%
src\cua\target_app\state.py            28      0      6      1    97%   58->60
src\cua\target_app\workflow.py         76      3     42      2    96%   93-94, 114
-------------------------------------------------------------------------------
TOTAL                                1318     10    300     14    99%
Required test coverage of 80% reached. Total coverage: 98.52%
======================= 165 passed, 2 skipped in 10.90s =======================
```

## src/cua/surface/base.py

```python
"""Normalize backend trees and merge frames; forbid policy, replay, and discovery imports."""

from cua.domain.common import DomainModel
from cua.domain.names import normalize_name
from cua.domain.observation import AxNode, AxState, Bounds, FrameInfo, Observation, walk_ax
from cua.domain.ports import TreeChange


class BackendNode(DomainModel):
    """Portable input for UIA, AX or CDP; backend IDs stay outside the public observation."""

    role: str
    name: str = ""
    value: str | None = None
    description: str = ""
    states: frozenset[AxState] = frozenset()
    bounds: Bounds | None = None
    ignored: bool = False
    backend_id: int | None = None
    children: tuple["BackendNode", ...] = ()


class NodeBinding(DomainModel):
    frame_path: tuple[str, ...]
    node_path: tuple[int, ...]
    backend_id: int | None
    normalized_name: str


class NormalizedFrame(DomainModel):
    root: AxNode
    bindings: tuple[NodeBinding, ...]


class FrameSnapshot(DomainModel):
    info: FrameInfo
    root: AxNode
    # Parent AX node_path of the frame element. None is only valid for the main frame.
    owner_path: tuple[int, ...] | None = None


def normalize_tree(tree: BackendNode, frame_path: tuple[str, ...] = ()) -> NormalizedFrame:
    """Preserve original accessible names in AxNode.name; fold names only in a matching index.

    Replacing names by folded strings would hide label drift from observation hashes. Ignored
    and presentational wrappers are flattened before assigning structural paths within each frame.
    Backend identities and transient positions never enter the semantic hash.
    """
    bindings: list[NodeBinding] = []

    def exposed(node: BackendNode) -> tuple[BackendNode, ...]:
        if node.ignored or node.role.lower() in {"none", "presentation", "generic"}:
            return tuple(item for child in node.children for item in exposed(child))
        return (node,)

    def build(node: BackendNode, path: tuple[int, ...]) -> AxNode:
        bindings.append(
            NodeBinding(
                frame_path=frame_path,
                node_path=path,
                backend_id=node.backend_id,
                normalized_name=normalize_name(node.name),
            )
        )
        children = tuple(item for child in node.children for item in exposed(child))
        return AxNode(
            role=node.role.lower(),
            name=node.name,
            value=node.value,
            description=node.description,
            states=node.states,
            bounds=node.bounds,
            frame_path=frame_path,
            node_path=path,
            children=tuple(build(child, (*path, i)) for i, child in enumerate(children)),
        )

    return NormalizedFrame(root=build(tree, ()), bindings=tuple(bindings))


def merge_frames(frames: tuple[FrameSnapshot, ...]) -> AxNode:
    """Splice child document roots at their actual frame owners, preserving per-frame paths."""
    main = next(frame for frame in frames if not frame.info.frame_path)

    def merge(node: AxNode) -> AxNode:
        children = tuple(merge(child) for child in node.children)
        attached = tuple(
            merge(frame.root)
            for frame in frames
            if frame.info.frame_path[:-1] == node.frame_path
            and frame.info.frame_path
            and frame.owner_path == node.node_path
        )
        return node.model_copy(update={"children": (*children, *attached)})

    return merge(main.root)


def tree_diff(before: Observation, after: Observation) -> tuple[TreeChange, ...]:
    def indexed(obs: Observation) -> dict[tuple[tuple[str, ...], tuple[int, ...]], AxNode]:
        return {(n.frame_path, n.node_path): n for n in walk_ax(obs.ax_root)}

    left, right = indexed(before), indexed(after)
    changes = []
    for key in sorted(left.keys() | right.keys()):
        if key not in left or key not in right:
            fields: tuple[str, ...] = ("added" if key in right else "removed",)
        else:
            fields = tuple(
                field
                for field in ("role", "name", "value", "description", "states")
                if (
                    (left[key].states - {"focused"}) != (right[key].states - {"focused"})
                    if field == "states"
                    else getattr(left[key], field) != getattr(right[key], field)
                )
            )
        if fields:
            changes.append(TreeChange(frame_path=key[0], node_path=key[1], fields=fields))
    return tuple(changes)
```

## src/cua/surface/web.py

```python
"""Adapt Chromium to Surface; forbid policy, replay, discovery, and target_app imports.

Pinned Playwright Python 1.63.0 has no Page.accessibility. This adapter deliberately uses
public BrowserContext.new_cdp_session + Accessibility.getFullAXTree, not the removed snapshot
API. CDP supplies values and backend node identity that Locator.aria_snapshot YAML omits.
If CDP disappears, fail closed: a replacement ARIA-snapshot parser AND an identity bridge must
pass the same contract tests before being enabled; never infer a DOM tree as an AX substitute.
Sources: playwright.dev/python/docs/api/class-browsercontext#browser-context-new-cdp-session
and chromedevtools.github.io/devtools-protocol/tot/Accessibility/#method-getFullAXTree.
"""

import asyncio
import base64
import hashlib
import socket
import struct
import zlib
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal, cast

from playwright.async_api import (
    BrowserContext,
    CDPSession,
    Error,
    Locator,
    Page,
    Playwright,
    Request,
    async_playwright,
)
from pydantic import Field, JsonValue

from cua.domain.actions import (
    Action,
    Assert,
    Click,
    Dismiss,
    Navigate,
    ReadValue,
    Scroll,
    SelectOption,
    TypeText,
    WaitFor,
)
from cua.domain.common import DomainModel, EvidenceRef, LiteralRef, ValueRef, digest
from cua.domain.locators import (
    AxPathCandidate,
    CoordinatesCandidate,
    CssCandidate,
    LabelCandidate,
    LocatorCandidate,
    LocatorLadder,
    RoleNameCandidate,
    ScopedRoleNameCandidate,
    StructuralCandidate,
    VisibleTextCandidate,
    stability_key,
)
from cua.domain.observation import AxNode, Observation, SurfaceFingerprint, walk_ax
from cua.domain.ports import (
    ActionResult,
    CandidateAttempt,
    Clock,
    ControlToken,
    EvidencePayload,
    EvidenceSink,
    IdGenerator,
    Resolution,
    ResolvedTarget,
    SettleFailure,
    SettleTimeout,
    ValueResolver,
)
from cua.domain.predicates import AmbiguousTargetError, AxTarget, evaluate
from cua.domain.steps import StepTiming
from cua.surface._cdp import FrameCapture, accessible_element, backend_selector, capture_frames
from cua.surface.base import merge_frames, tree_diff


def opaque_screenshot(png: bytes) -> bytes:
    """Fail closed until field redaction exists: retain only dimensions, replace EVERY pixel.

    Merely masking html can miss fixed elements outside its box. Re-encoding an opaque PNG
    avoids acquiring a misleading partial-redaction promise and strips all ancillary metadata.
    Raw screenshot bytes never reach EvidenceSink, files, logs, traces or exporter callbacks.
    """
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Screenshot transport must be PNG")
    width, height = struct.unpack(">II", png[16:24])

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        png[:8]
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + b"\0\0\0" * width) * height))
        + chunk(b"IEND", b"")
    )


class WebConfig(DomainModel):
    user_data_dir: str
    base_url: str
    tenant_id: str
    headless: bool = True
    debugging_port: int = Field(default=0, ge=0, le=65535)
    action_timeout_ms: int = Field(default=1000, gt=0)


class NoopEvidenceSink:
    """Return content identity without writing data; a durable redacted store comes later."""

    def __init__(self, ids: IdGenerator) -> None:
        self._ids = ids

    async def put(self, payload: EvidencePayload) -> EvidenceRef:
        return EvidenceRef(
            evidence_id=self._ids.new(),
            media_type=payload.media_type,
            content_hash=hashlib.sha256(payload.content.encode()).hexdigest(),
        )


class LiteralValues:
    async def resolve_value(self, reference: ValueRef) -> str:
        if not isinstance(reference, LiteralRef):
            raise ValueError("Parameter/secret reference requires an injected ValueResolver")
        return str(reference.value.value)


class ControlCededError(RuntimeError):
    """Automation cannot observe, resolve or act while the human owns this session."""


class ActionBlockedError(RuntimeError):
    """The recorded target is stale, ambiguous or no longer actionable; do not dispatch."""


class _Resolved:
    def __init__(self, ladder: LocatorLadder, index: int) -> None:
        self.ladder, self.index = ladder, index


class PlaywrightWebSurface:
    """Only domain values cross this API. Browser resources stay private and are never recorded."""

    def __init__(
        self,
        config: WebConfig,
        clock: Clock,
        ids: IdGenerator,
        evidence: EvidenceSink,
        values: ValueResolver,
    ) -> None:
        self.config, self._clock, self._ids = config, clock, ids
        self._evidence, self._values = evidence, values
        self._lock = asyncio.Lock()
        self._ceded = False
        self._token: ControlToken | None = None
        self._requests: set[Request] = set()
        self._navigating: set[str] = set()
        self._handles: dict[str, _Resolved] = {}
        self._pulse = asyncio.Event()
        self._playwright: Playwright
        self._context: BrowserContext
        self._owner: BrowserContext
        self._page: Page
        self._cdp: CDPSession
        self._endpoint = ""
        self._last_observation: Observation | None = None
        self._reading_node: AxNode | None = None
        self._last_activity = 0.0

    @classmethod
    async def launch(
        cls,
        config: WebConfig,
        *,
        clock: Clock,
        ids: IdGenerator,
        evidence: EvidenceSink | None = None,
        values: ValueResolver | None = None,
    ) -> "PlaywrightWebSurface":
        self = cls(config, clock, ids, evidence or NoopEvidenceSink(ids), values or LiteralValues())
        port = config.debugging_port
        if not port:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
        self._endpoint = f"http://127.0.0.1:{port}"
        self._playwright = await async_playwright().start()
        try:
            self._owner = await self._playwright.chromium.launch_persistent_context(
                Path(config.user_data_dir),
                headless=config.headless,
                args=[f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1"],
                viewport={"width": 1440, "height": 1200},
                service_workers="block",
            )
            self._context = self._owner
            self._page = self._context.pages[0]
            await self._monitor()
            await self._page.goto(config.base_url + f"/t/{config.tenant_id}/", wait_until="load")
        except BaseException:
            await self._playwright.stop()
            raise
        return self

    async def _monitor(self) -> None:
        self._cdp = await self._context.new_cdp_session(self._page)
        await self._cdp.send("Page.enable")
        self._cdp.on("Page.frameStartedLoading", self._navigation_start)
        self._cdp.on("Page.frameStoppedLoading", self._navigation_end)
        self._page.on("request", self._request_start)
        self._page.on("requestfinished", self._request_end)
        self._page.on("requestfailed", self._request_end)

    def _request_start(self, request: Request) -> None:
        self._last_activity = asyncio.get_running_loop().time()
        self._requests.add(request)
        self._pulse.set()

    def _request_end(self, request: Request) -> None:
        self._last_activity = asyncio.get_running_loop().time()
        self._requests.discard(request)
        self._pulse.set()

    def _navigation_start(self, event: dict[str, Any]) -> None:
        self._last_activity = asyncio.get_running_loop().time()
        self._navigating.add(str(event["frameId"]))
        self._pulse.set()

    def _navigation_end(self, event: dict[str, Any]) -> None:
        self._last_activity = asyncio.get_running_loop().time()
        self._navigating.discard(str(event["frameId"]))
        self._pulse.set()

    def _assert_control(self) -> None:
        if self._ceded:
            raise ControlCededError("Control is ceded; resume the issued token first")

    async def close(self) -> None:
        async with self._lock:
            self._assert_control()
            await self._owner.close()
            await self._playwright.stop()

    async def _observe(
        self, *, evidence: bool = False, capture: FrameCapture | None = None
    ) -> Observation:
        owns_capture = capture is None
        capture = capture or await capture_frames(self._context, self._page, self._cdp)
        try:
            tree = merge_frames(tuple(capture.snapshots))
            # Read tag structure only, never persist full DOM/text/attribute values.
            structures = [
                await frame.evaluate(
                    "() => Array.from(document.querySelectorAll('*'), e => "
                    "[e.tagName, e.children.length])"
                )
                for frame in capture.frames.values()
            ]
        finally:
            if owns_capture:
                await capture.close()
        response = await self._context.request.get(
            self.config.base_url + "/_meta/version", params={"tenant_id": self.config.tenant_id}
        )
        fingerprint = SurfaceFingerprint.model_validate(
            {
                **await response.json(),
                "observed_at": self._clock.now(),
            }
        )
        screenshot_ref = None
        if evidence:
            # Until observability.redaction exists, capture ONLY a fully masked viewport. No raw
            # screenshot is acquired/exported; partial PII masking would be an unjustified promise.
            masked = await self._page.screenshot(
                mask=[self._page.locator("html")], mask_color="#000000", animations="disabled"
            )
            screenshot_ref = await self._evidence.put(
                EvidencePayload(
                    media_type="image/png;base64;redaction=full-viewport",
                    content=base64.b64encode(opaque_screenshot(masked)).decode("ascii"),
                )
            )
        result = Observation(
            observation_id=self._ids.new(),
            captured_at=self._clock.now(),
            surface_kind="web",
            url=self._page.url,
            title=await self._page.title(),
            ax_root=tree,
            frames=tuple(s.info for s in capture.snapshots),
            screenshot_ref=screenshot_ref,
            dom_digest=digest(cast(JsonValue, structures)),
            fingerprint=fingerprint,
        )
        self._last_observation = result
        return result

    async def observe(self) -> Observation:
        async with self._lock:
            self._assert_control()
            return await self._observe(evidence=True)

    async def _candidate_locator(
        self,
        candidate: LocatorCandidate,
        observation: Observation,
        capture: FrameCapture,
        *,
        first_match: bool = False,
    ) -> tuple[Locator | None, int]:
        frame = capture.frames[candidate.frame_path]
        nodes: tuple[AxNode, ...] = ()
        if isinstance(candidate, (RoleNameCandidate, ScopedRoleNameCandidate)):
            target = AxTarget(
                role=candidate.value.role,
                name_matcher=candidate.value.name_matcher,
                frame_path=candidate.frame_path,
                within=candidate.value.ancestor
                if isinstance(candidate, ScopedRoleNameCandidate)
                else None,
            )
            try:
                nodes = target.select(observation)
            except AmbiguousTargetError:
                # Ancestor resolution still fails closed. Only the leaf may opt into first_match.
                roots = (
                    target.within.select(observation) if target.within else (observation.ax_root,)
                )
                nodes = tuple(
                    n
                    for root in roots
                    for n in walk_ax(root)
                    if n.role == target.role
                    and target.name_matcher.matches(n.name)
                    and n.frame_path == candidate.frame_path
                )
        elif isinstance(candidate, AxPathCandidate):
            nodes = tuple(
                n
                for n in walk_ax(observation.ax_root)
                if n.frame_path == candidate.frame_path and n.node_path == candidate.value.path
            )
        elif isinstance(candidate, (LabelCandidate, VisibleTextCandidate)):
            nodes = tuple(
                n
                for n in walk_ax(observation.ax_root)
                if n.frame_path == candidate.frame_path
                and candidate.value.matcher.matches(n.name)
                and (
                    (isinstance(candidate, VisibleTextCandidate) and n.role == "text")
                    or (
                        isinstance(candidate, LabelCandidate)
                        and n.role in {"textbox", "combobox", "checkbox", "radio"}
                    )
                )
            )
        elif isinstance(candidate, CssCandidate):
            return await self._fallback_locator(capture, candidate, candidate.value.selector)
        elif isinstance(candidate, StructuralCandidate):
            selector = "html" + "".join(f" > :nth-child({i + 1})" for i in candidate.value.path)
            return await self._fallback_locator(capture, candidate, selector)
        elif isinstance(candidate, CoordinatesCandidate):
            # Coordinates are frame-local CSS pixels, not global screen coordinates.
            css = await frame.evaluate(
                """p => {
                let e = document.elementFromPoint(p.x, p.y), parts = [];
                while (e && e.nodeType === 1) {
                    let i = 1, s = e.previousElementSibling;
                    while(s) { i++; s=s.previousElementSibling; }
                    parts.unshift(e.localName+':nth-child('+i+')'); e=e.parentElement;
                } return parts.join(' > ');
            }""",
                {"x": candidate.value.x, "y": candidate.value.y},
            )
            if not css:
                return None, 0
            return await self._fallback_locator(capture, candidate, str(css))
        if not nodes:
            return None, 0
        if len(nodes) > 1 and not first_match:
            return None, len(nodes)
        self._reading_node = nodes[0]
        binding = capture.bindings[(nodes[0].frame_path, nodes[0].node_path)]
        if binding.backend_id is None:
            return None, 0
        selector = await backend_selector(
            capture.sessions[candidate.frame_path], binding.backend_id
        )
        return frame.locator(selector), len(nodes)

    async def _fallback_locator(
        self, capture: FrameCapture, candidate: LocatorCandidate, selector: str
    ) -> tuple[Locator | None, int]:
        locator = capture.frames[candidate.frame_path].locator(selector)
        count = await locator.count()
        if count:
            document = capture.bindings[(candidate.frame_path, ())].backend_id
            if document is not None:
                raw = await accessible_element(
                    capture.sessions[candidate.frame_path], document, selector
                )
                if raw is not None:
                    self._reading_node = AxNode(
                        role=raw.role, name=raw.name, value=raw.value, description=raw.description
                    )
        return locator, count

    async def _actionable(self, locator: Locator) -> bool:
        if (
            not await locator.count()
            or not await locator.is_enabled()
            or not await locator.is_visible()
        ):
            return False
        # Trial click checks viewport, nested frame clipping, overlays and event interception,
        # and works for span[role=button] without dispatching an input event.
        try:
            await locator.click(trial=True, timeout=self.config.action_timeout_ms)
            return await locator.bounding_box() is not None
        except Error:
            return False

    async def _resolve(self, ladder: LocatorLadder) -> tuple[Resolution, Locator | None]:
        capture = await capture_frames(self._context, self._page, self._cdp)
        attempts: list[CandidateAttempt] = []
        final_count = 0
        try:
            observation = await self._observe(capture=capture)
            for index, candidate in enumerate(ladder.candidates):
                self._reading_node = None
                final_count = 0
                outcome: LiteralOutcome = "not_found"
                detail = "No node matched the recorded candidate"
                locator: Locator | None = None
                if candidate.frame_path not in capture.frames:
                    outcome, detail = "wrong_frame", "Recorded frame path is absent"
                else:
                    try:
                        locator, final_count = await self._candidate_locator(
                            candidate,
                            observation,
                            capture,
                            first_match=ladder.match_policy == "first_match",
                        )
                        if final_count > 1 and ladder.match_policy == "require_unique":
                            outcome, detail = (
                                "ambiguous",
                                "Multiple nodes match; require_unique rejected them",
                            )
                        elif locator is not None and final_count:
                            locator = locator.first
                            if await self._actionable(locator):
                                outcome, detail = (
                                    "matched",
                                    "Unique actionable candidate"
                                    if final_count == 1
                                    else "Explicit first_match policy",
                                )
                            else:
                                outcome, detail = (
                                    "not_actionable",
                                    "Node is disabled, occluded or outside the actionable viewport",
                                )
                        # The fixture replaces the pending destination with a modal. A blocking
                        # dialog takes precedence over not_found; we do not claim hidden existence.
                        elif any(
                            n.role in {"dialog", "alertdialog"}
                            for n in walk_ax(observation.ax_root)
                        ):
                            outcome, detail = (
                                "not_actionable",
                                "Modal blocks the pending destination; dismiss it before resolving",
                            )
                    except AmbiguousTargetError:
                        outcome, detail = (
                            "ambiguous",
                            "Semantic target or ancestor matches multiple nodes",
                        )
                        # No leaf count is known when the ancestor itself is ambiguous.
                        final_count = 0
                    except Error:
                        outcome, detail = "not_found", "Node detached or candidate is invalid"
                attempts.append(
                    CandidateAttempt(candidate=candidate, outcome=outcome, detail=detail)
                )
                if outcome == "matched":
                    handle = self._ids.new()
                    self._handles[handle] = _Resolved(ladder, index)
                    delta = float(
                        stability_key(ladder.candidates[0])[0] - stability_key(candidate)[0]
                    )
                    return Resolution(
                        winning_candidate=candidate,
                        index=index,
                        match_count=final_count,
                        attempts=tuple(attempts),
                        degraded=index > 0,
                        degradation_delta=delta,
                        target=ResolvedTarget(handle=handle, observation_hash=observation.hash),
                    ), locator
            return Resolution(
                winning_candidate=None,
                index=None,
                match_count=final_count,
                attempts=tuple(attempts),
                degraded=False,
                degradation_delta=0,
                target=None,
            ), None
        finally:
            await capture.close()

    async def resolve(self, ladder: LocatorLadder) -> Resolution:
        async with self._lock:
            self._assert_control()
            result, _ = await self._resolve(ladder)
            return result

    async def _settle(self, timing: StepTiming) -> Observation:
        """Wait on a timer/event race, never sleep. Network AND AX stability AND navigation gate.

        A poll interval separates consecutive samples; the stability window also applies to
        network quiescence. Events wake the scheduler but never permit back-to-back AX samples.
        The deadline encloses observation I/O as well, so a hung snapshot cannot defeat timeout.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timing.timeout_ms / 1000
        previous = last = self._last_observation
        stable_since = loop.time()
        next_poll = loop.time() + timing.poll_interval_ms / 1000
        try:
            async with asyncio.timeout_at(deadline):
                previous = last = await self._observe()
                while True:
                    self._pulse.clear()
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._pulse.wait(), max(0.001, next_poll - loop.time())
                        )
                    if self._requests or self._navigating:
                        stable_since = loop.time()
                    if loop.time() < next_poll:
                        continue
                    try:
                        current = await self._observe()
                    except Error:
                        if not self._requests and not self._navigating:
                            raise
                        next_poll = loop.time() + timing.poll_interval_ms / 1000
                        continue
                    previous, last = last, current
                    if previous.hash != last.hash:
                        stable_since = loop.time()
                    if (
                        not self._requests
                        and not self._navigating
                        and previous.hash == last.hash
                        and loop.time() - max(stable_since, self._last_activity)
                        >= timing.stability_window_ms / 1000
                    ):
                        return last
                    next_poll = loop.time() + timing.poll_interval_ms / 1000
        except TimeoutError as exc:
            raise SettleTimeout(
                SettleFailure(
                    last_hashes=(
                        previous.hash if previous else digest(None),
                        last.hash if last else digest(None),
                    ),
                    diff=tree_diff(previous, last) if previous and last else (),
                    pending_requests=len(self._requests),
                    pending_navigation=bool(self._navigating),
                )
            ) from exc

    async def settle(self, timing: StepTiming) -> Observation:
        async with self._lock:
            self._assert_control()
            return await self._settle(timing)

    async def act(
        self,
        action: Action,
        resolved_target: ResolvedTarget | None = None,
        *,
        timing: StepTiming | None = None,
    ) -> ActionResult:
        async with self._lock:
            self._assert_control()
            before = await self._observe()
            locator = None
            if resolved_target is not None:
                entry = self._handles.get(resolved_target.handle)
                if entry is None or resolved_target.observation_hash != before.hash:
                    raise ActionBlockedError("Resolved target is stale; observe and resolve again")
                resolution, locator = await self._resolve(entry.ladder)
                if (
                    resolution.index != entry.index
                    or locator is None
                    or resolution.target is None
                    or resolution.target.observation_hash != before.hash
                ):
                    raise ActionBlockedError("Target is no longer uniquely actionable")
            if (
                isinstance(action, (Click, Dismiss, TypeText, SelectOption, ReadValue))
                and locator is None
            ):
                raise ActionBlockedError("This action requires an actionable resolved target")
            value = None
            if isinstance(action, Navigate):
                await self._page.goto(action.url, wait_until="commit")
            elif isinstance(action, (Click, Dismiss)):
                assert locator is not None
                await locator.click(timeout=self.config.action_timeout_ms, no_wait_after=True)
            elif isinstance(action, (TypeText, SelectOption)):
                assert locator is not None
                text = await self._values.resolve_value(action.value_ref)
                if isinstance(action, TypeText):
                    await locator.fill(text, timeout=self.config.action_timeout_ms)
                else:
                    await locator.select_option(label=text, timeout=self.config.action_timeout_ms)
            elif isinstance(action, ReadValue):
                assert locator is not None
                if self._reading_node is None:
                    raise ActionBlockedError("Read target has no browser-computed AX attributes")
                value = getattr(self._reading_node, action.attribute)
            elif isinstance(action, WaitFor):
                await self._wait_predicate(
                    action,
                    timing
                    or StepTiming(
                        settle_strategy="ax_stable", timeout_ms=self.config.action_timeout_ms
                    ),
                )
            elif isinstance(action, Assert):
                result = evaluate(action.predicate, before)
                if not result.satisfied:
                    raise ActionBlockedError("Surface assertion is not satisfied")
            elif isinstance(action, Scroll):
                amount = action.amount if action.direction in {"down", "right"} else -action.amount
                await self._page.mouse.wheel(
                    amount if action.direction in {"left", "right"} else 0,
                    amount if action.direction in {"up", "down"} else 0,
                )
            after = (
                await self._settle(timing)
                if timing and timing.settle_strategy != "none"
                else await self._observe()
            )
            return ActionResult(
                attempted=action,
                before_hash=before.hash,
                after_hash=after.hash,
                changed=before.hash != after.hash,
                value=value,
                settle_outcome="settled"
                if timing and timing.settle_strategy != "none"
                else "not_requested",
            )

    async def _wait_predicate(self, action: WaitFor, timing: StepTiming) -> None:
        previous = last = await self._observe()
        try:
            async with asyncio.timeout(timing.timeout_ms / 1000):
                while not evaluate(action.predicate, last).satisfied:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            asyncio.Event().wait(), timing.poll_interval_ms / 1000
                        )
                    previous, last = last, await self._observe()
        except TimeoutError as exc:
            raise SettleTimeout(
                SettleFailure(
                    last_hashes=(previous.hash, last.hash),
                    diff=tree_diff(previous, last),
                    pending_requests=len(self._requests),
                    pending_navigation=bool(self._navigating),
                )
            ) from exc

    async def cede_control(self) -> ControlToken:
        async with self._lock:
            self._assert_control()
            info = (await self._cdp.send("Target.getTargetInfo"))["targetInfo"]
            token = ControlToken(
                cdp_endpoint=self._endpoint,
                context_id=info.get("browserContextId", "default"),
                page_guid=info["targetId"],
                issued_at=self._clock.now(),
            )
            self._page.remove_listener("request", self._request_start)
            self._page.remove_listener("requestfinished", self._request_end)
            self._page.remove_listener("requestfailed", self._request_end)
            await self._cdp.detach()
            self._token, self._ceded = token, True
            self._handles.clear()
            return token

    async def resume_control(self, token: ControlToken) -> None:
        async with self._lock:
            if not self._ceded or token != self._token:
                raise ControlCededError("Resume requires the exact outstanding control token")
            browser = await self._playwright.chromium.connect_over_cdp(token.cdp_endpoint)
            for context in browser.contexts:
                for page in context.pages:
                    session = await context.new_cdp_session(page)
                    info = (await session.send("Target.getTargetInfo"))["targetInfo"]
                    await session.detach()
                    if (
                        info["targetId"] == token.page_guid
                        and info.get("browserContextId", "default") == token.context_id
                    ):
                        self._context, self._page = context, page
                        self._requests.clear()
                        self._navigating.clear()
                        await self._monitor()
                        self._ceded, self._token = False, None
                        return
            raise ControlCededError(
                "Original page/context no longer exists; refusing a new session"
            )


LiteralOutcome = Literal["matched", "not_found", "ambiguous", "wrong_frame", "not_actionable"]
```

## src/cua/surface/_cdp.py

```python
"""Translate CDP into portable trees; forbid higher automation-layer imports."""

from typing import Any, cast

from playwright.async_api import BrowserContext, CDPSession, Error, Frame, Page
from pydantic import BaseModel, ConfigDict, Field

from cua.domain.observation import AxState, FrameInfo
from cua.surface.base import BackendNode, FrameSnapshot, NodeBinding, normalize_tree


class WireModel(BaseModel):
    """CDP may add transport metadata; consume only known fields inside this adapter."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class AXValue(WireModel):
    value: str | float | bool | None = None


class AXProperty(WireModel):
    name: str
    value: AXValue


class AXWireNode(WireModel):
    node_id: str = Field(alias="nodeId")
    ignored: bool = False
    role: AXValue = Field(default_factory=AXValue)
    name: AXValue = Field(default_factory=AXValue)
    value: AXValue = Field(default_factory=AXValue)
    description: AXValue = Field(default_factory=AXValue)
    backend_id: int | None = Field(default=None, alias="backendDOMNodeId")
    child_ids: tuple[str, ...] = Field(default=(), alias="childIds")
    properties: tuple[AXProperty, ...] = ()


class AXTree(WireModel):
    nodes: tuple[AXWireNode, ...]


def portable_tree(tree: AXTree) -> BackendNode:
    nodes = {node.node_id: node for node in tree.nodes}

    def build(node: AXWireNode) -> BackendNode:
        children = tuple(build(nodes[key]) for key in node.child_ids if key in nodes)
        role = str(node.role.value or "none").lower()
        role = {"rootwebarea": "document", "statictext": "text"}.get(role, role)
        name = str(node.name.value or "")
        if role == "row" and not name:
            # Native table rows often have no computed name. Aggregate their cell names, as
            # ARIA snapshots/UIA grid adapters do, so semantic row scoping is portable.
            name = " ".join(child.name for child in children if child.name)
        states = frozenset(
            cast(AxState, prop.name)
            for prop in node.properties
            if prop.name in {"disabled", "focused", "checked", "expanded", "required", "invalid"}
            and prop.value.value not in {False, None, "false", "none"}
        )
        return BackendNode(
            role=role,
            name=name,
            value=str(node.value.value) if node.value.value is not None else None,
            description=str(node.description.value or ""),
            states=states,
            ignored=node.ignored or role == "inlinetextbox",
            backend_id=node.backend_id,
            children=children,
        )

    return build(tree.nodes[0])


class FrameCapture:
    """Private transport ownership; none of these Playwright handles cross the Surface port."""

    def __init__(self) -> None:
        self.frames: dict[tuple[str, ...], Frame] = {}
        self.bindings: dict[tuple[tuple[str, ...], tuple[int, ...]], NodeBinding] = {}
        self.sessions: dict[tuple[str, ...], CDPSession] = {}
        self.owned_sessions: list[CDPSession] = []
        self.snapshots: list[FrameSnapshot] = []

    async def close(self) -> None:
        for session in self.owned_sessions:
            await session.detach()


async def capture_frames(context: BrowserContext, page: Page, cdp: CDPSession) -> FrameCapture:
    capture = FrameCapture()
    wire = await cdp.send("Page.getFrameTree")

    async def visit(
        info: dict[str, Any],
        frame: Frame,
        path: tuple[str, ...],
        owner_path: tuple[int, ...] | None,
    ) -> None:
        try:
            session = await context.new_cdp_session(frame)
            capture.owned_sessions.append(session)
        except Error as exc:
            if not path or "does not have a separate CDP session" not in str(exc):
                raise
            # Same-process frames share their parent's target, but still need their own frameId.
            session = capture.sessions[path[:-1]]
        capture.sessions[path] = session
        raw = await session.send("Accessibility.getFullAXTree", {"frameId": info["frame"]["id"]})
        normalized = normalize_tree(portable_tree(AXTree.model_validate(raw)), path)
        capture.frames[path] = frame
        capture.bindings.update({(path, b.node_path): b for b in normalized.bindings})
        capture.snapshots.append(
            FrameSnapshot(
                info=FrameInfo(frame_path=path, title=await frame.title()),
                root=normalized.root,
                owner_path=owner_path,
            )
        )
        used: set[str] = set()
        for child_info in info.get("childFrames", []):
            child_id = child_info["frame"]["id"]
            owner = await session.send("DOM.getFrameOwner", {"frameId": child_id})
            backend_id = owner["backendNodeId"]
            binding = next((b for b in normalized.bindings if b.backend_id == backend_id), None)
            if binding is None:
                raise RuntimeError("Frame owner is absent from the accessibility tree")
            css = await backend_selector(session, backend_id)
            element = await frame.locator(css).element_handle()
            if element is None:
                raise RuntimeError("Frame owner detached during observation")
            child_frame = await element.content_frame()
            if child_frame is None:
                raise RuntimeError("Frame content detached during observation")
            label = await element.get_attribute("title") or await element.get_attribute("name")
            if not label:
                label = f"frame[{binding.node_path}]"
            if label in used:
                raise RuntimeError("Duplicate sibling frame names require explicit fixture scoping")
            used.add(label)
            await visit(child_info, child_frame, (*path, label), binding.node_path)

    try:
        await visit(wire["frameTree"], page.main_frame, (), None)
    except BaseException:
        await capture.close()
        raise
    return capture


SELECTOR_FUNCTION = """function() {
    let e = this.nodeType === 1 ? this : this.parentElement, parts = [];
    while (e && e.nodeType === 1) {
        let i = 1, sibling = e.previousElementSibling;
        while (sibling) { i++; sibling = sibling.previousElementSibling; }
        parts.unshift(e.localName + ':nth-child(' + i + ')'); e = e.parentElement;
    }
    return parts.join(' > ');
}"""


async def backend_selector(session: CDPSession, backend_id: int) -> str:
    """Resolve this observed node to a short-lived DOM selector, never a recorded fallback.

    CDP backend IDs are authoritative within the snapshot. The selector is regenerated each
    resolve and rejected after AX state changes; it is not exported as semantic provenance.
    """
    remote = await session.send("DOM.resolveNode", {"backendNodeId": backend_id})
    object_id = remote["object"]["objectId"]
    try:
        result = await session.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": SELECTOR_FUNCTION,
                "returnByValue": True,
            },
        )
        value = result.get("result", {}).get("value")
        if not isinstance(value, str) or not value:
            raise RuntimeError("Observed node has no actionable DOM element")
        return value
    finally:
        await session.send("Runtime.releaseObject", {"objectId": object_id})


async def accessible_element(
    session: CDPSession, document_backend: int, selector: str
) -> BackendNode | None:
    """Read browser-computed AX attributes for a fallback element, never guess from innerText."""
    document = await session.send("DOM.resolveNode", {"backendNodeId": document_backend})
    document_id = document["object"]["objectId"]
    element_id: str | None = None
    try:
        element = await session.send(
            "Runtime.callFunctionOn",
            {
                "objectId": document_id,
                "functionDeclaration": "function(s) { return this.querySelector(s); }",
                "arguments": [{"value": selector}],
            },
        )
        element_id = element["result"].get("objectId")
        if element_id is None:
            return None
        raw = await session.send(
            "Accessibility.getPartialAXTree",
            {
                "objectId": element_id,
                "fetchRelatives": False,
            },
        )
        return portable_tree(AXTree.model_validate(raw))
    finally:
        if element_id is not None:
            await session.send("Runtime.releaseObject", {"objectId": element_id})
        await session.send("Runtime.releaseObject", {"objectId": document_id})
```

## src/cua/domain/ports.py

```python
"""Declare effect boundaries only; forbid implementations, I/O, and other cua packages."""

from datetime import datetime
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, JsonValue

from cua.domain.actions import Action
from cua.domain.capability import Capability, ExtractorSpec
from cua.domain.common import Digest, DomainModel, EvidenceRef, ValueRef
from cua.domain.locators import LocatorCandidate, LocatorLadder
from cua.domain.models import DecisionRequest, DecisionResult
from cua.domain.observation import Observation
from cua.domain.results import JournalEntry
from cua.domain.steps import StepTiming


class ExtractedValue(DomainModel):
    value: JsonValue
    evidence_ref: EvidenceRef


class EvidencePayload(DomainModel):
    """Adapters must redact before constructing payloads; this type cannot certify redaction."""

    media_type: str
    content: str


class Surface(Protocol):
    async def observe(self) -> Observation: ...
    async def resolve(self, ladder: LocatorLadder) -> "Resolution": ...
    async def act(
        self,
        action: Action,
        resolved_target: "ResolvedTarget | None" = None,
        *,
        timing: StepTiming | None = None,
    ) -> "ActionResult": ...
    async def settle(self, timing: StepTiming) -> Observation: ...
    async def cede_control(self) -> "ControlToken": ...
    async def resume_control(self, token: "ControlToken") -> None: ...


class CandidateAttempt(DomainModel):
    candidate: LocatorCandidate
    outcome: Literal["matched", "not_found", "ambiguous", "wrong_frame", "not_actionable"]
    detail: str


class ResolvedTarget(DomainModel):
    """Opaque adapter reference; never a Playwright handle or an executable locator."""

    handle: str
    observation_hash: Digest


class Resolution(DomainModel):
    winning_candidate: LocatorCandidate | None
    index: int | None
    match_count: int = Field(ge=0)
    attempts: tuple[CandidateAttempt, ...]
    degraded: bool
    degradation_delta: float = Field(ge=0)
    target: ResolvedTarget | None


class TreeChange(DomainModel):
    """Diff metadata excludes raw names/values so timeout errors do not become a data exporter."""

    frame_path: tuple[str, ...]
    node_path: tuple[int, ...]
    fields: tuple[str, ...]


class SettleFailure(DomainModel):
    last_hashes: tuple[Digest, Digest]
    diff: tuple[TreeChange, ...]
    pending_requests: int
    pending_navigation: bool


class SettleTimeout(TimeoutError):
    def __init__(self, detail: SettleFailure) -> None:
        self.detail = detail
        super().__init__("Surface did not settle before its deadline; inspect typed diff metadata")


class ActionResult(DomainModel):
    attempted: Action
    before_hash: Digest
    after_hash: Digest
    changed: bool
    settle_outcome: Literal["settled", "not_requested"]
    value: str | None = None


class ControlToken(DomainModel):
    """Session attachment coordinates, not authorization; an escalation lease must protect them."""

    cdp_endpoint: str
    context_id: str
    page_guid: str
    issued_at: AwareDatetime


class ValueResolver(Protocol):
    async def resolve_value(self, reference: ValueRef) -> str: ...


class LLMClient(Protocol):
    async def decide(self, request: DecisionRequest) -> DecisionResult: ...


class Extractor(Protocol):
    def extract(self, spec: ExtractorSpec, observation: Observation) -> ExtractedValue: ...


class EvidenceSink(Protocol):
    async def put(self, payload: EvidencePayload) -> EvidenceRef: ...


class JournalSink(Protocol):
    async def append(self, entry: JournalEntry) -> str: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    def new(self) -> str: ...


class VerificationCheck(DomainModel):
    asserted: str
    found: str
    passed: bool


class VerificationReport(DomainModel):
    verified: bool
    checks: tuple[VerificationCheck, ...]


class ProvenanceVerifier(Protocol):
    """Schema validation proves an artifact CLAIMS attribution; verification proves the claim.

    Stage 7 must resolve observation hashes in the named run, verify the chain through each
    referenced record, resolve and hash every evidence_ref, and check that HumanEdit before/after
    hashes bracket real changes. Without this boundary provenance is decoration.
    Verify approval.reviewed_digest against capability_content_digest and the journal's review
    record, including the authenticated reviewer and exactly the content that was reviewed.
    """

    def verify(self, capability: Capability) -> VerificationReport: ...
```

## src/cua/surface/desktop.py

```python
"""Declare the desktop adapter roadmap; forbid Playwright and higher-layer imports.

observe -> Windows UIAutomation tree walk / macOS AXUIElement hierarchy.
resolve -> AutomationElement property conditions / AXUIElementCopyAttributeValue, with the same
semantic ancestor scopes and fail-closed ambiguity policy as the web adapter.
act -> InvokePattern and ValuePattern / AXUIElementPerformAction and attribute setters.
settle -> repeated UIA/AX tree stability under a deadline; there is no network-quiescence signal.
cede_control -> release the input lease while retaining the native window/process reference.
resume_control -> reacquire that lease and revalidate the original window identity.

URL and frames do not port; desktop observations use url=None and an empty frame inventory.
Coordinates carry more weight for custom-drawn widgets. A future versioned control token must
replace web CDP attachment coordinates with native process/window references, never fake a URL.
Every method intentionally raises NotImplementedError rather than pretending desktop is supported.
"""

from cua.domain.actions import Action
from cua.domain.locators import LocatorLadder
from cua.domain.observation import Observation
from cua.domain.ports import ActionResult, ControlToken, Resolution, ResolvedTarget
from cua.domain.steps import StepTiming


class DesktopSurface:
    async def observe(self) -> Observation:
        raise NotImplementedError("Desktop perception requires a UIA/AX backend")

    async def resolve(self, ladder: LocatorLadder) -> Resolution:
        raise NotImplementedError("Desktop resolution requires a UIA/AX backend")

    async def act(
        self,
        action: Action,
        resolved_target: ResolvedTarget | None = None,
        *,
        timing: StepTiming | None = None,
    ) -> ActionResult:
        raise NotImplementedError("Desktop actions require a UIA/AX backend")

    async def settle(self, timing: StepTiming) -> Observation:
        raise NotImplementedError("Desktop settling requires UIA/AX tree stability")

    async def cede_control(self) -> ControlToken:
        raise NotImplementedError("Desktop handoff requires a native window token")

    async def resume_control(self, token: ControlToken) -> None:
        raise NotImplementedError("Desktop resume requires a native window token")
```

## src/cua/surface/README.md

```text
# Surface adapter

`Surface` separates normalized observations and typed actions from transport. Public methods
return domain models declared in `domain/ports.py`; no Playwright object crosses that boundary.
`PlaywrightWebSurface.launch` takes WebConfig, Clock, IdGenerator, EvidenceSink and ValueResolver.
The default value resolver accepts literal refs only; parameter/secret resolution is injected.
The desktop implementation is an explicit typed stub, with UIA/AX mappings in its docstring.

## Perception choice

The installed and pinned Python Playwright version is 1.63.0. Local API inspection confirmed
`Page.accessibility` is absent, while `Locator.aria_snapshot` returns a string. The web adapter
uses `Accessibility.getFullAXTree` through the public `new_cdp_session` API for values and DOM
node identity. A removed CDP API fails closed; an ARIA YAML parser plus a tested identity bridge
would be needed before enabling a replacement. There is no silent DOM-as-AX fallback.

Sources: [Playwright CDP sessions](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-new-cdp-session),
[ARIA snapshots](https://playwright.dev/python/docs/api/class-locator#locator-aria-snapshot),
[CDP Accessibility](https://chromedevtools.github.io/devtools-protocol/tot/Accessibility/).

Each frame is captured separately, sharing its parent's CDP session when Chromium puts them in
the same process. Child documents are spliced at their observed iframe owner. Paths use frame
titles/names; unnamed frames use structural paths and duplicate sibling names fail closed.
Original accessible names remain in AxNode.name. A private matching index stores folded names.
Presentational wrappers are flattened before per-frame node paths are assigned. Table rows
without names receive their cell names joined in order; this convention needs equivalent UIA/AX
grid support. Domain observation hashing is the only semantic hash implementation.

## Resolution and actions

Every attempted candidate records matched, not_found, wrong_frame, ambiguous or not_actionable.
`require_unique` never silently chooses a first node. `first_match` is an explicit ladder policy;
an ambiguous semantic ancestor still fails closed. Degradation delta is the difference in the
documented strategy ranks, not a model confidence score.

Targets are opaque adapter handles bound to the observation hash. Act reobserves and resolves
again, then rejects changed state or a different winning candidate. Actionability uses Chromium's
trial-click checks for visibility, enabled state, viewport reachability and event interception.
Coordinates are frame-local CSS pixels and require a real bounding box. ReadValue returns
browser-computed AX attributes, including accessible names/descriptions that differ from text.

The temporary CDP-to-DOM bridge generates a CSS path for the observed backend node. That path
is never stored in the capability or described as a semantic locator. It has not been qualified
for closed shadow roots, custom embedded controls, cross-origin out-of-process frames, or frame
titles that change mid-run. Those cases need adapter tests and may currently fail closed.

The fixture's interstitial replaces its pending destination. A modal therefore takes precedence
over a missing target: resolution reports not_actionable and requires dismissal. This does not
claim that the absent control has been observed behind the dialog. Untagged legacy overlays
are caught when they intercept an existing control, but a replacement screen without modal
semantics cannot be classified this way automatically.

## Settling and handoff

Settling requires network quiescence, no pending frame navigation, and equal consecutive AX
hashes separated by StepTiming.poll_interval_ms for StepTiming.stability_window_ms. A deadline
also bounds snapshot I/O. The scheduler waits on events/timers, never fixed sleeps. A timeout
contains the last two hashes and changed field/path metadata, not raw financial values. If no
snapshot has ever completed, the missing hash is the digest of JSON null.

Chromium uses a persistent profile and loopback CDP port. Cede waits for any current operation,
detaches the monitor and blocks all automated observation/actions. Resume checks the exact
issued token and reconnects to the original browser-context ID and target ID; it does not create
a fresh session. Tests use a second Playwright connection to edit the same unfinished form and
verify cookies and typed content survive. This proves mechanics, not an authenticated operator
lease; Stage 9 must protect token ownership and approval identity.

## Evidence and verification

DOM evidence is a digest of tag/child-count structure, never a persisted DOM dump. Screenshots
are currently replaced with an entirely opaque PNG before the injected EvidenceSink receives
them. This conservative default retains dimensions but offers no visual audit detail. Field-level
redaction must be implemented before exporting useful screenshots. The default sink returns
a digest reference and stores nothing; production evidence storage is a later stage.

`pytest tests/integration/surface` runs real Chromium against loopback servers. Install the pinned
browser with `uv run playwright install chromium` first. The earlier CI tenant smoke job installs
Chromium separately. Adding the same setup to the main checks job needs the requested CI scope
extension; until then a fresh checks runner must provision Chromium before the default suite.
No external site, model API, or banking service is contacted by the tests.
```

## src/cua/surface/part-b-checks.txt

```text
uv run --locked ruff check .
All checks passed!
uv run --locked ruff format --check .
75 files already formatted
uv run --locked lint-imports
=============
Import Linter
=============


---------
Contracts
---------

Analyzed 71 files, 169 dependencies.
------------------------------------

Domain is framework-free KEPT
Policy is framework-free KEPT
Replay is framework-free KEPT
Application dependency layers KEPT
Automation cannot import the target application KEPT
Only the surface adapter imports Playwright KEPT

Contracts: 6 kept, 0 broken.
uv run --locked mypy --strict src/cua
Success: no issues found in 37 source files
uv run --locked pytest
============================= test session starts =============================
platform win32 -- Python 3.12.14, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\interface.ai
configfile: pyproject.toml
testpaths: tests
plugins: anyio-4.15.1, Faker-40.39.0, hypothesis-6.168.0, langsmith-0.12.6, asyncio-1.4.0, cov-7.1.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 194 items

tests\browser\test_target_app_smoke.py ..                                [  1%]
tests\integration\surface\test_web.py ..................                 [ 10%]
tests\integration\target_app\test_app.py ............................... [ 26%]
.............                                                            [ 32%]
tests\unit\domain\test_approval.py ....                                  [ 35%]
tests\unit\domain\test_contracts.py .................................... [ 53%]
                                                                         [ 53%]
tests\unit\domain\test_invariants.py ................................... [ 71%]
...........                                                              [ 77%]
tests\unit\domain\test_relative_scope.py ...........                     [ 82%]
tests\unit\domain\test_schemas.py ...............                        [ 90%]
tests\unit\surface\test_base.py ......                                   [ 93%]
tests\unit\test_golden_updates.py .                                      [ 94%]
tests\unit\test_scaffold.py .                                            [ 94%]
tests\unit\test_surface_boundary.py ..                                   [ 95%]
tests\unit\test_target_app_boundary.py ..                                [ 96%]
tests\unit\test_target_app_cli.py ......                                 [100%]

=============================== tests coverage ================================
______________ coverage: platform win32, python 3.12.14-final-0 _______________

Name                                Stmts   Miss Branch BrPart  Cover   Missing
-------------------------------------------------------------------------------
src\cua\__init__.py                     1      0      0      0   100%
src\cua\catalog\__init__.py             0      0      0      0   100%
src\cua\discovery\__init__.py           0      0      0      0   100%
src\cua\domain\__init__.py              0      0      0      0   100%
src\cua\domain\actions.py              34      0      0      0   100%
src\cua\domain\capability.py          185      0     74      1    99%   269->253
src\cua\domain\common.py               43      0      0      0   100%
src\cua\domain\locators.py             72      0      6      0   100%
src\cua\domain\migrations.py           64      1     22      2    97%   52, 69->71
src\cua\domain\models.py               51      0      4      0   100%
src\cua\domain\names.py                38      0     16      1    98%   51->exit
src\cua\domain\observation.py          61      0      2      0   100%
src\cua\domain\ports.py                73      0      0      0   100%
src\cua\domain\predicates.py          130      0     24      0   100%
src\cua\domain\provenance.py           66      0     12      0   100%
src\cua\domain\results.py              45      0      0      0   100%
src\cua\domain\schemas.py              56      1     30      2    97%   39->exit, 51
src\cua\domain\steps.py                53      0      6      0   100%
src\cua\escalation\__init__.py          0      0      0      0   100%
src\cua\observability\__init__.py       0      0      0      0   100%
src\cua\policy\__init__.py              0      0      0      0   100%
src\cua\replay\__init__.py              0      0      0      0   100%
src\cua\surface\__init__.py             0      0      0      0   100%
src\cua\surface\_cdp.py               116      8     24      9    88%   105, 127, 131, 134, 137, 139, 181, 205, 215->217
src\cua\surface\base.py                56      0      8      0   100%
src\cua\surface\desktop.py             18      0      0      0   100%
src\cua\surface\web.py                356     13    102     12    94%   190->194, 208-210, 371->387, 394, 405->415, 407->415, 411->415, 490-491, 556-559, 612, 634, 648->654, 714->713, 718->714, 728
src\cua\target_app\__init__.py          0      0      0      0   100%
src\cua\target_app\__main__.py          8      1      2      1    80%   23
src\cua\target_app\app.py             125      3     40      3    96%   65, 152, 183
src\cua\target_app\config.py           31      0      0      0   100%
src\cua\target_app\faults.py           29      1      4      1    94%   67
src\cua\target_app\models.py           92      0     10      0   100%
src\cua\target_app\state.py            28      0      6      1    97%   58->60
src\cua\target_app\workflow.py         76      3     42      2    96%   93-94, 114
-------------------------------------------------------------------------------
TOTAL                                1907     31    434     35    97%
Required test coverage of 80% reached. Total coverage: 97.10%
======================= 194 passed in 69.41s (0:01:09) ========================
```

## Hostile-surface assessment

- Nested frames: tested across all four named fixture frames; frame scope is explicit. Unnamed/duplicate-titled and cross-origin out-of-process frames still need qualification.
- Table layouts and repeated rows: Savings is selected semantically inside its row on both row orders, with ambiguity rejected.
- Non-semantic controls: both javascript-href actions and span-role buttons pass actionability checks and execute through the same port.
- Missing IDs and changing classes: semantic locators avoid those attributes, but the temporary CDP-node-to-CSS bridge is the brittle piece. It would fail for closed shadow roots/custom embedded widgets and must be replaced or extended for those products.
- Tenant variation: original accessible names stay distinct, and equivalent normalized shapes are tested. Automatic cross-tenant reconciliation is deliberately still a later layer; the fixture no longer grants canonical aliases.
