# Maintenance and Release Workflow

This document owns the development, documentation, testing, release, and
migration workflow. The versioned manifest, CLI, JSON, error, provider,
secret, journal, cache, and platform contracts are owned by
[reference](reference.md). Milestone records own dated scope and closure
evidence; [status](status.md) owns the current snapshot.

## Local checks

Use the checkout's supported Python and the locked environment:

```text
uv sync --locked
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests
ruff check src tests
ruff format --check src tests
python3 -m compileall -q src tests
uv lock --check
uv build
git diff --check
```

M4 provider tests use an injected resolver or a fake `rbw` executable. They do
not access a real vault. The M3 ablation experiments and the M4 ablation are
part of the boundary review and must run in temporary directories.

For an installed-package full suite, copy `tests/` and `experiments/` into a
private directory without `src/`, clear `PYTHONPATH`, and use the installed
environment's Python. The copied suite's working directory must have no
group- or world-writable ancestors: the fake-rbw tests deliberately apply the
same executable-ancestry checks as the real adapter, so a suite under `/tmp`
cannot pass those tests. A private scratch directory below the checkout's
ignored `.venv/` is suitable. Check imported `luwu` module paths to confirm
that the suite used the selected environment's `site-packages`.

## M5 benchmark and interruption evidence

Run the performance experiments on a durable local filesystem. Do not use
tmpfs for the acceptance matrix, and keep the output artifacts under the M5
milestone record:

```text
uv run experiments/m5_benchmark.py --scratch /path/on/durable-fs/luwu-m5-scratch --output docs/milestones/m5-benchmark-current-source.json
uv run experiments/m5_profile.py --scratch /path/on/durable-fs/luwu-m5-profile --output-dir docs/milestones/m5-profile-current
uv run experiments/m5_provider_latency.py --output docs/milestones/m5-provider-latency-current.json
PYTHONPATH=src python3 -B -m unittest tests.test_m5_benchmark tests.test_m5_sigkill
```

The benchmark must retain sizes 1, 10, 30, 100, 176, and 200, first and five
warm samples, exact action lists, and the environment metadata. Profile plan
and apply separately, and report counted directory synchronization and journal
publication calls. The provider experiment uses an injected resolver and
synthetic latency only. Build and install the wheel and sdist in fresh
environments before claiming the installed-artifact gate; the installed full
suite does not replace the installed benchmark matrix.

## M6 isolated rehearsal and Dotter handoff

Run the sanitized version 7 rehearsal from the repository root using either
the locked checkout environment or an environment where Luwu is installed:

```text
# Checkout/source mode
uv run --locked --project . python -B experiments/m6_pilot.py

# Installed mode; replace this with the selected environment's Python path
env -u PYTHONPATH /path/to/venv/bin/python -B experiments/m6_pilot.py
```

The runner retains its PEP 723 metadata for standalone Python tooling. Use the
explicit `uv run --locked --project . python -B experiments/m6_pilot.py` form
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
[`tests/fixtures/m6/README.md`](../tests/fixtures/m6/README.md).

Before placing a real target in a version 7 manifest, admit the complete
read/write flow as public and perform the handoff manually for that target.
Inspect the whole target file, every associated global/local input file, and
the existing baseline (or the public values that would create a new baseline).
Admission of only the selected scalar fields is insufficient. Exclude
credentials, authentication state, provider configuration/files, and any file
that contains secrets. The bounded TOML input excludes tables, arrays, and
inline comments. JSONC, JSON comments, and trailing commas are unsupported. See
the [version 7 format and public-data contract](reference.md#m6-pilot-extension-manifest-version-7)
for the exact accepted shape and limits.

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
1. dated historical evidence; or
1. review pattern.

Edit the corresponding owner named in `AGENTS.md`. In every other document,
use a short summary and a relative link. A closed milestone may retain the
decision and evidence that existed at its date, but it must not become a
second current contract. Do not add a translation mirror or a new specialist
document unless it has an independent owner and audience.

After editing:

- search for a second definition of each changed field, state, error, or
  capability;
- check that summaries link to their owner and do not silently change scope;
- check that current status does not repeat historical logs;
- check that a skill pattern cites code, tests, or a dated closure record;
- run the narrowest relevant documentation and code gates.

## Pattern maintenance

Good and bad patterns belong in the relevant `.agents/skills/*/SKILL.md`.
Patterns are review evidence, not contracts. A pattern entry must identify the
observed behavior, evidence, risk, and smallest corrective direction. Promote
it to `product`, `reference`, `design`, `status`, or a milestone owner only
when repeated evidence makes it a stable rule. Do not create a parallel
pattern catalog merely to avoid choosing an owner.

## Release gates

Before release, test a wheel and an sdist in fresh virtual environments. Each
environment must run `luwu --version`, the v1-v6 smoke tests, and the complete
test suite without importing from the source checkout. The CI workflow keeps
the declared Linux/Python matrix visible and exercises the unsupported-platform
branch. Do not claim a platform or Python combination is supported solely
because the package installs there.
