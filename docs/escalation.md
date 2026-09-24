# Escalation and same-session handoff

A `SessionLease` answers who controls the live browser independently from LangGraph state. It
follows `RUNNING -> PAUSED -> OPERATOR_CONTROL -> HANDBACK_PENDING -> RUNNING`, with `ABORTED`
for expiry or cancellation. Illegal edges fail closed. LangGraph's SQLite checkpoint restores
graph data; the lease and exact `ControlToken` preserve the browser session.

The minimal FastAPI console lists open requests, shows the latest redacted screenshot, exposes the
loopback CDP endpoint after a named operator takes control, and accepts handback notes. The view
polls by refresh; co-browsing is outside this take-home.

Both successful bundles use actor `scripted-operator`. Discovery pauses on the named
`account.confirm-submit` policy rule, transfers the same CDP session, records the operator click
and AX diff, then resumes so the model can read the confirmation reference. Replay injects an
unhandled condition, transfers the same session, re-observes after handback, rechecks the failed
checkpoint, and resumes without a model.

A human uses the same path: start the operator app, open `/operator`, select the request, choose
**Take control**, attach a browser to the displayed loopback CDP endpoint, perform the reviewed
action, then choose **Hand back** with notes. The polling view is context only; control remains
exclusive through the lease.

See [the attempt ledger](escalation-attempts.md) for the retained failures and the two policy
findings. SQLite checkpoints are deliberately absent from Git because they contain resumable raw
graph state rather than publication-safe evidence.
