# CUA

## Overview

Computer-use capability discovery, typed artifacts, deterministic replay, and local evidence.

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```powershell
git clone https://github.com/AryanAjmera18/interface.ai.git
cd interface.ai
uv sync --locked
uv run --locked playwright install chromium
$env:OPENAI_API_KEY = "<your OpenAI API key>"
uv run --locked cua doctor
```

Existing process environment variables take precedence over `.env`. Doctor reports credential
presence only and never prints values, prefixes, or lengths. Default tests use fake models and
exclude live tests.

Run the repository gate with the script for your platform:

```powershell
./scripts/check.ps1
```

```sh
./scripts/check.sh
```

## Demo path

With setup complete, start the synthetic bank fixture in one terminal:

```powershell
uv run --locked python -m cua.target_app
```

In a second terminal, confirm readiness and run discovery:

```powershell
uv run --locked cua doctor
uv run --locked cua discover --goal "look up member 10023 and read their current savings balance" --target http://127.0.0.1:8099 --tenant alpha --param username=reviewer --param password=reviewer
```

The command prints `run_id` and the exact `manifest` path. Set `$bundle` to the containing
directory and verify its journal:

```powershell
$bundle = "evidence/<run_id printed by cua discover>"
uv run --locked python -c "import json; from pathlib import Path; from cua.observability.journal import verify_chain_directory; p=Path(r'$bundle'); run_id=json.loads((p/'manifest.json').read_text())['context']['run_id']; print(verify_chain_directory(p, run_id).model_dump_json())"
```

**Replay placeholder (Stage 8):** deterministic replay commands will be added after the replay
executor is implemented.

## Design

See [REPORT.md](REPORT.md). Binding implementation rules are in [AGENTS.md](AGENTS.md).

## Evidence

New runs are written below `evidence/<run_id>/`. Attempts 001–009 publish journals, manifests,
head anchors, indexes, and local span summaries while excluding their pre-mask AX blobs. Attempt
010 is the first complete bundle with redacted AX evidence. See
[docs/discovery-attempts.md](docs/discovery-attempts.md) for the attempt history.
