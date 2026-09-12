# Luwu Implementation Status

Status: M3b single-resource public mutation slice implemented; M3c durable multi-resource execution not started

This document records what the repository actually implements. The fixed M1 scope and closure checklist are maintained in [milestones/m1.md](milestones/m1.md); the M2 observation scope and closure checklist are maintained in [milestones/m2.md](milestones/m2.md); the M3a implementation boundary and closure evidence are maintained in [milestones/m3.md](milestones/m3.md). This document does not expand the product scope in [product.md](product.md), and it does not replace the contracts in [reference.md](reference.md).

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
- merge resources, additional link kinds, structured YAML comparison, and general formatter support;
- multi-file transactional rollback and a broad platform matrix.
- protection against unrelated processes that ignore Luwu's advisory directory
  lock; M1 does not claim those races are safe, and a kernel-level
  compare-and-swap primitive is future work.

M2 now implements version 2 read-only observation: multiple resources are
validated and planned in stable order, cross-resource path conflicts are
rejected, literal `copy` resources are observed as exact bytes, and template
resources may opt into a strict JSON comparison. JSON
formatting equivalence is `formatting/noop`; parsed semantic drift is
`drifted/report`; unsupported input is `blocked/block`. Version 2 `apply` is
rejected with `m2_read_only` before any write. M2 does not implement
multi-resource apply, rollback, baselines, field ownership, or reverse sync.

M3a now implements version 3 read-only field observation: explicit public JSON
template resources declare literal top-level field owners, may name an
explicit baseline envelope, and produce metadata-only three-way field
classification. Baselines are read through the declared path with no-follow
descriptor operations and are never created or updated. Missing baselines are
reported as `unbased`; one-sided changes produce ownership-aware candidates,
two-sided changes require review, and undeclared desired/live changes produce a
separate boolean signal. Version 3 apply is rejected with `m3_read_only` before
any writer path. M3a itself does not implement acceptance or reverse sync;
persistent plans and multi-resource execution/rollback remain M3c work.

M3b now adds version 4 as a narrow public mutation slice. `accept` can
explicitly write selected desired/live fields to a declared baseline, and
`reverse-sync` can write selected live-owned fields through an explicit
literal-JSON source mapping. Both commands require one resource, explicit
fields, and `--yes`; previews are zero-write and results are metadata-only.
Dynamic Jinja reverse writes, undeclared content, provider/secret inputs, and
version 4 `apply` remain blocked. Durable plan records, multi-resource apply,
partial-success recovery, and rollback are not implemented.

M3c groundwork now includes an unexposed metadata-only `PlanRecord` schema
with closed validation, immutable transitions, atomic JSON persistence, and a
fixed `rollback = "never"` policy. It is not yet connected to planning,
execution, recovery, or a CLI command.

## Verification

The implementation and isolated fixture were re-verified on the current POSIX development environment:

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -v  # 126 tests passed
PYTHONPYCACHEPREFIX=/tmp/luwu-compile python3 -m compileall -q src tests
UV_CACHE_DIR=/tmp/luwu-uv-cache uv lock --check
ruff check src tests
ruff format --check src tests
git diff --check
M3a regression: tests/test_manifest_m3.py, tests/test_ownership.py, and tests/test_m3.py
M3b regression: tests/test_m3b.py and the v4 manifest/mutation boundary tests
M3c record regression: tests/test_plan_record.py
isolated CLI fixture E2E: plan -> apply --yes -> inspect; clean post-apply state
```

The manual isolated CLI E2E check used a temporary copy of `tests/fixtures/m1`, confirmed the generated file content and mode `0644`, confirmed it was not a symlink, and confirmed no temporary `.luwu-*` entry remained. No repository fixture target was mutated. The fixture is a manual E2E input, not a hidden test dependency.

The M2 regression suite additionally uses temporary projects to verify stable
multi-resource observation, cross-resource path rejection, literal-copy
observation, strict JSON formatting/drift/unsupported boundaries, resource-
level error collection, metadata-only output, and the zero-write version 2
apply boundary. It confirms that a changed in-memory manifest version cannot
turn an M2 plan into a write-capable plan.

The locked `uv` packaging workflow is verified: after allowing the required
network access for the uncached Hatchling dependency, `uv build` produced both
the source distribution and wheel in a temporary output directory. The full
`prek run --all-files` gate passed, including type checking, Markdown
formatting, lockfile validation, and secret scanning.
