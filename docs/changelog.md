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

## M7a and M7b: daily loop and file resources (version 8)

- Resource kind is inferred from the source: `.j2` is a template, another file
  is a plain file, and a directory expands into one resource per file. A
  top-level `[defaults]` table sets `deploy`, `parents`, and `parent_mode`.
  The explicit `copy` and `directory` kinds and the whole-file `scope` and
  `owner` keys were removed from the manifest.
- Symlink resources are the default deployment. Copy is an explicit
  `deploy = "copy"`.
- Whole-file text templates render a `.j2` source without fields from declared
  inputs and reuse the copy baseline, adopt, and review mechanisms.
- Apply advances the baseline of the resources it wrote, reports a baseline
  failure as `committed_but_baseline_not_advanced`, and skips resources that
  need review instead of stopping the whole profile (outcome `partial`).
- `undeclared = "preserve"` keeps application-written keys; `public_names`
  admits exact key names past the sensitive-name check; the adopt preview and
  plan projection report `replaces_live_on_apply`.
- `reverse-sync --undeclared PATH` writes the named, proven literal leaves back
  to a template source.
- Apply also repairs the stale baseline of a converged structured template and
  of a copy or whole-file template whose live bytes already match the source.
- A copy or whole-file template whose target is absent and has no baseline file
  is created by apply, which then records its baseline; adopt stays required for
  an existing target.
- `adopt` accepts a repeated `--resource` and adopts the batch from one
  observation of the profile.
- A per-action freshness check expands only the source directories behind the
  resources it rechecks, and a shared input layer is parsed once per distinct
  content, so apply cost no longer grows with actions times directories.
- Capturing a parent chain verifies it with one path walk to the deepest
  directory instead of re-walking from the filesystem root at every level, so
  the opens per observation grow with depth rather than with its square.
- On Linux 5.6 and later a directory path is resolved with one `openat2` call
  instead of one open per component, and the portable walk remains the
  fallback.
- A command that confirms with `--yes` takes the plan it just built as its
  entry observation instead of observing the whole profile a second time.
- M7a was closed on the maintainer's real-migration trial (see the
  [roadmap](roadmap.md#m7a-daily-loop-usability)).

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

## Consolidation: one directory walk

- The manifest loader and the engine no longer carry their own no-follow
  directory walks. They use shared descriptor primitives in
  `luwu/filesystem.py` and map the typed failures to their existing error
  codes. The observation read of a regular file shares the same primitive.
- Strict JSON parsing and the public scalar boundary each have one
  implementation in `luwu/semantic.py`; baselines, input layers, and ownership
  call it.
- A manifest that is a symbolic link is now reported as `manifest_path`
  instead of `manifest_unreadable`, because the entry type is checked before
  it is opened.
- The ownership guard now forbids `os.open`, `os.read`, and `os.listdir`
  outside `luwu/filesystem.py`. Fault-injection tests cover each primitive and
  each caller's error code.

## Capability matrix by delivery

Rows are capabilities and columns are deliveries. A cell says where the
capability was introduced or widened, not whether it works now.

| Capability                     | M1  | M2  | M3  | M4  | M5  | M6  | M7  | M7a/b |
| ------------------------------ | --- | --- | --- | --- | --- | --- | --- | ----- |
| Template and symbolic resource | new |     |     |     |     |     | ext | ext   |
| Multi-resource observation     |     | new |     |     |     |     | ext |       |
| Structured comparison          |     | new |     |     |     | ext | ext | ext   |
| Ownership and baselines        |     |     | new |     |     | ext | ext | ext   |
| Reverse sync and accept        |     |     | new |     |     | ext | ext | ext   |
| Journal and recovery           |     |     | new | ext | ext |     |     |       |
| Providers and secrets          |     |     |     | new |     |     |     |       |
| Batch performance              |     |     |     |     | new |     | ext |       |
| Roots and local inputs         |     |     |     |     |     | new | ext |       |
| Profiles and directories       |     |     |     |     |     |     | new | ext   |
| JSONC and nested TOML          |     |     |     |     |     | ext | new |       |
| Text templates                 |     |     |     |     |     |     |     | new   |
