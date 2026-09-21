# Discovery attempts

Attempts 001 through 009 retain their hash-chained journals and, where produced, manifests. Their
pre-mask AX blobs are deliberately excluded: those blobs contain synthetic identity data in raw
form, and committing them would contradict the repository's evidence-handling policy. Attempt 001
ended before manifest finalization and therefore has only a journal. Attempt 010 is the first
complete bundle whose AX evidence passed the privacy scan.

| Attempt | What failed or succeeded | What the journal showed | Cause | Detection |
|---|---|---|---|---|
| 001 | The first OpenAI request returned HTTP 400; failure finalization had not yet been implemented. | The journal stops before a terminal manifest could be written. | Code defect | Only the operator could see the provider response and incomplete finalization. |
| 002 | OpenAI rejected a nested `oneOf` in the strict tool schema. | A terminal provider error was recorded after failure finalization was added. | Code defect | Detected by the system. |
| 003 | OpenAI returned a valid decision, but the local response-envelope model rejected provider fields under `extra=forbid`. | The run ended with the adapter validation error. | Code defect | Detected by the system. |
| 004 | The prompt displayed zero remaining budget, the model chose an assertion, and policy denied acting in the root frame. | `ModelDecided` and the named denying `PolicyDecision` are present, but the manifest incorrectly reports completion. | Config and prompt defect | The denial was detected; the inaccurate manifest was found only by operator review. |
| 005 | The model supplied a username reference, but dispatch had no injected value resolver. | The accepted action is followed by a generic `surface_error`. | Code defect | Terminal failure was detected; the missing resolver detail was visible only to the operator. |
| 006 | Search and detail navigation succeeded; reading the balance failed because a scoped target was flattened into an ambiguous unscoped locator. | Earlier actions and the terminal `surface_error` are present. | Code defect | Terminal failure was detected; the locator ambiguity detail was visible only to the operator. |
| 007 | The goal completed and returned the savings balance, but later locator ladders cited stale initial AX evidence. | A successful terminal event coexists with stale evidence references. | Code and evidence defect | Only the operator's provenance audit detected it. |
| 008 | The goal completed with per-observation AX evidence, but persisted AX blobs exposed synthetic name and address fields. | The successful journal does not report an evidence-policy violation. | Code and redaction-design defect | Only the operator's privacy scan detected it. |
| 009 | The model entered the username and clicked Login before supplying the required password. | The run ends in a generic `surface_error`. | Prompt/model-behavior defect | Terminal failure was detected; browser validation detail was visible only to the operator. |
| 010 | The real OpenAI/browser run reached the goal and read the savings balance. | Six decisions and six successful actions end in `goal_reached`; the chain verifies and the privacy scan is clean. | None | Detected and fully represented by the system. |

## Prompt provenance limit

The attempt-4 label change edited `config/prompts/discovery-planner.v1.txt` in place instead of
creating `discovery-planner.v2.txt`. Attempt 010 was recorded after that edit and its six prompt
hashes reproduce from the current v1 template and committed redacted AX blobs. Attempts 001 through
009 name a template file that exists, but their rendered hashes cannot be recomputed from the
committed repository: their AX inputs are excluded for privacy, and the earlier bytes associated
with the reused v1 identifier were not retained. Those historical decisions are therefore
tamper-evident but not fully reproducible from this checkout.


## Part 1 manifest correction and screenshot limit

Each existing manifest (002–010) was regenerated from its verified journal after appending
`EvidenceRelabeled` and `ManifestRegenerated` events. Attempt 001 had no manifest;
its misfiled screenshot was relabeled with an `EvidenceRelabeled` event only. A later, separately journaled
regeneration corrected `evidence_verified` to describe the **published** blobs: false for
002–009 because their pre-mask blobs are excluded, true for 010. Attempts 002 and 003 have null
token counts and null cost, since no provider usage event exists. The goal text in these amended
manifests is labeled `reviewer_reconstruction` in the journal: it comes from the recorded task
request, not a missing original `RunStarted` input field. Original parameter values are not
recoverable and remain absent.

All retained screenshots are legacy full-viewport black images, including discovery-010.
Their index entries now identify them as `screenshot`, `image/png`, and
`redaction=full_viewport`. No original pixels were recovered or recreated. New runs capture
a screenshot at every observation, link it to the AX hash, and mask semantic sensitive regions.
A masked AX node without bounds forces that one screenshot to full-viewport redaction.


The original discovery-010 journal also contains a raw **synthetic** savings balance in its
`ActionResult` event. That record cannot be edited without rewriting its hash chain and Git
history. New discovery runs redact `ActionResult.result.value` by sensitivity before journal
persistence, and a scripted-run regression test enforces the marker shape. The earlier raw
value is a known historical privacy limitation, not evidence that the new path is safe for
production financial data.
