# Luwu Implementation Status

Status: M1 verified

This document records what the repository actually implements. It does not expand the product scope in [product.md](product.md), and it does not replace the contracts in [reference.md](reference.md).

## M1: Developer Confidence Preview

Goal: let a developer exercise a complete, isolated loop for explicitly declared local template and symbolic resources:

```text
manifest -> inspect/plan -> explicit apply -> post-apply verification
```

The milestone is complete only when the implementation, tests, example, and documentation all agree and the verification commands are recorded below.

### Implemented in this change

- [x] Python package and `luwu` CLI entry point.
- [x] Versioned TOML manifest with suffix-based kind inference and explicit overrides.
- [x] Local `.j2` template rendering and source symlink reconciliation.
- [x] Rendered-template observation with `in_sync`, `missing`, `formatting`, `drifted`, and `blocked` states.
- [x] Read-only `inspect` and `plan` commands.
- [x] Explicit `apply --yes`, stale-target preflight, symlink refusal, permission preservation, and per-file atomic replacement.
- [x] Post-apply recalculation and metadata-only JSON output.
- [x] Isolated example and tests for the supported and fail-closed paths.

### Not started / exploratory

- provider or rbw integration;
- secret-aware inputs or secret persistence;
- baselines and durable plan records;
- field ownership and controlled reverse sync;
- merge resources, links, copies, structured YAML comparison, and general formatter support;
- multi-file transactional rollback and a broad platform matrix.

## Verification

The implementation and isolated example have been verified on the current POSIX development environment:

```text
uv lock --check
uv run --locked python -m unittest discover -s tests -v  # 32 tests passed
uv run --locked python -m compileall -q src tests
uv build
prek run --all-files
isolated CLI E2E: plan -> apply --yes -> inspect; clean post-apply state
```

The E2E check used a temporary copy of `examples/m1`, confirmed the generated file content and mode `0644`, confirmed it was not a symlink, and confirmed no temporary `.luwu-*` entry remained. No repository example target was mutated.
