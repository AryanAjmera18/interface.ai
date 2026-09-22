# Final build progress

Resume rule: read this file and `AGENTS.md`, then continue from the first part whose status is not
`done`. Do not redo completed parts. Before finishing any part, run `scripts/check.ps1` (Windows)
or `scripts/check.sh` (Linux/macOS), run `scripts/secret_scan.py`, update this file, commit, and
push without force.

| Part | Status | Completion commit | Open issues |
|---|---|---|---|
| 1. Manifests, demo index, dependency cleanup | done | d3f716a | Historical attempts 001–009 lack published blobs; attempt 010's old journal contains a raw synthetic balance. Both are documented, not rewritten. |
| 2. Compiler, verification, approval | done | 0c9a9f4 | Original URL/fingerprint omission is preserved as a labeled fixture reconstruction; future observations record both directly. |
| 3. Deterministic replay | pending | — | — |
| 4. Escalation and handoff | pending | — | — |
| 5. Stretch goals | pending | — | Exactly two: cross-tenant reuse and agent-facing catalog. |
| 6. HTML run report | pending | — | — |
| 7. README and REPORT.md | pending | — | Mermaid rendering depends on local mermaid-cli availability. |
| 8. Self-review and submission readiness | pending | — | Fresh-clone check and final GitHub Actions verification required. |

## Global constraints and accounting

- Stop after three consecutive live-model failures.
- Stop if cumulative live-model spend for this final-build prompt exceeds USD 3.00.
- Current final-build live spend: USD 0.00; no live run has been made in this prompt.
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
