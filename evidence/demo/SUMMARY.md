# Evidence summaries

## 3.1 Goal-driven agent loop

[gpt-6-astra discovery manifest](../discovery-010/manifest.json) and
[journal](../discovery-010/journal.ndjson): six decisions, six accepted actions, goal reached.

## 3.2 Structured artifact

[Approved capability](../../capabilities/look_up_member_savings_balance@1.0.0.json) and
[compiler comparison](../../docs/compiler-vs-handwritten.md): typed steps, outcomes, provenance,
and content-bound approval.

## 3.3 Deterministic replay

[Success](../replay-success/manifest.json), [business outcome](../replay-not-found/manifest.json),
[recovery](../replay-recovered/manifest.json), and
[hard failure](../replay-hard-failure/manifest.json) with no model in replay.

## 3.4 Safety & policy guardrails

[Escalation attempts](../../docs/escalation-attempts.md) record the route-scope and frame-scope
policy defects and their fixes. Unit coverage is in tests/unit/policy/.

## 3.5 Evidence / observability

The [evidence index](README.md) links hash-chained journals, content-addressed blobs, manifests,
and spans. The [attempt ledger](../../docs/discovery-attempts.md) includes failures and visibility.

## 3.6 Human-in-the-loop escalation & handoff

[Discovery handoff](../escalation-discovery/manifest.json) and
[replay handoff](../escalation-replay/manifest.json) retain same-session transfer, named actor,
AX change summary, handback, and resumed execution.

## 3.7 Heterogeneity & scale

[Before override](../cross-tenant-before/manifest.json) records beta drift and a checkpoint
failure. [After override](../cross-tenant-after/manifest.json) succeeds with the content-bound beta
override. See [REPORT.md section 4](../../REPORT.md#4-heterogeneity--multi-tenant).
