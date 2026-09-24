# Staff-engineer self-review

Ranked by likely interview damage, these are the five questions the implementation answers least
well. The review reads the code and retained evidence; it does not treat README claims as proof.

1. **How can an auditor trust a journal head created by the same process that wrote the journal?**
   The chain detects reordering and editing against its local head, but an attacker able to rewrite
   both can create a new valid chain. External signed anchoring remains a cut in REPORT.md.

2. **Why did sensitive synthetic output ever enter a historical journal?** Discovery-010 predates
   output-aware ActionResult redaction and contains one raw synthetic balance in Git history. New
   writes use the observability redaction choke point, and publication tests cover current bundles.
   Rewriting public history would cause more harm and was not authorized.

3. **What guarantees that tenant aliases remain correct after either tenant changes?** Overrides
   bind the exact base capability digest and can change only targets and checkpoints, but the beta
   aliases are named human edits rather than compiler output from a recorded beta discovery.
   Re-recorded tenant observations and expiry policy are the next production step.

4. **Could a model still influence replay?** No LLM client is reachable from replay, but a model
   influenced the artifact before approval. Content-bound approval, source observations, strict
   provenance, and deterministic verification are the controls. They establish reviewability, not
   that the original model intent was correct.

5. **What happens if a catalog model invents a plausible tool name?** Before this review, lookup
   leaked an incidental StopIteration and normalized name collisions were unchecked. The catalog
   now rejects collisions at construction and raises an actionable error for an unknown selection.
   A production API should turn that into a typed caller failure and journal it.

## Classification audit

Business detectors run before checkpoints, so missing and restricted members return declared
BusinessOutcome values. Recoverable states are journal events and never terminal caller variants.
The main residual risk is a UI wording change that defeats its detector and becomes a checkpoint
HardFailure; fingerprint and drift evidence expose this, but do not infer intent.

## Sensitive-data audit

Current journal adapters redact action values and extracted results; manifests use declared
sensitivity, screenshots mask AX bounds, and secret inputs never enter model-visible catalog
schemas. Historical discovery-010 and excluded pre-mask blobs remain documented limits. The
catalog has an explicit method capable of sending tool output to a provider, but the committed
demo does not call it; any production caller must authorize egress from OutputSpec sensitivity.

## Determinism audit

Replay has fixed ordering, explicit settle strategies, bounded retries, canonical serialization,
and no model. Browser scheduling, host performance, random run IDs, and an evolving external UI
remain nondeterministic. Locale-sensitive target text is avoided where possible, timestamps are
fixed in the fixture, and all runtime variation is evidence rather than artifact content.

## Deletion candidates

The unused live catalog answer path should be retained only if provider egress is explicitly
authorized and journaled; otherwise remove it. The desktop surface remains a deliberate protocol
stub. The operator console is intentionally minimal. These boundaries are named cuts rather than
claims of production completeness.
