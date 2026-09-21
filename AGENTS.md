# Binding repository instructions

- Full type hints everywhere; `mypy --strict` must pass on src/.
- Pydantic v2 models for anything serializable. No bare dicts cross module boundaries.
- domain/ contains no I/O. Side effects go behind Protocols declared in domain/ports.py.
- domain/, policy/, and replay/ must not import langchain, langgraph, langsmith, playwright, or fastapi. Those layers are framework-free so the graded core stays independently testable and the framework stays swappable.
- Never log, export, or persist a raw field value without passing it through observability.redaction.redact(). This is a hard rule — the domain is regulated financial data, and third-party exporters (LangSmith) count as egress.
- Structured logging only (structlog, JSON renderer). No print().
- Every behavioral change ships with a test in the same commit. Error paths are tested, not just happy paths.
- No live network calls in the default test suite. Live-model tests are marked `@pytest.mark.live` and excluded from CI.
- Conventional commits. Small, reviewable commits.
- When a design decision is non-obvious, record the reasoning in a docstring at the decision site, not in a separate doc nobody reads.
- Never edit goldens by hand. Regenerate only through `pytest --update-goldens` (or `make update-goldens`); never regenerate in reaction to a red test without stating in the commit message which intentional behavior or schema change caused the drift. Frozen historical schemas and their manifests must not be rewritten by this flag.
- Prompt templates are append-only. Any content change creates a new versioned file and prompt_template_id; never edit a template referenced by a journal in place because its hash is provenance.
- At the end of every final-build part, run the full gate and secret scanner before committing and pushing. Never push first, and never force-push.
