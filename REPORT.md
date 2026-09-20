# Design report

TODO: document the implemented artifact schema, deterministic replay, error taxonomy,
human control transfer, safety guardrails, field provenance, and tamper-evident evidence.

## 6. Known limits

- Attempts 001, 004, 005, 006, 007, 008, and 009 exposed details only to operator inspection:
  incomplete failure finalization, an inaccurate completion manifest, generic surface errors,
  stale evidence references, and privacy leakage detectable only by an external scan. The attempt
  table in `docs/discovery-attempts.md` identifies each observability gap.
- Prompt template v1 was edited in place during attempt 4. Attempt 010 remains reproducible, but
  attempts 001 through 009 lack both immutable historical template bytes and commit-safe AX inputs,
  so their prompt hashes cannot be independently re-rendered from this checkout.
