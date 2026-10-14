# Luwu Current Implementation Design

Status: current implementation design

The public manifest, CLI, JSON, error, and write promises are owned by
[reference.md](reference.md). This document explains the current call graph,
module responsibilities, and implementation mechanisms. It must not become a
second public contract or a historical status log.

## End-to-end flow

```text
CLI input
  -> manifest validation
  -> kind-specific source handling
  -> target observation
  -> in-memory plan
  -> explicit confirmation
  -> complete stale-state preflight
  -> atomic file replacement
  -> fresh post-action observation
```

The loader validates a declared manifest and creates resource objects. The
planner observes every resource in deterministic order and returns metadata
only. Mutation paths consume a current plan, revalidate the inputs they rely
on, perform one bounded write, and observe the result again. A blocked or
uncertain boundary remains visible instead of being converted into a
best-effort success.

## Module responsibilities

- `luwu/manifest.py` owns parsing and validation of the manifest input and the
  manifest-root path boundary.
- `luwu/rendering.py` renders only declared sources with a sandboxed Jinja
  environment, strict undefined variables, no loader, and no external
  capability.
- `luwu/semantic.py` and `luwu/ownership.py` perform pure comparison and
  responsibility classification. They do not write or persist configuration.
- `luwu/reconcile.py` coordinates observation, planning, stale checks, writer
  calls, execution progress, and post-action observation.
- `luwu/baseline.py`, `luwu/mutations.py`, and `luwu/reverse_sync.py` keep
  explicit acceptance and selective source updates separate from whole-file
  deployment.
- `luwu/plan_record.py` and the version-specific record code persist closed
  metadata about execution boundaries, never configuration content.
- `luwu/providers.py`, `luwu/secrets.py`, and provider cache code isolate the
  external capability path from public resources and diagnostics.
- `luwu/cli.py` translates command-line input and projects approved metadata
  into human-readable or machine-readable output.

The reference document owns the exact field names, state values, errors, and
output shapes associated with these responsibilities.

## Observation and semantic comparison

The desired side is rendered or otherwise read from the declared source before
it is compared with live state. Exact bytes remain the conservative default.
An explicitly selected comparison adapter can report representation-only
differences separately from semantic differences, but an adapter never grants
write authority.

The M2/M3 observation paths keep resource-level failures visible while
continuing to explain other declared resources. The field classifier compares
only declared fields, preserves missing values as a private sentinel, and
returns metadata without values. Baselines are read through their declared
path and are never silently created or updated by observation.

## Mutation and execution

All writers operate under the existing parent boundary. They stage the new
entry, retain relevant no-follow identity evidence, replace atomically, and
re-observe the target. A replacement-boundary exception is classified from
staged identity where possible; equal content is not used as provenance.
Cleanup, durability, and postcondition uncertainty remain unknown rather than
being guessed away.

The multi-resource executor completes its full preflight before the first
writer, keeps resource order stable, and retains target progress in memory
independently of journal publication. The journal writer and its lock use the
same path-conflict checks. Recovery re-observes current state and never
replays historical input or rolls back a previous write.

Batch execution keeps manifest identity as a captured byte digest and indexes
resource path relationships instead of repeating pairwise scans. Journal
records remain closed and metadata-only: the executor batches the all-resource
preflight transition, validates contiguous no-op transitions in one snapshot,
and uses immutable validated record snapshots for per-resource intent and
commit publications. Cached serialization and byte-level CAS comparison avoid
rebuilding or reparsing an unchanged record, while the write boundary still
uses the existing lock, temporary entry, fsync, and compare-and-swap checks.
These are internal cost reductions; they do not change the v5 journal schema,
resource states, or recovery write prohibition.

The implementation deliberately avoids a general transaction engine, a
second durable ledger, or a recovery mutation layer. The public partial
success and recovery meanings are defined in [reference.md](reference.md).

## Version 7 pilot mechanism

This section describes the implemented mechanism for the version 7 pilot. Its
public behavior is owned by the [version 7 contract in reference.md](reference.md#m6-pilot-extension-manifest-version-7).

### Resolve roots and declared inputs

Represent the source, input, and target roots as separate captured directory
authorities. Build the resource set only from explicit declarations; do not
discover resources by walking a root. Resolve and validate root identity,
containment, overlap/alias conflicts, parent identity, target type, and
symlink boundaries before observation and again at confirmation. Reuse the
existing no-follow filesystem primitives for descriptor-relative reads and
atomic writes.

Load only declared public global and local input files. Keep provenance
alongside each resolved value so the renderer and field observer can explain
which declared layer supplied it without exposing the value. There is no
implicit overlay: each field maps to one file. Treat input contents as
stale-check evidence only in memory; do not place input values or content
hashes in public output or durable records.

### Observe and patch selected fields

Render the desired configuration from declared templates and public inputs,
then construct one field observation from desired values, live values,
baseline values, and the existing field ownership declarations. Use this same
observation to explain forward and reverse candidates so the two directions
cannot disagree about a field's state. Any newly observed undeclared live
key/change in a pilot target becomes a review stop before a writer for that
target is entered.

Use the bounded format routines to parse the pilot's selected configuration
shapes and patch chosen fields. JSON uses strict parsing plus member-span
patching so unchanged bytes survive. TOML syntax is first validated by
`tomllib`, using `Decimal` for floating-point values. The bounded line scanner
then accepts only top-level bare-key scalar assignments and finds the end of
each scalar token with quote-aware handling of single- and double-quoted
strings, including basic-string escapes. An inline comment outside the token
is retained as part of the untouched line suffix; `#` inside a quoted string
stays within the token.

For an update, the patcher replaces only the selected value span. For a
permitted deletion, it removes the complete assignment line. It applies spans
from right to left, reparses the result, and verifies selected values and
unselected semantics. Full-line comments, inline comments, spacing, line
endings, and unselected source bytes survive updates. Tables, arrays, quoted
or dotted keys, multiline strings, duplicates, and unsupported values remain
outside the scanner subset. Public-key checks reuse the manifest's normalized
sensitive-name predicate while retaining the pilot's additional exclusions.

### Confirm, write, and recover

Keep baseline acceptance, reverse input mutation, and forward target mutation
as distinct confirmed operations. At confirmation, recalculate desired/live
fields and revalidate the manifest, templates, all contributing inputs,
baseline, root identities, allowlist, target, and parent boundary against the
preview observation. A mismatch stops before replacement and requires a new
preview. Create an absent target only from declared pilot content; apply only
the selected field patch to an existing target at the existing atomic write
boundary. Then re-read and recalculate before reporting success.

The mutation path checks loader provenance and binds the complete resource
declaration to a freshly loaded manifest. Writer callbacks repeat authorization
checks after staging. The atomic writer also checks the destination mode,
parent binding, and the staged inode before publishing it. A reverse operation
keeps an in-memory map of its completed input replacements, allowing those
specific changes during later checks while rejecting unrelated changes.
Replacement facts are retained separately from reload and verification, so a
later exception cannot erase an earlier successful write.

Treat writes to inputs, baselines, and live targets as separate boundaries;
do not imply a transaction across them. The pilot returns metadata-only
committed, no-change, stale, verification-failed, or state-unknown outcomes
and does not create a second durable execution journal. Recovery remains the
read-only act of building a fresh plan; it never replays or rolls back a
mutation.

The Dotter handoff is an external precondition for each allowlisted target.
Keep Dotter configuration changes and return-to-Dotter steps in the explicit
operator procedure; inspection, planning, confirmation, and recovery do not
perform that handoff themselves.

## Version 8 full-coverage mechanism

This section describes the implementation boundaries for version 8. The
technical core is implemented; overall M7 closure remains subject to the
remaining application-schema evidence linked from
[status.md](status.md). The exact public behavior belongs to the
[version 8 contract](reference.md#m7-full-dotfiles-coverage-manifest-version-8);
current implementation evidence belongs to [status.md](status.md).

The current module responsibilities are:

- `luwu/m7_manifest.py` loads the version 8 declaration, validates roots and
  path relationships, resolves the explicitly selected profile, computes
  stable package dependency closure and exclusions, and expands declared
  directory resources. It retains input-layer and expansion provenance so a
  later confirmation can reload and compare the same selection. Excluded
  resources and inputs used only by them are removed before payload metadata
  observation; declaration validation alone does not inspect their contents.
- `luwu/m7_inputs.py` resolves only the selected resource's declared input
  bindings, applies ordered layers, and keeps value provenance and file
  observations in memory for rendering and freshness checks. Reverse mutation
  must confirm that its destination wins the overlay and simulate the patch
  across every consumer in the selected profile before grouping writes by file.
- `luwu/m7_formats.py` parses complete JSON, JSONC, and TOML documents while
  retaining their original bytes. Literal tuple paths identify scalar leaves
  across formats. The format layer owns duplicate/sensitive-key checks,
  semantic equality, selected-span edits, reparsing, and verification that
  unselected document regions retain their meaning and bytes. It masks only
  selected leaves and absent mapping/table ancestors required by those edits;
  existing unknown containers, arrays, and type changes stay observable.
- `luwu/m7_baseline.py` validates schema-2 baselines against the declared
  roots, profile, resource paths, field mapping, input bindings, and directory
  expansion. Its closed payload variants persist selected public field values
  or an explicitly accepted copy digest as public content evidence, never the
  complete source or target document. The target copy payload records the
  digest algorithm and accepted digest, with mode evidence separately. Durable
  binding identity uses resolved root paths; root device/inode identity belongs
  to execution freshness, not baseline identity. The corrected identity
  encoding and its focused evidence are recorded in the
  [M7 record](milestones/m7.md).
- `luwu/m7_filesystem.py` provides no-follow regular-file observations,
  missing-parent inspection, one-directory-at-a-time parent creation, and
  staged file replacement; content and mode-only changes use that same staged
  file path. The selected parent-creation flow creates the final
  child name with exclusive `mkdir` at initial mode 0o700, then immediately
  opens it with no-follow directory flags and holds the descriptor. It applies
  the requested exact mode with `fchmod`, verifies descriptor and named-entry
  type, identity, and mode, syncs the child and parent, and rechecks the full
  ancestor chain. `FileExistsError` is handled at the `mkdir` call; errors
  after successful creation preserve committed/uncertain evidence. It returns
  an observed identity, not proof of creation provenance. This path does not
  use staged-directory publication, `ctypes`, or `renameat2`. The public
  mode, identity, outcome, and actor guarantees are in the [version 8 resource
  contract](reference.md#resources-and-declared-scope); current verification
  evidence is summarized in [status](status.md).
- `luwu/m7_plan_evidence.py` captures the engine's one complete plan projection
  into detached immutable evidence records and compares later projections
  against every recorded edge and scalar. It preserves exact scalar types and
  Decimal representation, rejects unsupported and cyclic graphs, and keeps
  no mutable plan references or persistent identity cache. Equivalent graphs
  may split or merge shared subnodes; topology alone is not authorization.
  Changed values, types, Decimal representation, document spans, or bytes do
  not match. The public authorization behavior is owned by the
  [version 8 contract](reference.md#m7-full-dotfiles-coverage-manifest-version-8).
- `luwu/m7_engine.py` composes read-only plan, explicit baseline accept,
  whole-file copy adopt, selected reverse sync, and profile-wide apply. It
  uses a finite-batch flow: complete preflight, shared and current-action
  complete-byte freshness checks before staging and again in the publication
  callback, then a fresh whole-batch observation. Reverse sync rechecks all
  shared consumers; per-action results retain earlier commits if a later
  resource or parent creation fails. This flow creates no simultaneous
  snapshot, generic transaction, replay layer, or durable execution journal. The
  [version 8 finite-batch contract](reference.md#finite-batch-freshness-and-verification)
  owns the exact checks and outcomes. At mutation entry, the caller plan is
  checked against its captured evidence; a fresh verify-observe-verify cycle
  then rederives the selected profile and must match the same evidence before
  execution. Freshly derived resources, parsed documents, inputs, observations,
  baselines, ownership, and write bytes drive the authorized operation. The
  engine no longer retains caller-origin pointers or repeats the origin-graph
  check after authorization; shared declarations still pass the independent
  registry check at each action. Complete current-action dependencies are
  re-read before staging and in the publication callback, with whole-batch
  preflight, shared-consumer verification, expected own-write identities,
  final no-op observation, and honest partial outcomes retained. Expected
  filesystem facts advance only from identities returned by successful Luwu
  actions. Current acceptance evidence and remaining scope limits are tracked in
  [status](status.md).

The CLI integrates these capabilities behind explicit profile and mutation
selection. Directory enumeration is rechecked at the write boundary so a new
or rebound leaf cannot silently enter the authorized set. The release evidence
for the current implementation is summarized in [status](status.md); overall
M7 closure remains pending the items recorded there and in the [M7
milestone](milestones/m7.md).
Application-shaped configuration uses the same declared-path and ownership
mechanism. The fixed-version targets and opaque live-owned Codex hook state
are defined in [reference.md](reference.md#m7-full-dotfiles-coverage-manifest-version-8);
the implementation does not infer mappings or grant application trust.
Droid workspace-trust SDK calls do not provide a file-schema mechanism, so no
workspace-trust file mapping is implied.

## Provider and secret path

The provider path is isolated behind an explicit runtime authority and a
bounded resolver seam. The subprocess adapter uses a fixed argument shape, no
shell, a minimal environment, bounded output, and fixed safe errors. Provider
values are opened only inside a private rendering context and are kept in
memory for the current calculation.

Secret-target writes use a stricter existing-parent boundary and owner-only
permissions. Provider records, caches, diagnostics, and errors use
metadata-only projections. The confidentiality contract and the supported
provider shape belong to [reference.md](reference.md); this section describes
the separation of implementation paths.

## Deliberate limits

The current implementation does not provide automatic replay, rollback,
historical secret-content proof, exact reviewed-plan tokens, network
providers, or strong consistency against writers that ignore Luwu's advisory
locks. These limits are current status and contract facts, not invitations to
silently broaden this design. Future changes need a new owner decision,
evidence, and updated reference/status records.
