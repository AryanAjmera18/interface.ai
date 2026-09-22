# Compiler output compared with the Stage 2 hand-authored artifact

The hand-authored reference is `tests/golden/capability.v2.json`. It demonstrated the schema;
the compiled artifact is `capabilities/look_up_member_savings_balance@1.0.0.json` and is derived
from discovery-010.

- The compiled artifact has six steps, including the synthetic login and the observed search and
  member-detail navigation. The reference assumed an authenticated session and had three steps.
- The compiler retains every model decision ID, prompt hash, observation hash, evidence reference,
  and provider-neutral model identity from the journal. The reference used illustrative compiler
  provenance.
- Member `10023` is replaced by `member_id` and member-specific accessible names become explicit
  regular expressions. Credential values are absent; the login binding is a `secret_ref`.
- Locator ladders contain semantic, label, AX-path, and structural-path candidates when the AX
  snapshot supports them. No coordinate candidate is emitted because the historical AX blobs have
  no bounds. The reference used a smaller illustrative ladder.
- The read extractor remains scoped to the Savings row inside the accounts table. Standard target
  outcomes are attached to every step with business, bounded recovery, or hard handling.
- Risk is reproduced from the recorded policy rule: form entry is reversible and read/navigation
  actions are safe. Model-suggested risk does not participate.
- The original run omitted URL and fingerprint fields. The artifact consumes the explicitly
  journaled `TargetMetadataReconstructed` event derived from the target fixture at commit
  `9c3c4fe`; this is reconstruction evidence, not a claim that the browser observed those fields.
- Approval is a later named journal event and is bound to the canonical content digest. Status and
  the approval record are the documented digest exclusions.
