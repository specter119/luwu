# Luwu M1/M2/M3a/M3b/M3c Reference

Status: current preview contract

This document owns the stable manifest, CLI, JSON, error, and compatibility contract for the developer-confidence preview and its M2, M3a, narrow single-resource M3b, and explicit M3c execution extensions. Product direction belongs in [product.md](product.md); implementation rationale belongs in [design.md](design.md); verified scope belongs in [status.md](status.md).

## Manifest

The manifest is a UTF-8 TOML file. Its root is the directory that scopes every declared source, target, and baseline. M1 uses manifest version `1`; M2 supports versions `1` and `2`; M3a adds version `3`; M3b adds version `4`; the current M3c execution slice adds version `5`.

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

`accept` writes only explicitly selected declared fields to the baseline envelope, from either the current desired or live object. `reverse-sync` writes only selected `live_changed`/`reverse_candidate` fields through their explicit source mapping. Neither command copies a complete live file into a Jinja template, updates the baseline implicitly, or accepts undeclared content. Version 4 `apply` remains read-only; version 5 is the separate current multi-resource execution capability.

### M3c manifest version 5 execution slice

Version `5` is an independent execution capability, not an upgrade of versions `1`–`4`. It accepts multiple resources only when each resource explicitly declares `kind = "template"` or `kind = "symbolic"`, `owner = "source"`, `scope = "whole-file"`, and `content_sensitivity = "public"`. It rejects comparison, fields, baseline, reverse-sync, provider, and secret fields. Resources are ordered by name, and duplicate or overlapping source/target paths are rejected before planning.

Version 5 `apply` uses a durable metadata-only journal. Without `--yes`, it is a pure preview and does not create the journal. With `--yes`, it requires an explicit `--record PATH`, completes full preflight for every resource before the first writer, and processes resources in stable order. Failure stops at the first failing resource; rollback is never attempted. Each resource is recorded as `unchanged`, `committed`, `unknown`, or `not-attempted`. A committed write whose durability or cleanup cannot be confirmed is reported as `recovery_required` or `unknown`, never as a clean success.

The journal has a closed schema and stores only execution/mutation contracts, manifest identity, relative resource/path metadata, non-content file conditions, and state transitions. It does not store configuration values, bytes, rendered output, diffs, patches, provider payloads, secrets, or source/target content hashes. `record-inspect` (also accepted as `inspect-record`) accepts only the version-5 execution contract, reads that journal without replaying inputs or writing files, and rejects legacy records. `recover --record PATH` (also accepted as `record-reobserve`) re-observes the recorded manifest and paths without writing; it returns `confirmed` only when the recorded boundary is still observed and otherwise returns `recovery_required`. There is no automatic replay, rollback, or recovery mutation command in this slice.

## Commands

All manifest commands accept `--manifest PATH` (default `luwu.toml`) and `--json`. `inspect` and `plan` never write the manifest, source, target, or any state file. `record-inspect --record PATH` and `recover --record PATH` read only the named execution journal; neither requires a manifest argument or writes state.

`inspect` reports the current state. `plan` reports the same observation together with the action an explicit apply could take. Both commands return exit code `0` after successfully calculating a plan, including when a resource is reported as `blocked`.

`apply` always calculates a plan first. In human output, a confirmed version 1 apply prints that plan before writing. Without `--yes`, it is only a preview, writes nothing, and returns exit code `2`. Version 1 with `--yes` rechecks every target against the calculated state, writes only `create` or `replace` actions, and recalculates the current plan after writing. Version 5 uses the execution journal contract described above and requires `--record PATH` for a confirmed apply. JSON mode keeps stdout as one result document; version 1 includes the initial and verification plans when verification can recalculate one, while version 5 includes only preview/journal metadata, target names, states, outcomes, and changed target names. A blocked, stale, or version 2/3/4 read-only plan returns exit code `2` and does not begin a write. A version 2 `apply` preview reports `m2_read_only`; a version 3 or 4 preview reports `m3_read_only`; a plan containing an unsafe resource reports `plan_blocked`.

`accept` and `reverse-sync` require `--resource`, one or more `--field` values, and explicit `--yes` confirmation for mutation. Without `--yes`, both return exit code `2` and emit a metadata-only preview. The preview is explanatory and is not a persisted plan token; `--yes` recalculates the current inputs and then performs the documented stale checks. Successful mutation returns exit code `0`, records the selected fields and write path, and includes a fresh verification plan. The result never contains accepted values, rendered content, diffs, or hashes.

Writes use a temporary file or temporary symlink in the target's existing parent followed by an atomic replacement. Existing regular-file permissions are preserved for template targets; a new regular template target starts with mode `0644`. A template target symlink is refused rather than followed or replaced. A symbolic target symlink is accepted only when it resolves to the declared source; a different existing symlink is blocked. The current implementation uses descriptor-relative no-follow operations, records source/target identities, and takes a non-blocking advisory lock for cooperating Luwu writers on POSIX. It fails closed when those filesystem primitives are unavailable. An advisory lock does not control unrelated writers that ignore it, so M1 does not claim protection against those races; a stronger kernel compare-and-swap boundary is future work. M1 does not create parent directories, keep a baseline, or make a backup containing configuration content.

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

Version 5 apply output has `preview`, `journal`, `target_names`, `state`, `outcome`, and `changed_targets`; a preview has `journal.created = false`, and a failed confirmed execution includes the readable journal state so partial success is not hidden. `record-inspect` emits the same journal metadata without path conditions or transition history. `recover --record PATH --json` emits `schema_version`, `command = "recover"`, `mode = "reobserve-only"`, `plan_id`, `record_state`, `outcome`, and metadata-only per-resource/path observations; human output reports the same state and outcome without configuration values.

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

The manifest `version` and JSON `schema_version` are independent explicit contracts. M1 uses version `1` for both; M2 uses version `2`; M3a uses version `3`; M3b uses manifest and mutation result version `4`; M3c execution and re-observation use version `5`. A future incompatible change must introduce a new version or a deliberate migration. There is no migration or automatic recovery mutation command.
