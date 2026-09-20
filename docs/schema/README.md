# Domain schema v1

Run `uv run cua schema emit` from the repository root. Emission writes public schema metadata
only. Loading a capability is pure: pass JSON text to `load_capability` plus a Clock and
IdGenerator; reading files and obtaining real time/IDs belong to callers outside domain/.

## Shared provider representation

The artifact uses one canonical JSON format. `on_outcome` is a list of typed entries, serialized
in code order; its cached mapping view is read-only, and the underlying list rejects mutations.
Parameters accept tagged scalar literals only. There are no open-keyed objects in capability or
tool-input schemas. Results and journal event payloads are deliberately exempt.

All properties are required in emitted tool schemas; nullable values encode absence. Python
constructors retain ergonomic defaults, but canonical serialization always writes the fields.
Action tools use an object envelope `{action: ...}` because a root union is not supported by
OpenAI strict schemas. Every object union has a disjoint literal tag. Optional/null alternatives
are not extra polymorphic variants. Pydantic's disjoint `oneOf` variants become equivalent
`anyOf` variants; schema annotations are removed without deleting fields named `default`.

The same schema is placed in OpenAI `function.parameters` and Anthropic `input_schema`.
Offline tests independently check schema structure and validate the emitted subset, then parse
the same JSON through the same domain models. These are not claims of live-provider acceptance.
Cross-field invariants (outcome references, provenance, risk/retry rules) remain mandatory domain
validation after either provider returns JSON; neither provider grammar proves evidence truth.

Sources: [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
and [Anthropic structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).

## Version freeze

The default suite compares every emitted schema to this directory and its separately pinned
SHA-256 in `tests/golden/schema-digests.v1.json`. Regenerating schema files alone fails CI.
After publication, do not rewrite the v1 manifest to accommodate a changed schema: add a new
schema version, retain v1 snapshots, and register the corresponding migration. The current v1
registry entry is an explicitly recorded identity/validation pass, not a fictional upgrade to v2.
Migration records live in the load result so simply reading an artifact does not rewrite it.

## Golden evidence

`tests/golden/capability.v1.json` is a synthetic schema example, not a discovered or approved
banking capability. Its fake model metadata and prompt are test data. Both tenant AX fixtures
are normalized transcriptions of the Stage 1 search surface. The locator evidence hash is the
SHA-256 of the committed alpha fixture bytes; step provenance also binds the normalized AX hash.
The tool-schema hash binds the shared ActionInput schema. Fixed Clock/IdGenerator implementations
in tests make recompilation byte-stable; domain code never reads the clock or generates IDs.

Name normalization folds Unicode compatibility forms, case, punctuation and whitespace.
Semantic aliases such as Member ID / Account Holder Number must be explicitly listed in
`one_of`; normalization never invents them. Regex patterns are data, but compilation does not
guarantee bounded matching cost; only reviewed patterns should enter approved artifacts.

## Model profiles

`config/models.yaml` binds all three roles to the fake provider. CLI configuration loading
accepts `CUA_MODEL_DISCOVERY_PLANNER`, `CUA_MODEL_EXTRACTOR`, and `CUA_MODEL_CATALOG_AGENT`.
Each override is a complete JSON ModelRef, for example:

```json
{"kind":"model","provider":"fake","model_id":"deterministic-v1","api_flavor":"offline","structured_output_mode":"json_schema"}
```

Replacing identity atomically prevents a provider override from retaining another provider's
model ID or API flavor. Configuration loading does not instantiate a client or call a network.
DecisionResult requires a typed action only for completed decisions; refusal, truncation, and
error require `action:null` so unsuccessful model calls cannot smuggle executable actions.

## Stage 2 patch: v2 compatibility decision

The original v1 snapshots and SHA manifest are frozen and retained. Recursive AxTarget adds
required nullable fields, parameters reject more inputs, and StepTiming gains explicit polling
and stability windows. Therefore the capability, action, and decision-result schemas advance to
v2; unchanged schemas retain their versions. Do not re-emit changed bytes under capability.v1.
The v1-to-v2 migration adds scope/timing defaults, preserves historic positional targets, and
invalidates approvals over old bytes. Inputs violating stricter sensitivity rules fail validation
and need an explicit human edit; migration never silently removes sensitive fields.

The current golden is capability.v2.json, with a second approved variant. Both describe the
hand-authored synthetic savings flow, not a discovered successful run. The prior one-step v1
artifact remains as a migration fixture. Stage 7 must implement ProvenanceVerifier before any
claim of verified attribution. Tests preserve all original v1 schema hashes independently.

## Explicit regeneration and content-bound approval

Intentional change for the commit message: `fix(domain): bind approval to canonical content and
regenerate v2 fixtures`. Approval excludes exactly /status and /provenance/approval from the
canonical digest. Current v2 fixtures were regenerated with `make update-goldens`; every rewrite
reported its filename and byte delta. No git commit is implied by this note.

The v2 schema is the current candidate; historical v1 snapshots remain frozen. Default tests
assert all snapshots. `--update-goldens` is an explicit authoring operation, never CI behavior.
An approved v1 migration now removes approval but retains approved status so validation raises
StaleApprovalError. It does not silently downgrade to candidate; a caller must explicitly change
status and obtain a fresh review. This supersedes the earlier migration description above.
