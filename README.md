# CUA

## Overview

Computer-use capability discovery, typed artifacts, deterministic replay, and local evidence.

## Setup

Requires Python 3.12, uv, and GNU Make.

```sh
make install
uv run pre-commit install
make lint typecheck test
```

Copy `.env.example` to `.env` when configuring a provider. Default tests inject fake models and
exclude `live`; `make test-live` explicitly selects live-model tests. Local file traces remain the
offline default. The CLI is excluded from coverage; other discovery logic remains subject to the
80% gate.

Run this first when reviewing a configured checkout:

```sh
uv run cua doctor
```

The CLI and explicitly selected live tests load `.env`; existing process environment variables
take precedence. Doctor reports credential presence only and never prints values, prefixes, or
lengths.

## Demo path (TODO)

`make demo` identifies `evidence/demo/`. The local app and recorded demo are pending.

## Design

See [REPORT.md](REPORT.md). Binding implementation rules are in [AGENTS.md](AGENTS.md).

## Evidence

Runtime output belongs in `evidence/` and is ignored by Git. `evidence/demo/` is reserved for
the committed, redacted demo bundle. Field provenance and a tamper-evident journal are required
in later implementation work; this scaffold does not produce evidence.
