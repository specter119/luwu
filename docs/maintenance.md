# Maintenance and Release Workflow

This document owns the development, documentation, testing, release, and
migration workflow. The versioned manifest, CLI, JSON, error, provider,
secret, journal, cache, and platform contracts are owned by
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

Provider tests use an injected resolver or a fake `rbw` executable. They do
not access a real vault. The ablation experiments in `experiments/` are part
of the boundary review and must run in temporary directories.

For an installed-package full suite, copy `tests/` and `experiments/` into a
private directory without `src/`, clear `PYTHONPATH`, and use the installed
environment's Python. The copied suite's working directory must have no
group- or world-writable ancestors: the fake-rbw tests deliberately apply the
same executable-ancestry checks as the real adapter, so a suite under `/tmp`
cannot pass those tests. A private scratch directory below the checkout's
ignored `.venv/` is suitable. Check imported `luwu` module paths to confirm
that the suite used the selected environment's `site-packages`.

## Test layout

Tests are organized by capability, not by delivery. A new behavior appends to
the test module for its capability under `tests/<capability>/`; it does not
create a parallel file named after a delivery, ticket, or version. Where one
capability has behavior that differs by manifest version, keep each version's
tests in its own `V<n>` test class in the same module, and never merge two
implementations into one class. Shared builders and fakes live in
`tests/support/`, whose module names do not start with `test`. Fixtures are
named by scenario, not by delivery.

A change to the layout must preserve every test: compare collected test ids
and per-test source before and after, and run each moved module on its own.

## Run output

Benchmark, profile, rehearsal, and ablation output is a local, reproducible
observation, not repository content. Write it under the ignored `.evidence/`
directory (or another path outside the checkout). Do not commit it. What a
delivery added belongs in the [changelog](changelog.md); a check that matters
is a command or test that can be rerun, not a captured result.

## Performance acceptance

Run the performance experiments on a durable local filesystem. Do not use
tmpfs for the acceptance matrix. Use the same recorded host, filesystem,
Python version, fixture and sandbox mode for before/after comparisons:

```text
mkdir -p .evidence
uv run experiments/batch_benchmark.py --scratch /path/on/durable-fs/luwu-scratch --output .evidence/batch-benchmark.json
uv run experiments/batch_cprofile.py --scratch /path/on/durable-fs/luwu-profile --output-dir .evidence/batch-profile
uv run experiments/provider_latency.py --output .evidence/provider-latency.json
PYTHONPATH=src python3 -B -m unittest tests.tooling.test_benchmark_harness tests.interruption.test_sigkill
```

For 200 public resources, the warm-run median budgets are:

| Operation                  | Budget             |
| -------------------------- | ------------------ |
| Plan                       | At most 1 second   |
| No-op apply                | At most 2 seconds  |
| One-change apply           | At most 3 seconds  |
| Fresh or all-changed apply | At most 10 seconds |
| Read-only recovery         | At most 2 seconds  |

Run at least five repetitions per scenario, report every sample, median and
maximum, and keep the first run separate without calling it a controlled
cold-cache measurement. Warm maxima must not exceed twice the corresponding
budget. Doubling 100 to 200 resources must take no more than 3x the median
command time for each scenario; inspect operation counts to separate fixed
overhead from structural growth. Timing excludes setup but includes process
startup, preflight, journal writes, target durability and verification. The
complete 176-file fixture must finish apply, unchanged reapply and recovery;
missing or blocked resources cannot be omitted to improve timing. The budgets
apply to version 8 workloads as well. Provider latency has no budget; it
is measured with synthetic delays only.

Do not relax a budget without measured evidence, remove durability, or report
only a relative speedup. Keep a failed run rather than averaging it away. Do
not add parallel writers as an optimization, because concurrency must not
obscure ordering and partial-success boundaries. A zero exit code or
`complete=true` alone is not acceptance: inspect the recorded postconditions,
interpreter and module provenance, and budget fields.

The benchmark must retain sizes 1, 10, 30, 100, 176, and 200, first and five
warm samples, exact action lists, and the environment metadata. Profile plan
and apply separately, and report counted directory synchronization and journal
publication calls. The provider experiment uses an injected resolver and
synthetic latency only. Build and install the wheel and sdist in fresh
environments before claiming the installed-artifact gate; the installed full
suite does not replace the installed benchmark matrix.

The version 8 profile workloads use `experiments/profile_benchmark.py` with an
explicit interpreter and scratch on the same filesystem as the measured
worktree:

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

Run the sanitized version 7 rehearsal from the repository root using either
the locked checkout environment or an environment where Luwu is installed:

```text
# Checkout/source mode
uv run --locked --project . python -B experiments/pilot_rehearsal.py

# Installed mode; replace this with the selected environment's Python path
env -u PYTHONPATH /path/to/venv/bin/python -B experiments/pilot_rehearsal.py
```

The runner retains its PEP 723 metadata for standalone Python tooling. Use the
explicit `uv run --locked --project . python -B experiments/pilot_rehearsal.py` form
above for checkout mode so uv selects the project environment instead of
creating an isolated script environment. `luwu` must resolve from that selected
environment. The runner does not add `src/` to `sys.path` or fall back to a
source tree. For installed verification, invoke the selected environment's
Python directly from a suite copy that contains the runner and fixtures but no
`src/`; clear `PYTHONPATH` so the installed-artifact gate cannot resolve a
checkout through inherited paths.

The output directory must not already exist and must be outside the real HOME.
The runner copies the public fixtures under a temporary HOME-shaped layout
where `repo/`, `inputs/`, and the application target paths are inside HOME, and
uses `python -m luwu` through the active environment for the CLI acceptance,
create, two bidirectional cycles, and no-op observation. Its uncertainty probe
injects a post-replace directory-sync fault, then starts a fresh CLI process for
read-only observation. The Dotter handoff and return steps mutate only a
synthetic operator-state file and target link. The runner does not launch Pi,
Codex, or Dotter; it does not claim that either application read the fixture.
Its metadata-only report does not authorize or attest to a real target.

The reusable normal-HOME fixtures, exact copy-and-run CLI commands, and user
feedback checklist are maintained in
[`tests/fixtures/pilot_v7/README.md`](../tests/fixtures/pilot_v7/README.md).

Before placing a real target in a version 7 manifest, admit the complete
read/write flow as public and perform the handoff manually for that target.
Inspect the whole target file, every associated global/local input file, and
the existing baseline (or the public values that would create a new baseline).
Admission of only the selected scalar fields is insufficient. Exclude
credentials, authentication state, provider configuration/files, and any file
that contains secrets. The current version 7 TOML subset accepts the bounded
inline-comment behavior recorded in the [version 7 contract](reference.md#pilot-extension-manifest-version-7).
Tables, arrays, quoted or dotted keys, multiline strings, JSONC, JSON comments,
and trailing commas remain unsupported in version 7; version 8 supports a
wider set under its own contract. See
[reference](reference.md#full-dotfiles-coverage-manifest-version-8) and
[status](status.md).

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
   the target is an allowlisted regular file under the declared target root,
   that its parent exists, and that it is not a conflicting symlink.
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
test and is not performed by inspection, planning, confirmation, or recovery.

`prek run --all-files` must be run in a checkout whose hook and cache
directories are writable. A read-only shared agent directory is an environment
failure, not evidence that the source or tests passed. An isolated copy is
acceptable when it is byte-checked against the working tree.

## Boundary ablations

Run the production-module ablations from the repository root. They write to
temporary directories, use synthetic data, and check actual results against
independently specified expectations:

```text
uv run --script experiments/toml_comment_ablation.py
uv run --script experiments/profile_ablation.py --python .venv/bin/python
```

The TOML comment ablation applies the production patcher to synthetic TOML and
contrasts it with a comment-rejecting scalar scan, naive `#` splitting, and
whole-document reserialization, plus whole-line deletion and unsupported-syntax
controls. The profile ablation exercises the version 8 boundaries through the
production modules and the real writer. Neither substitutes for the focused
tests under `tests/`. The older design and guard ablations in `experiments/`
follow the same rule.

## Version 8 fixtures and application shapes

Keep fixtures under synthetic roots and public test data. The version 8
fixture includes synthetic app-shaped settings workflows for the fixed
OpenCode, Codex, and Droid targets. These exercise file-shape handling only;
they do not launch applications, establish runtime compatibility, or authorize
a live target handoff. The [version 8 contract](reference.md#full-dotfiles-coverage-manifest-version-8)
owns the target shapes and states that generic Luwu declarations do not
enforce application-specific enum domains. Droid workspace-trust SDK calls do
not establish a file schema; the open constraint is tracked in the
[roadmap](roadmap.md).

For documentation-only changes, run relative-link and whitespace checks. Link
and formatting checks do not imply that a product suite passed.

## Provider and secret operations

The commands below are workflow examples, not a second CLI contract. See
[reference](reference.md) for their exact arguments, output, errors, and
security promises.

```text
luwu inspect --manifest luwu.toml --allow-subprocess --rbw-executable /absolute/path/rbw
luwu apply --manifest luwu.toml --allow-subprocess --rbw-executable /absolute/path/rbw --yes --record /absolute/path/journal.json
luwu record-inspect --record /absolute/path/journal.json --json
luwu recover --record /absolute/path/journal.json --allow-subprocess --rbw-executable /absolute/path/rbw --json
```

Provider values, references, rendered bytes, hashes, journal content evidence,
and cache values must not be added to logs or diagnostics. The declared secret
target is the only durable secret output.

The cache is an explicit diagnostic facility, independent of reconciliation:

```text
luwu cache-refresh --cache /absolute/path/provider-cache.json --rbw-executable /absolute/path/rbw
luwu cache-inspect --cache /absolute/path/provider-cache.json --rbw-executable /absolute/path/rbw
```

Inspection, planning, applying, and recovery do not refresh or repair the
cache. A cache status cannot authorize a provider or change a plan decision.

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
