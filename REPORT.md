# Design report

TODO: document the implemented artifact schema, deterministic replay, error taxonomy,
human control transfer, safety guardrails, field provenance, and tamper-evident evidence.

## Safety findings from control transfer

- Irreversible rules initially named a route the fixture never served. The broad safe-click rule therefore matched. Policy configuration now self-checks the concrete review and beta-extra routes.
- The frameset kept its outer URL stable while the content frame navigated. Policy now classifies an action with the URL of the frame that owns its target.

## 6. Known limits

- Attempts 001, 004, 005, 006, 007, 008, and 009 exposed details only to operator inspection:
  incomplete failure finalization, an inaccurate completion manifest, generic surface errors,
  stale evidence references, and privacy leakage detectable only by an external scan. The attempt
  table in `docs/discovery-attempts.md` identifies each observability gap.
- Prompt template v1 was edited in place during attempt 4. Attempt 010 remains reproducible, but
  attempts 001 through 009 lack both immutable historical template bytes and commit-safe AX inputs,
  so their prompt hashes cannot be independently re-rendered from this checkout.


- The hand-built OpenAI httpx transport remains a cut. It now has an explicit timeout and bounded
  429/5xx retries, but migrating to the official OpenAI SDK would reduce owned transport behavior.
- Attempt 010's old journal includes a raw synthetic balance. New `ActionResult` writes use
  the sensitivity-aware redaction choke point, but the old Git history cannot be retroactively
  made private without a separately authorized history rewrite.
