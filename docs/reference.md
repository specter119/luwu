# Luwu M1/M2/M3a/M3b/M3c/M4 Reference

Status: current preview contract

This document owns the stable manifest, CLI, JSON, error, and compatibility contract for the developer-confidence preview and its M2, M3a, narrow single-resource M3b, explicit M3c execution, and M4 provider extensions. Product direction belongs in [product.md](product.md); implementation rationale belongs in [design.md](design.md); verified scope belongs in [status.md](status.md).

## Manifest

The manifest is a UTF-8 TOML file. Its root is the directory that scopes every declared source, target, and baseline. M1 uses manifest version `1`; M2 supports versions `1` and `2`; M3a adds version `3`; M3b adds version `4`; the M3c execution slice adds version `5`; M4 adds the independent provider execution version `6`.

```toml
version = 1

[resources.settings]
source = "templates/settings.conf.j2"
target = "live/settings.conf"
owner = "source"
scope = "whole-file"
variables_sensitivity = "public"

[resources.settings.variables]
profile = "developer"
```

M1 accepts exactly one resource. It is intentionally single-resource so that the first apply contract has no multi-file partial-success semantics. That resource must declare:

- `kind` (optional): when omitted, a `.j2` source is inferred as `template` and every other source is inferred as `symbolic`; an explicit value overrides that inference, including `kind = "symbolic"` for an exceptional `.j2` source;
- `source`: a relative path to a regular file inside the manifest directory;
- `target`: a relative path without `..` inside the manifest directory;
- `owner = "source"`: the declared source resource is the owner of the target;
- `scope = "whole-file"`: the resource owns the complete target file;
- `variables_sensitivity = "public"` (required when template variables are present): explicitly classifies those manifest literals as public M1 inputs;
- `variables` (optional for templates): TOML scalar, array, or table values. The loader classifies these manifest literals as public M1 inputs; obvious secret-bearing field names are rejected, and there is no provider or secret-value path into rendering. This is a closed capability boundary, not a scanner that can prove an arbitrary string is non-secret. Symbolic resources cannot have variables.

`template` renders the source as Jinja; `symbolic` makes the target a symlink to the declared source. Explicit `kind = "template"` requires a `.j2` source. Unknown kinds, source/target overlap, absolute paths, path traversal, and provider/secret fields fail validation. A target's parent directory must already exist and must not contain a symlink. M1 does not infer resources from existing files.

### M2 manifest version 2

Version `2` is the read-only observation contract. It accepts multiple declared resources, orders them by resource name, and rejects duplicate targets, source/target path conflicts, and ancestor-overlapping declared paths before planning. It adds the explicitly observed `kind = "copy"`, which reads a regular source file as literal bytes; copy resources are not template-rendered and are not applyable in M2. A version 2 resource supports the same ownership, scope, source, target, and public-variable rules as M1, plus an optional explicit `comparison`:

- `comparison = "exact-bytes"` (default): the conservative byte comparison used by M1;
- `comparison = "json"`: a strict UTF-8 JSON comparison for template resources only.

Version 2 does not expand ownership, field scope, baseline, reverse sync, or provider capabilities. It is an observation experiment: `inspect` and `plan` are supported, while `apply` is rejected with `m2_read_only` before any target write. Version 1 remains the only applyable manifest contract in M2.

### M3a manifest version 3

Version `3` is a read-only field ownership experiment. It accepts multiple explicitly declared template resources, requires `comparison = "json"`, `owner = "fields"`, `scope = "fields"`, and `content_sensitivity = "public"`, and rejects template variables and implicit resource kinds. Its `[resources.NAME.fields]` table maps non-empty literal top-level JSON keys to `source`, `live`, `merge`, or `ignore`; dots and slashes have no path syntax, and nested objects and arrays are compared as one complete field. A resource may name one root-relative `baseline` file, but Luwu never creates or updates it.

The baseline file is a strict JSON envelope with exactly `schema_version`, `resource`, `source`, `target`, `owners`, and `values`. `schema_version` is numeric `1`; `owners` must exactly match all declared fields, including ignored fields; `values` is an object containing only non-ignored declared fields. An absent value means that field was absent from the accepted comparison input, while a JSON `null` is a value. Baselines are read through the declared path with no-follow descriptor operations; symlinks, non-regular files, invalid envelopes, identity mismatches, and missing files block that resource. Desired and live JSON documents must both be objects, and live must already exist.

For a supplied baseline, each field is classified as `unchanged`, `converged`, `source_changed`, `live_changed`, `conflict`, or `ignored`. Ownership determines whether a one-sided change is a `forward_candidate` or `reverse_candidate`; unsupported directions and two-sided changes remain `review`. Without a baseline, non-ignored fields are `unbased` and produce no candidate. Undeclared desired/live changes are reported by a boolean signal without exposing their names or values. Candidates are observations only. Version 3 `apply` is rejected with `m3_read_only` before any write, and M3a does not accept, reverse-sync, persist, or roll back configuration.

### M3b manifest version 4

Version `4` preserves the v3 field contract while admitting two explicit, single-resource public mutations. It still requires an explicit template kind, strict JSON comparison, field scope, and `content_sensitivity = "public"`. The optional `reverse_sync` table must use `format = "literal-json"` and identity-map declared live/merge fields to their same-named top-level literal keys in the source JSON; alias mappings are rejected in this frozen slice. Dynamic Jinja expressions, nested path syntax, ignored/source-owned mappings, and provider or secret inputs are rejected.

`accept` writes only explicitly selected declared fields to the baseline envelope, from either the current desired or live object. `reverse-sync` writes only selected `live_changed`/`reverse_candidate` fields through their explicit source mapping. For literal JSON, an existing selected member is replaced in place; a selected member added or deleted by the live object changes only the required local separator. Preview patch metadata reports each selected key's `replace`, `add`, or `delete` operation and whether a separator was adjusted, without values, source text, or diffs. All unselected and undeclared source bytes remain unchanged; an input whose decoded top-level member spans cannot be proven safe is rejected before writing. Neither command copies a complete live file into a Jinja template, updates the baseline implicitly, or accepts undeclared content. Version 4 `apply` remains read-only; version 5 is the separate current multi-resource execution capability.

Reverse-sync rejects the resource with `review_required` when any declared field requires review, even if that field is not selected. This includes a two-sided conflict or a change against its declared ownership direction. The check applies to both preview and confirmation before building a patch. A compatible forward candidate on an unselected field does not block a selected reverse candidate. Explicit `accept` remains a separate choice of desired/live baseline values and can be used while the observation contains conflicts.

Reverse-sync authorization remains bound to the baseline actually used for field classification. Confirmed execution rechecks that baseline and the manifest under the source writer lock, immediately before replacement, and after replacement alongside the existing source/live checks. A changed or unavailable authorization input blocks before replacement with `stale_plan`; a change detected after replacement preserves the committed/unknown or committed-but-verification-failed outcome. Internal baseline stale-check evidence is never emitted or persisted.

### M3c manifest version 5 execution slice

Version `5` is an independent execution capability, not an upgrade of versions `1`–`4`. It accepts multiple resources only when each resource explicitly declares `kind = "template"` or `kind = "symbolic"`, `owner = "source"`, `scope = "whole-file"`, and `content_sensitivity = "public"`. It rejects comparison, fields, baseline, reverse-sync, provider, and secret fields. Resources are ordered by name, and duplicate or overlapping source/target paths are rejected before planning.

Version 5 `apply` uses a durable metadata-only journal. Without `--yes`, it is a pure preview and does not create the journal. With `--yes`, it requires an explicit `--record PATH`, completes full preflight for every resource before the first writer, and processes resources in stable order. Failure stops at the first failing resource; rollback is never attempted. Each resource is recorded as `unchanged`, `committed`, `unknown`, or `not-attempted`. A committed write whose durability or cleanup cannot be confirmed is reported as `recovery_required` or `unknown`, never as a clean success.

The journal and its persistent sibling lock file `.<journal-name>.luwu-lock` must not overlap the manifest or any declared source/target, including resolved aliases and ancestor relationships. A conflict returns `record_path_conflict` before creating either file or changing any target. The lock coordinates journal updates and is retained after execution. A writer that raises at the `os.replace` boundary is classified from no-follow staged-entry identity, not from equal target bytes: a confirmed staged identity means the target is known changed but durability may be unknown; an identity mismatch or unobservable boundary is `unknown` and requires recovery, without adding the target to `changed_targets`.
During a target write, a rebinding of the locked parent or a changed temporary entry is reported by the writer as `concurrent_change`; the executor preserves the failure boundary and does not treat it as a committed target. This code is distinct from a stale plan detected before the writer and from an uncertain replacement detected at `os.replace`.

The journal has a closed schema and stores only execution/mutation contracts, manifest identity, relative resource/path metadata, non-content file conditions, and state transitions. It does not store configuration values, bytes, rendered output, diffs, patches, provider payloads, secrets, or source/target content hashes. `record-inspect` (also accepted as `inspect-record`) accepts only the version-5 execution contract, reads that journal without replaying inputs or writing files, and rejects legacy records. `recover --record PATH` (also accepted as `record-reobserve`) re-observes the recorded manifest and paths without writing; it returns `confirmed` only when the recorded boundary is still observed and otherwise returns `recovery_required`. There is no automatic replay, rollback, or recovery mutation command in this slice.

For a recorded `committed` or `unchanged` resource, recovery confirmation requires both matching non-content path conditions and a freshly calculated `in_sync` observation. An `unknown` resource meeting those checks is only `matches_postcondition`, and still requires recovery; `not-attempted` remains unattempted. Path-level `matches_postcondition` reports only the non-content metadata comparison; the resource result also accounts for current drift. Confirmation is evidence of current convergence and matching metadata, not proof that historical bytes are unchanged: coordinated source/live edits that preserve all recorded metadata cannot be distinguished without persisted content evidence. Recovery remains an observation, with no guarantee against unrelated concurrent writers.

The overall recovery outcome is `confirmed` only when every resource is confirmed. A journal containing only `not-attempted` resources still returns `recovery_required`, including when their current configurations happen to be in sync.

### M4 manifest version 6 provider execution

Version `6` is an independent provider execution capability. Its root allows
only `version = 6`, `capabilities = ["subprocess"]`, and `resources`. Each
resource explicitly declares `kind = "template"`, `owner = "source"`,
`scope = "whole-file"`, `content_sensitivity = "secret"`, and a non-empty
`providers` table. The only provider type is `rbw`; provider item and field
values are opaque, bounded argv values. Targets are absolute paths outside the
manifest root and source tree, below an existing non-symlink parent.

The manifest capability is declarative. Provider execution also requires a
per-invocation runtime authority constructed from `--allow-subprocess` and an
absolute `--rbw-executable PATH`; without it, version-6 `inspect`, `plan`, and
`apply` are blocked without starting a provider, and `recover` reports
`capability_required` without starting one. The adapter invokes
`[executable, "get", "--field", field, item]` with bounded output,
`shell=False`, `stdin=DEVNULL`, a minimal environment, fixed cwd, strict UTF-8,
and fixed provider error codes. It removes at most one terminal LF/CRLF and
rejects empty, NUL-containing, malformed, non-zero, oversized, timed-out, or
executable-identity-changing results.

Each provider is resolved at most once per calculation. A confirmed
`apply --yes` obtains a fresh value once, completes preflight, and writes only
the bytes captured by that calculation; its write preflight does not fetch
again. Provider values are short-lived in memory. They never enter output,
logs, errors, diffs, hashes, baselines, cache, backups, or journals. The only
durable content containing a secret is the explicitly declared secret target,
which must be owner-only (`0600` for a new target), regular, single-linked, and
owned by the current user; each read and write boundary rechecks this state.

Version-6 execution records use a separate closed, metadata-only schema. They
retain only the contract, opaque execution id, resource ordinal/public label,
target non-content state, and execution events, plus the manifest locator needed
for read-only re-observation. They contain no provider reference, resource or
target path, size, digest, or content evidence. `record-inspect` reads this
record without a provider. `recover` is re-observation only: without authority
it returns a recovery-required capability result; with authority it may report
`currently_converged`, never historical secret-content confirmation. The
metadata-only provider cache is opt-in and written only by `cache-refresh`;
cache status cannot authorize or alter reconciliation. `platform-check` is
diagnostic only, and unsupported filesystem/provider primitives fail closed
with `platform_unsupported`.

## Commands

All manifest commands accept `--manifest PATH` (default `luwu.toml`) and `--json`. Version-6 manifest commands additionally accept `--allow-subprocess` and `--rbw-executable ABSOLUTE_PATH`; both are required to run the provider. `inspect` and `plan` never write the manifest, source, target, or any state file. `record-inspect --record PATH` and `recover --record PATH` read only the named execution journal; neither requires a manifest argument or writes state. `cache-inspect`, `cache-refresh`, and `platform-check` are separate diagnostic commands; only `cache-refresh` writes, and it writes a metadata-only owner-only cache.

`inspect` reports the current state. `plan` reports the same observation together with the action an explicit apply could take. Both commands return exit code `0` after successfully calculating a plan, including when a resource is reported as `blocked`.

`apply` always calculates a plan first. In human output, a confirmed version 1 apply prints that plan before writing. Without `--yes`, it is only a preview, writes nothing, and returns exit code `2`. Version 1 with `--yes` rechecks every target against the calculated state, writes only `create` or `replace` actions, and recalculates the current plan after writing. Versions 5 and 6 use their independent execution journal contracts and require `--record PATH` for a confirmed apply. Version 6 additionally requires explicit subprocess authority and writes only the external secret target after owner-only preflight. JSON mode keeps stdout as one result document; version 1 includes the initial and verification plans when verification can recalculate one, while versions 5 and 6 include only preview/journal metadata, target labels, states, outcomes, and changed target labels. A blocked, stale, or version 2/3/4 read-only plan returns exit code `2` and does not begin a write. A version 2 `apply` preview reports `m2_read_only`; a version 3 or 4 preview reports `m3_read_only`; a plan containing an unsafe resource reports `plan_blocked`.

`accept` and `reverse-sync` require `--resource`, one or more `--field` values, and explicit `--yes` confirmation for mutation. Without `--yes`, both return exit code `2` and emit a metadata-only preview. The preview is explanatory and is not a persisted plan token; `--yes` recalculates the current inputs and then performs the documented stale checks. Successful mutation returns exit code `0`, records the selected fields and write path, and includes a fresh verification plan. The result never contains accepted values, rendered content, diffs, or hashes.

Writes use a temporary file or temporary symlink in the target's existing parent followed by an atomic replacement. Existing regular-file permissions are preserved for template targets; a new regular template target starts with mode `0644`. A template target symlink is refused rather than followed or replaced. A symbolic target symlink is accepted only when it resolves to the declared source; a different existing symlink is blocked. The current implementation uses descriptor-relative no-follow operations, records source/target identities, and takes a non-blocking advisory lock for cooperating Luwu writers on POSIX. It fails closed when those filesystem primitives are unavailable. An advisory lock does not control unrelated writers that ignore it, so M1 does not claim protection against those races; a stronger kernel compare-and-swap boundary is future work. M1 does not create parent directories, keep a baseline, or make a backup containing configuration content.

Version 6 uses the same recoverable atomic replacement boundary only after
stricter secret-target checks. It never creates a parent directory, follows a
target symlink, or accepts a target with group/other/special permissions,
multiple hard links, or an unverified owner. Its journal is owner-only and
separate from the version-5 path-condition record.

## States and actions

| State        | Meaning                                                                             | Action         |
| ------------ | ----------------------------------------------------------------------------------- | -------------- |
| `in_sync`    | rendered bytes equal a template target, or a symbolic target resolves to its source | `noop`         |
| `missing`    | target is absent                                                                    | `create`       |
| `formatting` | reserved for a future format adapter; M1 does not emit it                           | not applicable |
| `drifted`    | rendered content differs, or a symbolic target is a regular file                    | `replace`      |
| `blocked`    | the target boundary or declared input cannot be handled safely                      | `block`        |

M1 has no formatting equivalence. Without a format adapter or parser evidence, any byte difference—including line endings, trailing whitespace, and final-newline differences—is `drifted` and may be `replace`d. The reserved `formatting` value is not emitted by M1.

The table above is the M1 exact-byte action table. In M2, `comparison = "json"` uses this additional observation table:

| State        | Meaning                                                   | Action              |
| ------------ | --------------------------------------------------------- | ------------------- |
| `missing`    | target is absent; the selected comparison was not run     | `create` (deferred) |
| `in_sync`    | strict JSON bytes are equal                               | `noop`              |
| `formatting` | strict JSON values are equal but represented differently  | `noop`              |
| `drifted`    | strict JSON values differ                                 | `report`            |
| `blocked`    | strict JSON parsing or the target boundary is unsupported | `block`             |

For version 2 `comparison = "json"`, the rendered template bytes and live target bytes are each parsed as strict UTF-8 JSON. Duplicate object keys, non-finite numbers, invalid UTF-8, isolated Unicode surrogates, comments, trailing commas, and other unsupported syntax are `blocked`. Unknown fields, nested values, string whitespace, and array order remain part of the comparison; object key order, JSON representation whitespace, and different spellings of the same finite numeric value may be formatting-only differences. Booleans are never equal to numbers. A semantic difference is `drifted` with action `report`, never `replace`; an equivalent representation is `formatting` with action `noop`. The comparator returns metadata only and never includes configuration values.

## JSON output

Successful `inspect` and `plan` output has this shape:

```json
{
  "schema_version": 1,
  "command": "plan",
  "manifest": "/absolute/path/luwu.toml",
  "resources": [
    {
      "name": "settings",
      "kind": "template",
      "source": "templates/settings.conf.j2",
      "target": "live/settings.conf",
      "owner": "source",
      "scope": "whole-file",
      "status": "missing",
      "action": "create",
      "reason": "target does not exist",
      "impact": {
        "writes": ["live/settings.conf"],
        "overwrites": [],
        "scope": "whole-file",
        "undeclared": "content outside the declared target is not examined"
      }
    }
  ],
  "summary": {
    "total": 1,
    "changes": 1,
    "in_sync": 0,
    "formatting": 0,
    "blocked": 0
  }
}
```

`apply` adds `applied`, `mutated`, `outcome`, `changed_targets`, `verification`, and `verification_error`. A confirmation preview has `applied = false` and `reason = "confirmation_required"`; a blocked apply uses `reason = "plan_blocked"`. A successful write has `outcome = "committed"`; a successful no-op has `outcome = "no_changes"`; both return exit code `0`, with `mutated` distinguishing whether a target was written. If a target was committed but verification could not recalculate a clean plan, the result has `outcome = "committed_but_verification_failed"`; if commit happened but durability or cleanup could not be confirmed, it has `outcome = "committed_state_unknown"`. If no target was written and verification failed, the outcome is `verification_failed`. All failure outcomes return exit code `2`; committed outcomes include every known changed target and callers must inspect before retrying. Rendered bytes, variable values, diffs, and hashes are intentionally absent from machine-readable output.

Version 2 successful observation output uses `schema_version = 2`, includes `manifest_version = 2`, `applyable = false`, and `apply_block_reason`. Version 2 summaries additionally include `reported`; resource entries may include metadata-only `comparison` results with `strategy`, `status`, `code`, `reason`, and `equivalent`. Version 3 and 4 observations use their matching schema and manifest versions, remain `applyable = false`, and include metadata-only field ownership. M3b mutation results use `schema_version = 4`, identify the operation/resource/selected fields/write path, and never include field values, rendered content, diffs, or hashes.

Version 5 apply output has `preview`, `journal`, `target_names`, `state`, `outcome`, and `changed_targets`; a preview has `journal.created = false`, and a failed confirmed execution includes the readable journal state so partial success is not hidden. `record-inspect` emits the same journal metadata without path conditions or transition history. `recover --record PATH --json` emits `schema_version`, `command = "recover"`, `mode = "reobserve-only"`, `plan_id`, `record_state`, `outcome`, and metadata-only per-resource/path observations; human output reports the same state and outcome without configuration values. The version-5 journal condition domain is closed: condition types are `missing`, `unsafe`, `regular`, `symlink`, or `other`; `mode` is `0..0o777`, sizes and file identities are non-negative integers, and timestamps may be signed integers. Empty resources, ordinal gaps, and a non-contiguous `next_ordinal` are invalid.

Version 6 plan and apply output uses `schema_version = 6`, public resource
labels such as `resource-0`, safe status/action/reason/impact metadata, and no
source, target, provider item/field, provider reference, or secret-derived
content metadata. Confirmed execution reports the v6 journal contract and
resource states using labels. `record-inspect` and `recover` retain the same
safe projection; a v6 recovery with authority can report current convergence
but never `confirmed` historical content.

Once version 5 execution has started tracking a validated plan, errors also include an `execution` object with exactly `plan_id`, `committed`, `changed_targets`, and ordered `resources`. Each resource contains only `name`, `target`, and `state`. These are this invocation's in-memory target observations; `journal` separately describes the record actually readable from disk. A missing or unreadable journal does not discard known target outcomes. Human output exposes the same target paths and states.

If journal diagnostics cannot even determine whether the record exists, `journal.created` is `null` and its state is `unreadable`; the error does not claim the record is absent. Successful existence checks retain the usual boolean value even if reading the record subsequently fails.

The error context distinguishes `not-attempted` (the target writer was not entered), `failed` (the writer was attempted and explicitly reported no replacement), `unknown` (replacement, durability, cleanup, or target verification is uncertain), `committed` (the writer and target postcondition succeeded), and `unchanged` (a no-op was processed). A replace-boundary `indeterminate` result is exposed as an `unknown` resource and `recovery_required` outcome with `committed = false` when no staged identity proves a replacement; it is never inferred from equal target bytes. `failed` is an error-context state only; the persistent journal schema is unchanged. Subsequent journal failure does not downgrade a known committed target. `changed_targets` lists only known target replacements in plan order, including those followed by an uncertain durability outcome, and `committed` is true exactly when that list is nonempty. It does not describe journal publication: an initial journal failure before target writes has `committed = false`, while a final journal failure after target writes retains `committed = true`. These fields contain no configuration values, conditions, content digests, or exception payloads.

Expected failures use exit code `2`. With `--json`, they are emitted as:

```json
{
  "error": {
    "code": "stale_plan",
    "message": "target for resource 'settings' changed after planning; run plan again"
  }
}
```

Error messages identify the failing boundary but never print rendered content or variable values. No other output format is a compatibility promise in M1.

## Compatibility

The manifest `version` and JSON `schema_version` are independent explicit contracts. M1 uses version `1` for both; M2 uses version `2`; M3a uses version `3`; M3b uses manifest and mutation result version `4`; M3c execution and re-observation use version `5`; M4 provider execution and re-observation use version `6`. A future incompatible change must introduce a new version or a deliberate migration. There is no migration or automatic recovery mutation command.
