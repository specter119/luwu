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

## M6 pilot extension mechanism

This section describes the implemented mechanism for the version 7 pilot. The
public behavior is owned by [the M6 pilot extension section in reference.md](reference.md#m6-pilot-extension-manifest-version-7).

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

Use the bounded format routines to parse the pilot's selected configuration shapes
and patch the chosen fields. JSON uses strict parsing plus member-span patching
so unchanged bytes survive. TOML accepts only top-level scalar assignments and
preserves comments and unselected bytes; tables, inline comments, duplicates,
and unsupported values are rejected. Do not parse and re-encode a complete
file for a field mutation.

TOML syntax is validated with `tomllib` using `Decimal` for floating-point
values before the bounded scanner locates assignment spans. The scanner
requires a complete scalar token, so an inline comment cannot become part of
a replacement span. Public-key checks reuse the manifest's normalized
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
