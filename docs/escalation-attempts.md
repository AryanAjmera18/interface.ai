# Escalation evidence attempts

All actors that exercised operator control are recorded as `scripted-operator`. Discovery used a
real OpenAI planner; replay used no model. SQLite checkpoints contain raw graph state and are
runtime-only, so `.gitignore` excludes them from every published bundle.

| Bundle | Result | What happened | Detection |
|---|---|---|---|
| `escalation-discovery-attempt-001` | Premature success | Any `ReadValue` ended the graph, so an intermediate nickname read was mistaken for completion. Retained locally because its pre-fix metadata did not pass publication review. | The manifest said success; only goal-versus-journal review exposed the defect. |
| `escalation-discovery-attempt-002` | Hard failure | LangGraph returned `__interrupt__` beside domain state and strict Pydantic rejected the framework envelope. | The exception and hard-failure manifest identified the boundary. |
| `escalation-discovery-attempt-003` | Unsafe success | The irreversible policy route named a nonexistent legacy path, so the broad safe-click rule permitted submission. | Named `PolicyDecision` events made the wrong rule visible during evidence review. |
| `escalation-discovery-attempt-004` | Unsafe success | Policy evaluated the stable outer frameset URL instead of the content iframe URL, again missing the irreversible rule. | The journal paired a safe-click rule with an unchanged outer URL; review exposed the scope defect. |
| `escalation-discovery-attempt-005` | Hard failure | The planner repeated a completed deposit action because `PREVIOUS_RESULT` was always `none`; dead-end escalation transferred control before the scripted operator rejected the non-confirmation page. | Dead-end detection and the operator failure were journaled. |
| `escalation-discovery` | Success | Frame-scoped policy escalated `account.confirm-submit`; the scripted operator submitted in the same CDP session, handed back, and the model read the reference. | The chain records escalation, transfer, human action, AX diff, resume, and success. |
| `escalation-replay-attempt-001` | Hard failure | The scripted operator advanced beyond the failed step checkpoint; replay refused to guess. | The checkpoint recheck failed deterministically. |
| `escalation-replay` | Success | The operator restored exactly the failed checkpoint in the same session; replay re-observed and resumed without a model. | The chain records the transfer, AX diff, resumed steps, and success. |

Two safety findings changed the implementation. Irreversible route rules now match the concrete
`/t/*/ui/review` and beta `/t/*/ui/extra` pages, with a self-check against the target fixture.
Policy also selects the URL of the frame that owns the target. A frameset's stable top-level URL
cannot classify an action occurring inside a changing content frame.

The successful discovery manifest reports 58,873 input tokens, 3,091 output tokens, 580 reasoning
tokens, USD 0.74328, one escalation, and a 119.4-second wall clock. Across all six retained
Part 4 discovery attempts, recorded spend was USD 3.583646. The budget override prohibits any
further gpt-6-astra discovery run.
