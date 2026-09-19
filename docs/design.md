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

The implementation deliberately avoids a general transaction engine, a
second durable ledger, or a recovery mutation layer. The public partial
success and recovery meanings are defined in [reference.md](reference.md).

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
