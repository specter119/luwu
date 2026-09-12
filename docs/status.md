# Luwu Implementation Status

Status: M3 complete within the frozen public v3/v4/v5 contracts; automatic replay and rollback remain explicitly out of scope

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
classification with strict JSON type distinctions. Baselines are read through the declared path with no-follow
descriptor operations and are never created or updated. Missing baselines are
reported as `unbased`; one-sided changes produce ownership-aware candidates,
two-sided changes require review, and undeclared desired/live changes produce a
separate boolean signal. Version 3 apply is rejected with `m3_read_only` before
any writer path. M3a itself does not implement acceptance or reverse sync;
persistent plans and multi-resource execution are provided by the separate M3c
contract; rollback remains explicitly outside the frozen scope.

M3b now adds version 4 as a narrow public mutation slice. `accept` can
explicitly write selected desired/live fields to a declared baseline, and
`reverse-sync` can write selected live-owned fields through an explicit
literal-JSON source mapping. Both commands require one resource, explicit
fields, and `--yes`; previews are zero-write and results are metadata-only.
Dynamic Jinja reverse writes, undeclared content, provider/secret inputs, and
version 4 `apply` remain blocked. The module-level single-resource guard,
identity-only mapping, stale/parent checks, post-write verification, and
structured committed/unknown CLI outcomes close the frozen M3b contract.

M3c now has a narrow version 5 execution capability for explicit public,
source-owned, whole-file template and symbolic resources. Planning uses stable
resource order and full preflight; confirmed execution writes a closed,
metadata-only `PlanRecord` before and after each intent/commit boundary, stops
without rollback, and records committed, unchanged, unknown, and
not-attempted resources. The CLI requires an explicit journal path for
confirmed version 5 apply and exposes `record-inspect` plus the read-only
`recover`/`record-reobserve` commands, all restricted to the version-5
execution contract. Automatic replay, rollback, and guarantees against
unrelated writers that ignore advisory locks remain explicitly outside scope;
the implemented M3c contract is complete without those behaviors.

## Verification

The implementation and isolated fixture were re-verified on the current POSIX development environment:

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -v  # 190 tests passed
PYTHONPYCACHEPREFIX=/tmp/luwu-compile python3 -m compileall -q src tests
UV_CACHE_DIR=/tmp/luwu-uv-cache uv lock --check
ruff check src tests
ruff format --check src tests
git diff --check
M3a regression: tests/test_manifest_m3.py, tests/test_ownership.py, and tests/test_m3.py
M3b regression: tests/test_m3b.py and the v4 manifest/mutation boundary tests
M3c record regression: tests/test_plan_record.py and tests/test_m3c_execution.py
M3c CLI regression: tests/test_cli.py
isolated CLI fixture E2E: plan -> apply --yes -> inspect; clean post-apply state
```

The manual isolated CLI E2E check used a temporary copy of `tests/fixtures/m1`, confirmed the generated file content and mode `0644`, confirmed it was not a symlink, and confirmed no temporary `.luwu-*` entry remained. No repository fixture target was mutated. The fixture is a manual E2E input, not a hidden test dependency.

The M2 regression suite additionally uses temporary projects to verify stable
multi-resource observation, cross-resource path rejection, literal-copy
observation, strict JSON formatting/drift/unsupported boundaries, resource-
level error collection, metadata-only output, and the zero-write version 2
apply boundary. It confirms that a changed in-memory manifest version cannot
turn an M2 plan into a write-capable plan.

The current focused source/test checks pass, including the 190-test suite,
compileall, Ruff check/format, lockfile validation, and `git diff --check`.
The M3 ablation experiment also passes its 13 reference scenarios and all
documented counterexample counts. The full `prek` gate is not claimed for this
sandbox: with a temporary writable cache its hook clone requires GitHub DNS;
the default cache is read-only. `uv build` is likewise environment-blocked
while resolving `hatchling` because the configured package index cannot be
resolved. These are verification-environment limits, not passing gate claims.
