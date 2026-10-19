# Luwu Changelog

This document owns the record of what each delivery added. It is not a
contract, a status report, or a plan: the public behavior is in
[reference](reference.md), what is implemented now is in [status](status.md),
and what is open or planned is in [roadmap](roadmap.md).

Delivery names (M1-M7) are labels for sequence, and the manifest versions named in
older entries were retired by the consolidation entry below. They do not allocate manifest
versions and do not organize tests, fixtures, or documents. Per-delivery
review notes, benchmark samples, and acceptance reports from before this
changelog existed are preserved in commit `bb80335` (the last commit before they were removed).
For example: `git show bb80335:docs/milestones/m3.md`.

## M1: Preview (manifest version 1)

- Single-resource template and symbolic-link loop with read-only `inspect` and
  `plan`.
- Explicit `apply --yes` with stale-state checks and atomic replacement.
- Path and symlink safety checks; metadata-only JSON output.

## M2: Observation (manifest version 2)

- Multiple declared resources, literal `copy`, and cross-resource conflict
  validation.
- Strict JSON semantic comparison next to exact-byte comparison.
- Version 2 is read-only: `apply` is refused before any write.

## M3: Ownership and reconciliation (versions 3, 4, 5)

- Three-way observation from an explicit public baseline and declared field
  ownership, with conflict and undeclared-change reporting.
- Explicit baseline acceptance and selective literal-JSON reverse sync for one
  resource, with post-write verification.
- Whole-file multi-resource execution with a metadata-only journal, partial
  outcomes, and read-only recovery.
- Repairs found during review: selective span patching, staged-identity
  re-observation after a replace boundary, closed journal validation, sidecar
  path collisions, stale-baseline authorization, and recovery that disagreed
  with a fresh plan.

## M4: Providers and secrets (version 6)

- A narrow `rbw` provider behind explicit subprocess authority and bounded
  output.
- Secret-bearing templates, owner-only secret targets, and metadata-only
  records.
- An opt-in metadata cache that cannot authorize a decision, and a
  fail-closed platform probe.

## M5: Batch performance and interruption safety (PR #16)

- Reproducible scaling benchmark, profiling, and removal of repeated global
  validation and journal costs, without weakening durability or outcomes.
- Interruption tests including real `SIGKILL` at each boundary.
- Performance budgets and measurement rules now live in
  [maintenance](maintenance.md#performance-acceptance).

## M6: Bidirectional pilot (version 7, PR #17)

- Explicit source, target, and input roots; declared public machine-local
  inputs with provenance.
- Public JSON and a bounded TOML subset for application-shaped Pi and Codex
  configuration; reverse sync into declared inputs.
- Isolated rehearsal and a Dotter handoff procedure; the user trial is still
  pending (see [roadmap](roadmap.md)).
- Inline TOML comments extend version 7 without a new version.

## M7: Full dotfiles coverage (version 8, PR #18)

- Profiles with package closure and exclusion, layered public inputs, and
  template, copy, and directory resources.
- Declared parent and file modes, explicit copy adoption, JSONC and nested
  TOML field paths, and selected multi-resource mutation.
- Finite-batch freshness checks and immutable plan evidence for
  authorization.
- Synthetic OpenCode, Codex, and Droid settings shapes; no application
  runtime has loaded them.

## Consolidation: one manifest contract

- Retired manifest versions 1 through 7. Version 8 is the only manifest the
  loader accepts; the provider, journal, cache, record, recovery, and platform
  commands left with the versions that defined them. Their code remains in git
  history. The M1-M7 entries above describe what each delivery added at that
  time, not what exists now.
- One implementation per primitive: a single no-follow filesystem and atomic
  write module, one baseline and one format module, and one sensitive-name
  predicate shared by the manifest, input layers, and documents. Modules, error
  codes, test classes, and fixtures are named by capability, not by delivery.
- Removed dead code and checks that repeated an earlier check without adding a
  guarantee. Added fault-injection tests for the post-callback target
  recheck, the pre-staging stale check, and the post-replace verification.
- An unsafe live target, baseline, or input file now blocks only its resource
  instead of aborting the whole profile.
- Reverse sync can write into a declared destination layer that is absent or
  below the winner, as long as no higher layer overrides it, and creates
  missing destination parents when the resource declares `parents = "create"`.
- A baseline recorded for a changed declaration is reported as `baseline_stale`
  and replaced by an explicit accept or adopt, instead of blocking the
  resource until the file is deleted by hand.
## Capability matrix by delivery

Rows are capabilities and columns are deliveries. A cell says where the
capability was introduced or widened, not whether it works now.

| Capability                     | M1  | M2  | M3  | M4  | M5  | M6  | M7  |
| ------------------------------ | --- | --- | --- | --- | --- | --- | --- |
| Template and symbolic resource | new |     |     |     |     |     | ext |
| Multi-resource observation     |     | new |     |     |     |     | ext |
| Structured comparison          |     | new |     |     |     | ext | ext |
| Ownership and baselines        |     |     | new |     |     | ext | ext |
| Reverse sync and accept        |     |     | new |     |     | ext | ext |
| Journal and recovery           |     |     | new | ext | ext |     |     |
| Providers and secrets          |     |     |     | new |     |     |     |
| Batch performance              |     |     |     |     | new |     | ext |
| Roots and local inputs         |     |     |     |     |     | new | ext |
| Profiles and directories       |     |     |     |     |     |     | new |
| JSONC and nested TOML          |     |     |     |     |     | ext | new |
