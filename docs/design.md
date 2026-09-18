# Luwu M1/M2/M3a/M3b/M3c/M4 Design

Status: current implementation design

M1 is a small forward-only vertical slice that tests Luwu's central boundary: a declared configuration relationship can be observed and explained before an explicit mutation. The public details are owned by [reference.md](reference.md); this document explains the current flow and why its limits are intentional.

## Flow

```text
CLI input
  -> manifest validation
  -> kind-specific source handling
  -> target observation
  -> in-memory plan
  -> explicit confirmation
  -> complete stale-state preflight
  -> atomic file replacement
  -> fresh post-apply plan
```

`luwu/manifest.py` owns the TOML schema, kind inference, and manifest-root path boundary. `luwu/rendering.py` reads only a declared template source and renders it with a sandboxed Jinja environment, strict undefined variables, no loader, and no network or subprocess capability. `luwu/reconcile.py` owns kind-specific observation, plan metadata, stale checks, and atomic writes for files and symlinks. `luwu/cli.py` translates argparse input and serializes human or metadata-only JSON output.

The plan keeps rendered bytes in memory only so the apply step can use exactly what was inspected. They are not persisted, printed, hashed into output, or copied into a baseline. Applying a plan first validates the declared target, including no-op observations, so an already-stale target cannot be silently ignored. M1 accepts exactly one resource and therefore does not define a multi-file transaction or rollback contract. Each replacement is atomic, and the existing target remains untouched until the final replacement step. On the supported POSIX path, apply walks the manifest root with descriptor-relative `O_NOFOLLOW` operations, records source/target identities, locks the target directory for cooperating Luwu writers, creates its temporary entry in that held directory, and replaces the target through that directory descriptor. Unsupported filesystem primitives fail closed. An advisory lock cannot constrain an unrelated process that ignores it; M1 therefore does not claim protection against such races, and a stronger kernel compare-and-swap boundary remains future work.

## Ownership and scope

M1 supports only `owner = "source"` and `scope = "whole-file"`. That is an explicit limitation, not a generic precedence rule: the declared template or source path owns the complete target path, while anything outside that target is outside the resource. A live-side difference is reported as drift and is never reverse-synced or silently adopted. Field ownership, baselines, merge decisions, and provider values require a later contract.

## Semantic observation

For templates, the desired side is always the rendered template. M1 only treats exact byte equality as `in_sync`; without a format adapter, line endings, trailing whitespace, and final-newline differences remain `drifted` and are replaceable. For symbolic resources, the desired side is the source path resolved within the manifest root. All other readable differences are `drifted`. Missing targets and unsafe target boundaries are separate states so the next action is visible.

This deliberately avoids pretending that a generic formatter or parser can safely preserve every configuration language. Structured comparison and field-level reverse sync are later experiments with their own contracts. M1 template variables are loader-classified public literals; provider references and secret-bearing inputs have no accepted path into rendering.

## M2 read-only observation

M2 extends the flow only after manifest validation:

```text
version 2 manifest
  -> stable multi-resource observations
  -> explicit comparison adapter
  -> metadata-only plan
  -> read-only boundary
```

The manifest loader rejects cross-resource target conflicts and ancestor overlaps before any resource is planned. The planner then observes every resource in stable name order; a rendering failure becomes a resource-level blocked observation so another resource is not silently omitted. A blocked observation blocks the plan as a whole, but does not prevent the remaining resources from being explained.

Exact bytes remain the default. The literal `copy` kind reads source bytes without Jinja rendering. The JSON adapter receives rendered template bytes and live target bytes, not Jinja source. It has a deliberately small strict-JSON equivalence relation: it retains all fields and array order, rejects duplicate keys and unsupported syntax, and distinguishes semantic drift from formatting-only representation differences. JSON semantic drift is reported rather than mapped to a write action.

Version 2 has no apply capability. This is a capability boundary, not a dry-run flag: `inspect` and `plan` are observations, while `apply --yes` is rejected with `m2_read_only` before stale checks or target writes. Multi-resource transaction, rollback, baselines, field ownership, and reverse sync remain M3 responsibilities.

## M3a field observation

M3a keeps the M2 read-only boundary and adds one narrow three-way observation:

```text
version 3 manifest
  -> render desired JSON
  -> no-follow read live target and explicit baseline
  -> classify declared top-level fields by ownership
  -> report metadata only
```

`luwu/ownership.py` is a pure classifier. It parses desired and live objects with the existing strict JSON rules, validates a closed baseline envelope bound to the resource/source/target and complete field-owner map, and compares each declared top-level field as a whole subtree. Missing fields use a private sentinel so they remain distinct from JSON `null`. A missing baseline produces `unbased` observations and never grants a candidate. A source/live/merge owner only changes the direction of a one-sided candidate; it does not override a conflict. Undeclared desired/live changes are a separate boolean signal and do not expose unknown keys or values.

`luwu/reconcile.py` reads a baseline through the original declared path with descriptor-relative no-follow operations. It does not use the rendering source resolver for this read, does not create a baseline, and blocks symlinks, non-regular files, missing files, invalid envelopes, and identity mismatches. M3a resources always return `m3_read_only` for apply; candidates are observations and are not translated into `create` or `replace` actions. Persistent plans, multi-resource execution, and rollback belong to the separate M3c contract described below.

## M3b explicit mutation

Version 4 keeps the M3a observation path and adds a separate single-resource
mutation path. `accept` builds a new closed baseline envelope from an explicit
desired/live choice and selected fields. `reverse-sync` first requires a
`live_changed` reverse candidate and then patches only an explicitly mapped
top-level key in a literal JSON source. Dynamic Jinja source is rejected; the
live target is never copied wholesale into a template. Both writers use
descriptor-relative no-follow parents, temporary entries, directory locks,
atomic replacement, and a fresh post-write plan. Durable plan records and
multi-resource execution are separated into the version 5 execution slice.

Field observations retain a private baseline digest calculated from the bytes
actually supplied to the classifier, alongside existing source/live stale
evidence. Reverse-sync reuses that binding in its writer callback; it does not
infer authorization from an independently reread baseline or persist another
snapshot. The callback covers the manifest and baseline around replacement
and post-write verification. Public behavior is defined in [reference.md](reference.md).

## M3c execution slice

Version 5 keeps observation and mutation separate while granting a new,
narrow capability to explicit public whole-file resources:

```text
version 5 manifest
  -> stable observations
  -> complete source/target/manifest preflight
  -> durable planned/preflighted/commit_intent journal
  -> one locked atomic writer at a time
  -> committed / unchanged / unknown / not-attempted states
  -> read-only recover/reobserve
```

The execution journal is a closed metadata schema. It binds the manifest
identity, resource order, source and target roles, relative paths, operation,
non-content file conditions, and state transitions; it never persists rendered
bytes, values, diffs, provider payloads, secrets, or source/target content
hashes; the manifest digest is retained only to bind the manifest identity. A
resource is marked committed only after its atomic replacement and directory sync return
success. If the replacement or cleanup boundary is uncertain, execution stops,
records recovery-required state when possible, and never rolls back an earlier
resource. `record-inspect` and `recover`/`record-reobserve` are deliberately
read-only: they inspect or re-observe the recorded boundary, do not replay old
inputs, and do not provide an automatic recovery mutation.

The journal writer and execution preflight share one deterministic lock-name
function, so the auxiliary write cannot escape path-conflict checks. Recovery
combines existing non-content condition checks with the fresh plan's resource
status when classifying confirmed resources; it adds no persistent content
evidence or recovery engine.

The executor also retains target progress in memory independently of journal
publication. A single execution error boundary attaches that progress to
`ApplyError`, so a later record or failure-marking error cannot erase known
target replacements. The CLI serializes only the allowed metadata fields and
keeps the readable journal separate from this invocation's target evidence.
This uses the existing writer outcomes and journal, without another durable
ledger or recovery mechanism. The exact error states and recovery aggregation
are defined in [reference.md](reference.md).

## M4 provider execution

Version 6 is a separate path from the public version-5 executor:

```text
version 6 manifest
  -> closed schema and external-target boundary
  -> explicit runtime authority
  -> one bounded rbw lookup per provider declaration
  -> short-lived secret render context
  -> metadata-only observation
  -> complete preflight with the captured bytes
  -> owner-only atomic external-target replacement
  -> independent secret-safe journal
  -> read-only current re-observation
```

The manifest declares that subprocess capability is needed, but the CLI or
direct library caller must supply a runtime `ProviderAuthority`. The provider
resolver has one supported adapter, rbw, and one narrow command shape. Its
subprocess runner does not inherit the caller environment, does not use a
shell, does not connect to a network, bounds both pipes, and maps all provider
failure details to fixed safe errors. Executable identity is checked before,
around, and after the run; this is detection for a local TOCTOU threat model,
not a claim of OS-level isolation.

The renderer receives a sealed `SecretRenderContext`. Only its private
renderer token can open a temporary alias mapping; public variables cannot
occupy the `secrets` namespace. The opened values are cleared after rendering,
and `RenderedTemplate`/error/projection boundaries do not serialize or print
rendered bytes. Reconciliation keeps the captured rendered bytes only in the
in-process plan so a confirmed apply cannot fetch a different value during
its write preflight.

Provider targets are absolute and outside the manifest/source and operational
state trees. Descriptor-relative no-follow reads and writes reject symlinks,
non-regular entries, unsafe owner/mode bits, and multiple hard links before a
secret is read and again before replacement. A new target is `0600`; an
existing target must remain current-user-owned and owner-only. The boundary
detects, classifies, and fails closed on local races, but cannot prevent a
privileged or otherwise authorized unrelated writer from changing a directory
between checks.

Version 6 deliberately does not reuse the version-5 persisted content/digest
condition schema. `SecretPlanRecord` contains only public resource labels,
non-content target state, state transitions, and fixed execution contracts.
The cache is a separate explicit metadata diagnostic and has no reconciliation
decision seam. Recovery never replays or writes: without authority it reports
that current provider observation is unavailable; with authority it can report
current convergence, which is not proof of historical secret content.

## Safety boundaries

The normative manifest, CLI, error, and write contract is owned by
[reference.md](reference.md); this section does not repeat its field and state
tables. The design consequences are deliberately narrow: M1 remains
single-resource and forward-only, v2–v4 remain observation or explicit
single-resource mutation contracts, and versions 5 and 6 are separate
multi-resource execution capabilities. Automatic recovery mutation, rollback,
and unrelated-writer race guarantees remain outside the verified closure. The
descriptor-relative, no-follow implementation is the mechanism that preserves
both target boundaries described by the reference contract.
