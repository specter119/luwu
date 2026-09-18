# Luwu Implementation Status

Status: M4 complete within the frozen public v1-v6 contracts; automatic replay,
rollback, and strong consistency against unrelated writers remain outside scope.

This document records what the repository actually implements. The fixed M1 scope and closure checklist are maintained in [milestones/m1.md](milestones/m1.md); the M2 observation scope and closure checklist are maintained in [milestones/m2.md](milestones/m2.md); the M3a implementation boundary and closure evidence are maintained in [milestones/m3.md](milestones/m3.md); the M4 closure record is maintained in [milestones/m4.md](milestones/m4.md). This document does not expand the product scope in [product.md](product.md), and it does not replace the contracts in [reference.md](reference.md).

The earlier M3 execution/conflict closure below is historical evidence for
then-current revisions. The current checkout completed the frozen-contract
repair recorded in [milestones/m3-repair-plan.md](milestones/m3-repair-plan.md);
the current closure evidence is recorded below.

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

M4 now implements an independent version 6 provider execution capability.
Version 6 has a closed manifest shape, explicit runtime subprocess authority,
one bounded `rbw get --field FIELD ITEM` lookup per provider per calculation,
fixed provider error boundaries, executable identity checks, and platform
fail-closed behavior. Secret values enter only the private renderer context and
the in-process write path; the v6 journal, cache, diagnostics, errors, and
machine-readable projections exclude secret values, provider references,
rendered content, content hashes, and secret-derived metadata. Secret targets
are external owner-only 0600 files with no automatic parent creation.

M4 also provides a separate closed `SecretPlanRecord`, read-only current
re-observation, explicit metadata-only provider cache inspection/refresh, and
the `platform-check` diagnostic. Existing v1-v5 paths remain provider-free
and retain their prior read-only, single-resource mutation, and multi-resource
execution boundaries. The CI workflow declares the Linux x86_64 Python
3.12-3.14 matrix and clean wheel/sdist package checks; automatic replay,
rollback, network providers, and strong protection from non-cooperating
writers are not implemented.

## 2026-09-16 M4 closure

After fetching `origin`, it already pointed at the current HEAD
`2307f32945c8cbf64adac54526425a314a061232`; no rebase or fast-forward was
needed. The implementation and documentation changes remain uncommitted and
unpushed in this worktree.

The final local evidence is:

```text
343 unittest tests: passed
M3 ablation, follow-up ablation, execution-closure ablation, final-closure ablation: passed
M4 plan ablation and secret sentinel experiment: passed
ruff check/format, ty check src tests, compileall, uv lock --check, git diff --check: passed
isolated prek run --all-files and post-hook byte check: passed
platform-check --json: supported Linux/x86_64, Python 3.14.7
wheel and sdist build: passed
fresh wheel install and full 343-test suite: passed on Python 3.12.9, 3.13.15, and 3.14.7
fresh sdist install and full 343-test suite: passed on Python 3.12.9 and 3.14.7
```

The isolated hook copy was
`/tmp/luwu-m4-prek-final2.NR0USH`; the final package gate and venvs were
`/tmp/luwu-m4-package-final3-escalated.7q3PQS`. The hook and package directories are
temporary and outside the checkout. Network-dependent dependency setup
required authorized retries after sandbox DNS failures. The remote GitHub
Actions workflow was not dispatched from this session; its declared
3.12-3.14 Linux matrix and unsupported-platform unit tests are present in
`.github/workflows/ci.yml` and were locally exercised with the same three
Python versions.

## 2026-09-14 frozen-contract repair closure

The current checkout started from `83ca72e` after a direct remote `master` ref
check returned the same commit. The M3 repair closes the three independently
reviewed gaps: literal-JSON reverse-sync now patches only selected value spans
and required local separators; baseline, M3b source, and M3c target writers
classify the replace boundary from staged no-follow identity; and v5
`PlanRecord` conditions use a closed value domain with non-empty contiguous
resources. Equal bytes from an independent target are not treated as a known
Luwu commit.

The repair adds 31 focused regression tests, bringing the full suite to 274
passing tests. The final verification was run against the current worktree
before submission:

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -v  # 274 passed
uv run experiments/m3_ablation.py
uv run experiments/m3_followup_ablation.py
uv run experiments/m3_execution_closure_ablation.py
uv run experiments/m3_final_closure_ablation.py
ruff check src tests experiments
ruff format --check src tests experiments
UV_TOOL_DIR=/tmp/luwu-uv-tools-m3-final uvx ty check src tests
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m compileall -q src tests
UV_CACHE_DIR=/tmp/luwu-uv-cache-m3-final uv lock --check
UV_CACHE_DIR=/tmp/luwu-uv-cache-m3-final uv build --out-dir /tmp/luwu-m3-dist.fHihxw  # wheel + sdist
git diff --check
temporary M1 CLI: plan -> apply --yes -> inspect; content/mode/symlink/temp-entry checks passed
isolated prek: all hooks passed; exact tracked and unignored-file byte check passed
```

The isolated hook copy was `/tmp/luwu-m3-gate-submit.7jHFAv`; it contained every
tracked and unignored current file and passed the full fixed hook set, including
`ty` and `mdformat`. Build artifacts are in `/tmp/luwu-m3-dist.fHihxw/`.
Network-dependent dependency setup required an authorized external retry; no
user configuration or repository fixture target was modified by verification.

M3a, M3b, and M3c are complete within the repaired frozen contracts. This does
not add exact reviewed-plan consent, automatic replay/rollback, or strong
consistency against unrelated writers. M4 is closed within the version-6
provider, secret, portability, and operational contract described above.

## Verification of the 2026-09-13 follow-up

The earlier follow-up recorded these results on the POSIX development environment. They are historical evidence; the subsequent closure review is recorded below.

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -v  # 213 tests passed
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

The 213-test suite, compileall, Ruff check/format, lockfile validation, and
`git diff --check` pass. Both M3 ablation experiments pass. The earlier
cache/network limitations are historical: this follow-up successfully built
the wheel and source distribution and ran the repository hooks with
`PREK_HOME=/tmp/luwu-prek`. Explicit `--files` hook runs include all new,
untracked follow-up files; `-a` alone checks only tracked files. The full
tracked-file gate cannot open the protected `.agents/skills` files for writing
in this sandbox. Final complete-hook verification therefore uses an isolated
temporary copy of every tracked and untracked repository file, leaving the
original index and protected files untouched.

## 2026-09-13 follow-up

The current-worktree audit reproduced three gaps despite the previous 190
tests passing: the journal lock sidecar could create a declared target before
execution, reverse-sync did not bind its authorization to the classified
baseline, and recovery could report confirmed while its fresh plan reported
drift. The reviewed plan and ablation record are in
[milestones/m3-followup.md](milestones/m3-followup.md).

The follow-up adds the lock path to preflight, binds a private baseline digest
to the actual classifier input, rechecks authorization around source writes,
and combines recovery metadata checks with the current plan state. These
changes preserve the existing manifest versions and metadata-only output;
their stable behavior is defined in [reference.md](reference.md).

Independent logic/value reviews identified the gaps and reviewed the plan;
the final independent code review found no remaining blocking issue in the
frozen contracts. The follow-up adds 23 tests, including baseline ABA binding,
pre/post-commit input changes, lock collisions with declared paths, metadata-
preserving content drift, unknown-state recovery, and CLI redaction/outcomes.
M3a, M3b, and M3c are implemented within their frozen scopes. At that
historical point, M4 provider, secret, portability, and operational work
remained unstarted; the current M4 closure is recorded above.

## Historical 2026-09-14 execution and conflict closure

The next audit started from freshly fetched `origin/master` at `d9d7092`.
Its 213 passing tests did not cover three additional cases: a final journal
failure losing known target commits, standalone unattempted recovery being
reported as confirmed, and reverse-sync proceeding past an unselected field
conflict. The reviewed plan, ablation and completion audit are maintained in
[milestones/m3-execution-closure.md](milestones/m3-execution-closure.md).

The implementation now separates target outcomes from journal publication,
retains execution metadata even if journal diagnostics fail, requires every
resource to be confirmed for successful recovery, and blocks reverse-sync on
resource-level review. The existing public manifest versions and persistent
journal schema are unchanged. Independent logic, consent and confidentiality
reviews found no remaining implementation blocker. The additional preflight
journal fault cases requested by final review are covered, along with
failure-marking after a known replacement and failure of journal diagnostics.

Current verification: 243 unittest tests pass (30 added to the fetched
baseline), all three M3 ablation scripts pass, and Ruff check/format, ty,
compileall, lockfile validation, wheel/sdist build and `git diff --check` pass.
The isolated M1 CLI loop again finishes in sync with a regular mode-0644
target. Test operations use temporary projects, not user configuration.

The complete hook gate passes in `/tmp/luwu-m3-closure-gate-MJnGeL`, containing
every current tracked and untracked repository file. File-by-file byte
comparison verifies that the checked copy matches the working tree. This
allows formatting hooks to run without opening the original protected
`.agents` files or changing the original Git index. Hooks with no applicable
files report skipped, not test coverage. Initial dependency resolution was
blocked by sandbox DNS; authorized retries succeeded. Build artifacts are in
`/tmp/luwu-m3-execution-closure-dist/`.

M3a, M3b and M3c are complete within their frozen contracts. At that historical
point, M4 remained unstarted; automatic replay, rollback and strong consistency
against unrelated writers remain outside the M3 closure.
