# Luwu M1/M2 Reference

Status: current preview contract

This document owns the stable manifest, CLI, JSON, error, and compatibility contract for the developer-confidence preview and its M2 read-only extension. Product direction belongs in [product.md](product.md); implementation rationale belongs in [design.md](design.md); verified scope belongs in [status.md](status.md).

## Manifest

The manifest is a UTF-8 TOML file. Its root is the directory that scopes every declared source and target. M1 uses manifest version `1`; M2 supports versions `1` and `2`.

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

## Commands

All commands accept `--manifest PATH` (default `luwu.toml`) and `--json`. `inspect` and `plan` never write the manifest, source, target, or any state file.

`inspect` reports the current state. `plan` reports the same observation together with the action an explicit apply could take. Both commands return exit code `0` after successfully calculating a plan, including when a resource is reported as `blocked`.

`apply` always calculates a plan first. In human output, a confirmed apply prints that plan before writing. Without `--yes`, it is only a preview, writes nothing, and returns exit code `2`. With `--yes`, it rechecks every target against the calculated state, writes only `create` or `replace` actions, and recalculates the current plan after writing. JSON mode keeps stdout as one result document and includes the initial and verification plans in that document when verification can recalculate one. A blocked, stale, or version 2 read-only plan returns exit code `2` and does not begin a write. A version 2 `apply` preview reports `m2_read_only`; a plan containing an unsafe resource reports `plan_blocked`.

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

Version 2 successful observation output uses `schema_version = 2`, includes `manifest_version = 2`, `applyable = false`, and `apply_block_reason`. Version 2 summaries additionally include `reported`; resource entries may include metadata-only `comparison` results with `strategy`, `status`, `code`, `reason`, and `equivalent`. These fields never contain rendered configuration content.

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

The manifest `version` and JSON `schema_version` are independent explicit contracts. M1 uses version `1` for both; M2 uses manifest version `2` and schema version `2` for its read-only observation output. A future incompatible change must introduce a new version or a deliberate migration; M1/M2 have no migration command.
