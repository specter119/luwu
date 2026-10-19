# Maintenance and Release Workflow

This document owns the development, documentation, testing, release, and
migration workflow. The manifest, CLI, JSON, and error contracts are owned by
[reference](reference.md). [Status](status.md) owns the current snapshot, and
the [changelog](changelog.md) records what each delivery added.

## Local checks

Use the checkout's supported Python and the locked environment:

```text
uv sync --locked
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -t .
ruff check src tests
ruff format --check src tests
python3 -m compileall -q src tests
uv lock --check
uv build
git diff --check
```

The ablation experiments in `experiments/` are part of the boundary review and
must run in temporary directories.

For an installed-package full suite, copy `tests/`, `experiments/`, and
`scripts/` into a private directory without `src/`, clear `PYTHONPATH`, and use
the installed environment's Python. Check imported `luwu` module paths to
confirm that the suite used the selected environment's `site-packages`.

## Lockfile

`uv.lock` points at public PyPI (`https://pypi.org/simple` and
`https://files.pythonhosted.org`), so CI and any contributor can use it
without access to a private mirror. Refresh it with the wrapper, not with a
bare `uv lock`:

```text
python3 scripts/uv_lock_pypi.py
```

The wrapper resolves through the index configured on the machine, rewrites
every registry and file URL to PyPI, refuses to keep a lock that still names
another host, and checks the result against PyPI. File hashes are not touched,
so `uv` still verifies every download. The rewrite keys on the
`aa/bb/<60 hex>/<filename>` tail that PyPI paths and mirrors share, because
mirrors differ in the prefix before it. `--rewrite-only` converts an existing
lock without resolving again.

On a machine whose default index is not PyPI, `uv sync --locked` and
`uv lock --check` report the lock as out of date. Add
`--default-index https://pypi.org/simple`, or use `--frozen` for ordinary runs
so `uv` does not rewrite the file. Do not commit a lock produced by a bare
`uv lock` on such a machine.

The `uv-lock` and `ty` hooks in `prek.toml` set `UV_DEFAULT_INDEX` to PyPI for
this reason, so `prek run --all-files` needs no extra flags.

## Test layout

Tests are organized by capability, not by delivery. A new behavior appends to
the test module for its capability under `tests/<capability>/`; it does not
create a parallel file named after a delivery, ticket, or version. Shared
builders and fakes live in `tests/support/`, whose module names do not start
with `test`. Fixtures are named by scenario, not by delivery.

A change to the layout must preserve every test: compare collected test ids
and per-test source before and after, and run each moved module on its own.

## Run output

Benchmark, profile, rehearsal, and ablation output is a local, reproducible
observation, not repository content. Write it under the ignored `.evidence/`
directory (or another path outside the checkout). Do not commit it. What a
delivery added belongs in the [changelog](changelog.md); a check that matters
is a command or test that can be rerun, not a captured result.

## Preventing duplicate implementations

Several rounds of work each added a complete, correct copy of a primitive
(atomic write, no-follow open, baseline read, sensitive-name test) beside the
earlier copy instead of extending it. Reviews checked each diff against its own
contract, each copy had its own tests, and "independent" version contracts
were read as permission to copy the writer. Nothing compared the copies, and
they drifted: five secret-name predicates disagreed on `apikey`, dashes, and
camel case.

A review cannot see this, because the copy is created while writing. Three
controls act at that moment:

1. `AGENTS.md` tells the author to find the owner in
   [design](design.md#shared-primitives) before writing the code.
1. `tests/tooling/test_primitive_ownership.py` fails when a module outside the
   owner calls a primitive such as `os.replace`, `os.open`, or `json.loads`,
   and names the owner in the message. Its ceilings record known debt and may
   only go down.
1. The project skill `luwu-primitive-reuse` lists the moments a duplicate is
   written and the review checks, including the ablation that proves a guard
   redundant.

When a capability needs different behavior, add a parameter or a second
function in the shared module, with a test for each caller's case. When review
finds a second copy, removing one is part of the same change.

## Performance acceptance

Run the performance experiments on a durable local filesystem. Do not use
tmpfs for the acceptance matrix. Use the same recorded host, filesystem,
Python version, fixture, and sandbox mode for before/after comparisons.

For 200 public resources, the warm-run median budgets are:

| Operation                  | Budget             |
| -------------------------- | ------------------ |
| Plan                       | At most 1 second   |
| No-op apply                | At most 2 seconds  |
| One-change apply           | At most 3 seconds  |
| Fresh or all-changed apply | At most 10 seconds |

Run at least five repetitions per scenario, report every sample, median and
maximum, and keep the first run separate without calling it a controlled
cold-cache measurement. Warm maxima must not exceed twice the corresponding
budget. Doubling 100 to 200 resources must take no more than 3x the median
command time for each scenario; inspect operation counts to separate fixed
overhead from structural growth. Timing excludes setup but includes process
startup, preflight, target durability, and verification. Missing or blocked
resources cannot be omitted to improve timing.

Do not relax a budget without measured evidence, remove durability, or report
only a relative speedup. Keep a failed run rather than averaging it away. Do
not add parallel writers as an optimization, because concurrency must not
obscure ordering and partial-success boundaries. A zero exit code alone is not
acceptance: inspect the recorded postconditions, interpreter and module
provenance, and budget fields.

The profile workloads use `experiments/profile_benchmark.py` with an explicit
interpreter and scratch on the same filesystem as the measured worktree.
Build and install the wheel in a fresh environment before claiming the
installed-artifact gate; the installed full suite does not replace the
installed benchmark matrix:

```sh
uv build --out-dir .evidence/dist
uv venv .evidence/installed
uv pip install --python .evidence/installed/bin/python .evidence/dist/luwu-0.1.0-py3-none-any.whl
uv run --script experiments/profile_benchmark.py \
  --python .evidence/installed/bin/python --installed \
  --scratch .evidence --resources 100 200 --repeats 5 \
  --scenarios plan noop onechange allchanged \
  --output .evidence/profile-benchmark.json
```

Run timing without concurrent test suites.

## Isolated rehearsal and Dotter handoff

Rehearse in an isolated temporary HOME before touching a real target. The
copy-and-run commands are in the
[isolated profile fixture guide](../tests/fixtures/dotfiles_profile/README.md).
They use public synthetic data and do not read or write the real HOME, and
they do not launch any application or Dotter.

Before placing a real target in a manifest, admit the complete read/write flow
as public and perform the handoff manually for that target. Inspect the whole
target file, every associated input file, and the existing baseline (or the
public values that would create a new baseline). Admission of only the
selected scalar fields is insufficient. Exclude credentials, authentication
state, provider configuration or files, and any file that contains secrets.

Check access as the identity that will run Luwu: source templates and every
input must be readable; each directory in their paths must be traversable; and
the target parent plus the input and baseline parent directories used for
atomic replacement must grant the required write/traverse access. Review the
effective permissions or ACLs on those specific paths. As a direct check, use
`test -r` on each source/input file, `test -x` on each path directory, and
`test -w` plus `test -x` on the exact target, input, and baseline parent
directories that will be replaced. Review ACLs or mount restrictions if those
checks disagree with the intended access. Do not apply blanket permission
changes to a HOME, repository, or input tree.

Then perform the handoff manually for that target:

1. Record the target label and its current manager as metadata. Confirm that
   the target is a declared regular file under the declared target root,
   that its parent exists or the resource may create it, and that it is not a
   conflicting symlink.
1. Cancel the target's active Dotter package declaration or add/retain an
   explicit exclusion through the normal Dotter workflow. Review the Dotter
   preview and verify that it will not deploy this target during the trial.
   Luwu does not edit Dotter configuration for this step.
1. Run `inspect` and `plan` against an isolated target root first. Review the
   ownership and undeclared-change results before enabling a real target.
1. To return management, first preserve the trial's public edits and review
   them for the next configuration change. Prepare a Dotter preview in an
   isolated or staged configuration with the intended declaration restored;
   review its planned target actions before changing the live declaration.
   Then stop using the Luwu resource, restore the live Dotter declaration
   through the normal workflow, and verify the target's manager before the
   next deployment. Do not run both managers against one target during the
   transition.

The handoff is an operator boundary. It is not inferred from a clean isolated
test and is not performed by inspection, planning, confirmation, or apply.

`prek run --all-files` must be run in a checkout whose hook and cache
directories are writable. A read-only shared agent directory is an environment
failure, not evidence that the source or tests passed. An isolated copy is
acceptable when it is byte-checked against the working tree.

## Boundary ablations

Run the production-module ablation from the repository root. It writes to
temporary directories, uses synthetic data, and checks actual results against
independently specified expectations:

```text
uv run --script experiments/profile_ablation.py --python .venv/bin/python
```

The ablation exercises the manifest boundaries through the production modules
and the real writer. It does not substitute for the focused tests under
`tests/`. When a guard is a candidate for removal, disable it in a private copy
and run the suite: a guard that no test notices needs a fault-injection test
before it is kept, and a guard that another check fully covers is a candidate
for deletion.

## Fixtures and application shapes

Keep fixtures under synthetic roots and public test data. The profile fixture
includes synthetic app-shaped settings workflows for the fixed OpenCode,
Codex, and Droid targets. These exercise file-shape handling only; they do not
launch applications, establish runtime compatibility, or authorize a live
target handoff. The [contract](reference.md#manifest-and-profile-contract)
owns the target shapes and states that generic Luwu declarations do not
enforce application-specific enum domains. Droid workspace trust is
unsupported; see the [roadmap](roadmap.md).

For documentation-only changes, run relative-link and whitespace checks. Link
and formatting checks do not imply that a product suite passed.

## Documentation maintenance

Before editing documentation, classify the statement:

1. product intent or value;
1. stable public contract;
1. current implementation fact;
1. internal mechanism;
1. delivery sequence;
1. workflow instruction;
1. delivery history; or
1. review pattern.

Edit the corresponding owner named in `AGENTS.md`. In every other document,
use a short summary and a relative link. The changelog records what a delivery
added; it must not become a second current contract. Do not add a translation
mirror or a new specialist document unless it has an independent owner and
audience.

After editing:

- search for a second definition of each changed field, state, error, or
  capability;
- check that summaries link to their owner and do not silently change scope;
- check that current status does not repeat historical logs;
- check that a skill pattern cites code, tests, or a current contract;
- run the narrowest relevant documentation and code gates.

## Pattern maintenance

Good and bad patterns belong in the relevant `.agents/skills/*/SKILL.md`.
Patterns are review evidence, not contracts. A pattern entry must identify the
observed behavior, evidence, risk, and smallest corrective direction. Promote
it to the `product`, `reference`, `design`, `status`, or `roadmap` document
only when repeated evidence makes it a stable rule. Do not create a parallel
pattern catalog merely to avoid choosing an owner.

## Release gates

Before release, test a wheel and an sdist in fresh virtual environments. Each
environment must run `luwu --version` and the complete
test suite without importing from the source checkout. The CI workflow keeps
the declared Linux/Python matrix visible and exercises the unsupported-platform
branch. Do not claim a platform or Python combination is supported solely
because the package installs there.
