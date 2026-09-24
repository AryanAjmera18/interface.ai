# Final build progress

Resume rule: read this file and `AGENTS.md`, then continue from the first part whose status is not
`done`. Do not redo completed parts. Before finishing any part, run `scripts/check.ps1` (Windows)
or `scripts/check.sh` (Linux/macOS), run `scripts/secret_scan.py`, update this file, commit, and
push without force.

| Part | Status | Completion commit | Open issues |
|---|---|---|---|
| 1. Manifests, demo index, dependency cleanup | done | d3f716a | Historical attempts 001–009 lack published blobs; attempt 010's old journal contains a raw synthetic balance. Both are documented, not rewritten. |
| 2. Compiler, verification, approval | done | 0c9a9f4 | Original URL/fingerprint omission is preserved as a labeled fixture reconstruction; future observations record both directly. |
| 3. Deterministic replay | done | db17e64 | Pre-fix failed evidence was withheld after the privacy scan found raw fixture values; the published reruns are redacted and verified. |
| 4. Escalation and handoff | done | 2aeeca9 | Attempt 001 remains local because it predates corrected completion metadata; attempts 002–005 document the subsequent code, config, policy-scope, and prompt defects. |
| 5. Stretch goals | done | 898df05 | Cross-tenant override and one-call Luna catalog evidence are complete. |
| 6. HTML run report | skipped | — | Cut by the budget and speed override; document in REPORT.md. |
| 7. README and REPORT.md | done | documentation commit | Four requested diagrams are syntax-reviewed but not locally rendered. |
| 8. Self-review and submission readiness | pending | — | Fresh-clone check and final GitHub Actions verification required. |

## Global constraints and accounting

- Stop after three consecutive live-model failures.
- Stop if cumulative live-model spend for this final-build prompt exceeds USD 3.00.
- Current final-build live spend: USD 3.583646 across six retained Part 4 discovery attempts. The budget override prohibits further gpt-6-astra runs; only the capped Part 5 Luna catalog call remains allowed.
- Never rewrite Git history or force-push.
- Every scripted operator action must be labeled `scripted-operator` in journals and docs.
- Early attempts 001–009 and their failure evidence remain intentionally preserved.


## Part 1 verification

- Implementation commit: `d3f716a`.
- Full gate: 222 fast tests passed, 92% coverage, seven import contracts kept; lint,
  format, and strict mypy passed.
- Browser lane: 67 passed, two skipped (no external services).
- Full-history scanner: five exact allowlisted findings, zero failures and zero tracked-file
  failures.
- Discovery manifests 002–010 were rebuilt from their hash-chained journals. Attempts 002–003
  report null usage and cost. Attempt 001 has no manifest; its screenshot metadata was relabeled
  through a journal event.
- No live model run was made for Part 1; cumulative final-build live spend remains USD 0.00.

## Part 2 verification

- Implementation commit: `0c9a9f4`.
- Full gate: 225 fast tests passed, 92% coverage, seven import contracts kept; lint,
  format, and strict mypy passed.
- Full-history scanner: five exact allowlisted findings, zero failures and zero tracked-file
  failures.
- The approved six-step artifact content digest is
  `234a5cc673d858de22fff897edc269af902b0d2f8b2a98d447f1edcbbdc9e4fa`.
- `cua verify` passed the journal chain, every observation link, every referenced evidence blob,
  deterministic recompilation, and the journaled content-bound approval.
- No live model run was made for Part 2; cumulative final-build live spend remains USD 0.00.
## Part 3 verification

- Implementation commit: `db17e64`.
- Full gate: 231 fast tests passed, 92% coverage, seven import contracts kept; lint,
  format, and strict mypy passed.
- Integration lane: 78 passed and two skipped, including eleven replay evidence checks.
- Full-history scanner: five exact allowlisted findings, zero failures and zero tracked-file
  failures. Replay journal, manifest, and span metadata had zero raw fixture-value matches.
- Eight real-browser bundles cover success, two business outcomes, two bounded recoveries,
  a hard server failure, slow load, and invalid input. Every manifest binds the discovery run,
  capability ID, and exact approved content digest.
- No live model run was made for Part 3; cumulative final-build live spend remains USD 0.00.


## Part 4 verification

- Implementation commit: `2aeeca9`.
- Full gate: 246 fast tests passed, 92% coverage, seven import contracts kept; lint,
  format, and strict mypy passed.
- Escalation evidence lane: six non-live integration tests passed, including chain integrity,
  named actor attribution, AX change summaries, and publication privacy checks.
- Full-history scanner: five exact allowlisted findings, zero failures and zero tracked-file
  failures. Checkpoints, browser profiles, lock files, and pre-fix attempt 001 are excluded.
- The successful discovery run recorded one irreversible policy escalation, same-session CDP
  transfer, scripted operator action, handback, re-observation, and resumed completion. The
  replay handoff exercised the framework-free escalation port and also resumed successfully.
- Part 4 retained live spend is USD 3.583646. The budget override prohibits further Astra
  discovery runs; no live call was made during this close-out.


## Budget-override execution checkpoints

- Step 1 complete: Part 4 closed, gated, scanned, committed, and pushed.
- Step 2 complete: the required replay success, not-found, recovered, hard-failure, and
  escalation-replay bundles were already present and tracked. Offline verification reported
  `intact=true` for all five, so no evidence rerun was needed.
- Live spend during these checkpoints: USD 0.00.

## Part 5.1 verification

- Cross-tenant replay without an override failed at the first alpha-specific heading checkpoint;
  evidence/cross-tenant-before/ retains that intact, typed hard-failure run.
- The beta override may change only locator ladders and checkpoints, carries named human edits,
  and is bound to approved base digest
  234a5cc673d858de22fff897edc269af902b0d2f8b2a98d447f1edcbbdc9e4fa.
- Cross-tenant replay with the override completed all six steps; the journal chain and indexed,
  redacted evidence in evidence/cross-tenant-after/ verify.
- cua verify passed the override's parent identity and content-digest binding.
- Full gate: 250 fast tests passed, 92% coverage, seven import contracts kept; lint, format, and
  strict mypy passed. Fourteen focused override and evidence tests also passed.
- Full-history scanner: five exact allowlisted findings, zero failures and zero tracked-file
  failures.
- No live model call was made; budget-override live spend remains USD 0.00.


## Part 5.2 verification

- The registry exposes approved capabilities only and derives strict function schemas from
  ParamSpec. Secret-resolved inputs stay local and are absent from model-visible tools.
- One gpt-5.6-luna call selected look_up_member_savings_balance with typed member_id input.
  The linked deterministic replay succeeded without a model in its decision loop.
- The call used 78 input and 24 output tokens, no cached or reasoning tokens, and cost
  USD 0.0000444 from the dated pricing entry. It succeeded on attempt one.
- The safer one-call boundary kept the raw financial result local. The catalog question,
  arguments, tool result, and replay publication are redacted in evidence/catalog-invoke/.
- Full gate: 253 fast tests passed, 92% coverage, seven import contracts kept; lint, format,
  and strict mypy passed. The focused catalog and evidence lane passed 21 tests.
- An initial post-gate commit command incorrectly continued after the scanner found a new
  catalog Authorization construction. The follow-up centralizes that construction, records the
  already-published occurrence as an exact history-only exception, and restores zero failures.
- Budget-override live spend is USD 0.0000444.


## Part 7 verification

- README covers requirements, architecture, quickstart, the full evidence-backed demo path,
  offline operation, artifact and result contracts, provenance, safety, testing, layout, and FAQ.
- It contains exactly four GitHub-compatible Mermaid blocks: system architecture, capability
  lifecycle, replay decision, and escalation state machine.
- REPORT.md has exactly the seven requested headings in order and 956 words. Safety includes both
  policy defects exposed by escalation attempts. Cuts includes the explicit HTML-viewer cut.
- Mermaid CLI was not installed locally, so diagram rendering must be checked on GitHub.
- No live model call was made; budget-override live spend remains USD 0.0000444.

## Submission requirement map

| Brief section | Code | Evidence |
|---|---|---|
| 3.1 target application | src/cua/target_app/ | tests/integration/target_app/ |
| 3.2 discovery loop | src/cua/discovery/graph.py | evidence/discovery-010/ |
| 3.3 capability schema | src/cua/domain/capability.py | capabilities/ and docs/schema/ |
| 3.4 deterministic replay | src/cua/replay/executor.py | evidence/replay-success/ and replay-hard-failure/ |
| 3.5 human escalation | src/cua/escalation/ | evidence/escalation-discovery/ and escalation-replay/ |
| 3.6 guardrails | src/cua/policy/ and observability/redaction.py | policy tests and escalation attempts |
| 3.7 provenance and observability | src/cua/observability/ | journals, manifests, blobs, and spans under evidence/ |
| 6 deliverables | README.md, REPORT.md, evidence/ | evidence/demo/README.md |

Required root paths README.md, REPORT.md, and evidence/ exist. The evidence tree contains the
approved example artifact by link, discovery logs, replay logs, business and hard error runs,
cross-tenant before/after runs, escalation, and the catalog invocation.

## Fresh-clone finding

The first Windows fresh clone failed nine byte-stability checks because Git converted committed
JSON and golden files to CRLF. Root .gitattributes now pins text checkout to LF while preserving
binary screenshots. This is a repository portability fix; no golden content was regenerated.
