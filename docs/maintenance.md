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
