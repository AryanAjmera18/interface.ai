# Demo evidence index

All paths are relative to this file. The target application contains synthetic data. A scenario is
marked **done** only when its linked bundle exists and its journal verifies.

| Scenario | Status | Evidence |
|---|---|---|
| 1. Discovery: real model learns savings lookup | Done | [discovery-010 manifest](../discovery-010/manifest.json), [journal](../discovery-010/journal.ndjson), [AX and screenshot index](../discovery-010/index.json) |
| 2. Compile: journal becomes capability | Done | [approved artifact](../../capabilities/look_up_member_savings_balance@1.0.0.json), [comparison](../../docs/compiler-vs-handwritten.md) |
| 3. Replay success without a model | Done | [manifest](../replay-success/manifest.json), [journal](../replay-success/journal.ndjson) |
| 4. Replay business outcome: member not found | Done | [manifest](../replay-not-found/manifest.json), [journal](../replay-not-found/journal.ndjson) |
| 5. Replay recovery: dismiss interstitial | Done | [manifest](../replay-recovered/manifest.json), [journal](../replay-recovered/journal.ndjson) |
| 6. Replay hard failure with debug payload | Done | [manifest](../replay-hard-failure/manifest.json), [journal](../replay-hard-failure/journal.ndjson) |
| 7. Same-session human escalation | Done | [discovery handoff](../escalation-discovery/manifest.json), [replay handoff](../escalation-replay/manifest.json), [attempt ledger](../../docs/escalation-attempts.md) |
| 8. Cross-tenant replay with override | Pending | Part 5 |
| 9. Catalog agent discovers and invokes capability | Pending | Part 5 |

The [discovery attempt history](../../docs/discovery-attempts.md) explains why attempts
[001–009](../discovery-001/journal.ndjson) are retained. Their failed runs are evidence of what
the system diagnosed and which defects were visible only to an operator. The pre-mask blobs for
those attempts were deliberately excluded; their manifests now report
`evidence_verified: false` for the published bundle.

Discovery-010 ended at `goal_reached` after six model decisions and six accepted actions. Its
manifest reports 18,090 input tokens (2,742 cached), 1,152 output tokens (112 reasoning tokens
already inside output), and $0.213822 under the dated post-run pricing entry. The savings-balance
output is a redacted marker with a SHA-256 digest; the manifest contains no raw balance.
The original journal predates output sensitivity masking and contains the raw **synthetic**
balance in one `ActionResult`. This is documented as a historical limit; no real PII exists
in the fixture.

From the repository root, verify the chain and every indexed blob:

```powershell
uv run --locked python -c "import json,hashlib; from pathlib import Path; from cua.observability.journal import verify_chain_directory; p=Path('evidence/discovery-010'); m=json.loads((p/'manifest.json').read_bytes()); print(verify_chain_directory(p,m['context']['run_id']).model_dump_json()); print(all(hashlib.sha256((p/'blobs'/e['sha256'][:2]/e['sha256']).read_bytes()).hexdigest()==e['sha256'] for e in m['evidence_index']['entries']))"
```

The historical screenshot is a blank full-viewport PNG. Its `redaction` metadata says so;
region-masked screenshots apply only to runs recorded after the Part 1 implementation.

Scenario 2 compiles to six steps and verifies every observation and evidence digest. Run `uv run cua verify capabilities/look_up_member_savings_balance@1.0.0.json` from the repository root.


Replay evidence is described in [docs/replay.md](../../docs/replay.md). Every canonical replay
manifest binds capability `01M2YKF25VVM1RVNVNV97TG07H`, content digest
`234a5cc673d858de22fff897edc269af902b0d2f8b2a98d447f1edcbbdc9e4fa`, and discovery parent
run `01M2YKF25VVM1RVNVNV97TG07H`. Each chain and indexed evidence set verifies.
