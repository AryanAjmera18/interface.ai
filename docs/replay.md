# Deterministic replay evidence

`cua replay` loads an approved capability, validates inputs before acting, and executes its
ordered action, locator, outcome, and checkpoint data. The executor accepts no LLM client and
has no model fallback. Draft artifacts require `--allow-draft`; irreversible actions still need
all policy gates, including `--allow-irreversible`.

Each manifest records the discovery parent run, capability ID, and approved content digest.
`LocatorResolved` records the winning candidate and all misses; `DriftObserved` records fallback
or fingerprint mismatch. Outcome detectors run before checkpoints. Business outcomes return
normally, bounded dismiss and reauthentication recoveries emit `Recovered`, and hard failures
retain the failed step, expectation, observed state, trace, journal head, and evidence reference.

The published real-browser evidence includes success, not-found, permission-denied,
interstitial recovery, session reauthentication, server failure, slow load, and invalid input.
An earlier not-found attempt exposed an initial AX capture that did not retry a destroyed frame
context. Its pre-fix evidence contained raw fixture values, so it is intentionally withheld. The
adapter now retries that transient capture within the existing settle deadline, and executor
exceptions become typed failures.

Verify any bundle by reading its run ID from `manifest.json` and calling
`verify_chain_directory(bundle, run_id)`. The manifest's `evidence_verified` must also be true.
