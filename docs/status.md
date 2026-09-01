# Luwu Implementation Status

Status: M1 functional scope closed; safety-contract corrections applied; packaging and hook gates pending

This document records what the repository actually implements. The fixed M1 scope and closure checklist are maintained in [milestones/m1.md](milestones/m1.md). This document does not expand the product scope in [product.md](product.md), and it does not replace the contracts in [reference.md](reference.md).

## Current implementation

M1 provides a complete, isolated loop for explicitly declared local template and symbolic resources:

```text
manifest -> inspect/plan -> explicit apply -> post-apply verification
```

The current implementation also requires plans to be issued by the manifest
loader and planner, compares template output byte-for-byte unless a future
format adapter supplies evidence, records source/target identities, serializes
cooperating writers with a directory lock, and distinguishes `no_changes`,
`committed`, `committed_but_verification_failed`, `committed_state_unknown`,
and `verification_failed`. M1 template variables require an explicit
`variables_sensitivity = "public"` declaration and are loader-classified
manifest literals; provider and secret inputs remain outside the accepted data
path.

The following capabilities remain outside M1:

- provider or rbw integration;
- secret-aware inputs or secret persistence;
- baselines and durable plan records;
- field ownership and controlled reverse sync;
- merge resources, links, copies, structured YAML comparison, and general formatter support;
- multi-file transactional rollback and a broad platform matrix.
- protection against unrelated processes that ignore Luwu's advisory directory
  lock; M1 does not claim those races are safe, and a kernel-level
  compare-and-swap primitive is future work.

## Verification

The implementation and isolated fixture were re-verified on the current POSIX development environment:

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -v  # 44 tests passed
PYTHONPYCACHEPREFIX=/tmp/luwu-compile python3 -m compileall -q src tests
UV_CACHE_DIR=/tmp/luwu-uv-cache uv lock --check
ruff check src tests
ruff format --check src tests
git diff --check
isolated CLI fixture E2E: plan -> apply --yes -> inspect; clean post-apply state
```

The manual isolated CLI E2E check used a temporary copy of `tests/fixtures/m1`, confirmed the generated file content and mode `0644`, confirmed it was not a symlink, and confirmed no temporary `.luwu-*` entry remained. No repository fixture target was mutated. The fixture is a manual E2E input, not a hidden test dependency.

The locked `uv` packaging workflow and `prek run --all-files` remain required repository gates. `uv build` was blocked while resolving the uncached Hatchling build dependency because network/DNS access is unavailable. `prek run --all-files` was blocked before hook execution because its cache and the repository's read-only `.agents/skills` entry cannot be written in this environment.
