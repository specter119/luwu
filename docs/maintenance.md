# Maintenance and release workflow

This document owns the development and release workflow. The versioned manifest,
CLI, JSON, error, provider, secret, journal, cache, and platform contracts are
defined by [reference](reference.md) and [milestone M4](milestones/m4.md).

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

`prek run --all-files` must be run in a checkout whose hook/cache directories
are writable. A read-only shared agent directory is an environment failure, not
evidence that the source or tests passed.

## Provider and secret operations

Version 6 provider execution requires an explicit runtime authority for each
invocation:

```text
luwu inspect --manifest luwu.toml --allow-subprocess --rbw-executable /absolute/path/rbw
luwu apply --manifest luwu.toml --allow-subprocess --rbw-executable /absolute/path/rbw --yes --record /absolute/path/journal.json
luwu record-inspect --record /absolute/path/journal.json --json
luwu recover --record /absolute/path/journal.json --allow-subprocess --rbw-executable /absolute/path/rbw --json
```

`record-inspect` is read-only. `recover` re-observes and never replays or
rolls back a historical secret. Provider values, references, rendered bytes,
hashes, journal content evidence, and cache values must not be added to logs or
diagnostics. The declared secret target is the only durable secret output.

The cache is an explicit diagnostic facility, independent of reconciliation:

```text
luwu cache-refresh --cache /absolute/path/provider-cache.json --rbw-executable /absolute/path/rbw
luwu cache-inspect --cache /absolute/path/provider-cache.json --rbw-executable /absolute/path/rbw
```

Inspection, planning, applying, and recovery do not refresh or repair the
cache. A cache status cannot authorize a provider or change a plan decision.

## Release gates

Before release, test a wheel and an sdist in fresh virtual environments. Each
environment must run `luwu --version`, the v1–v6 smoke tests, and the complete
test suite without importing from the source checkout. The CI workflow keeps
the declared Linux/Python matrix visible and exercises the unsupported-platform
branch. Do not claim a platform or Python combination is supported solely
because the package installs there.
