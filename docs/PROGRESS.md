# Final build progress

Resume rule: read this file and `AGENTS.md`, then continue from the first part whose status is not
`done`. Do not redo completed parts. Before finishing any part, run `scripts/check.ps1` (Windows)
or `scripts/check.sh` (Linux/macOS), run `scripts/secret_scan.py`, update this file, commit, and
push without force.

| Part | Status | Completion commit | Open issues |
|---|---|---|---|
| 1. Manifests, demo index, dependency cleanup | in progress | — | Implement items 1.1–1.11; regenerate committed manifests from journals. |
| 2. Compiler, verification, approval | pending | — | — |
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
