# Luwu M1 Design

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

The plan keeps rendered bytes in memory only so the apply step can use exactly what was inspected. They are not persisted, printed, hashed into output, or copied into a baseline. Applying a plan first validates the declared target, including no-op observations, so an already-stale target cannot be silently ignored. M1 accepts exactly one resource and therefore does not define a multi-file transaction or rollback contract. Each replacement is atomic, and the existing target remains untouched until the final replacement step. On the supported POSIX path, apply walks the manifest root with descriptor-relative `O_NOFOLLOW` operations, creates its temporary entry in the held target directory, and replaces the target through that directory descriptor; unsupported filesystem primitives fail closed.

## Ownership and scope

M1 supports only `owner = "source"` and `scope = "whole-file"`. That is an explicit limitation, not a generic precedence rule: the declared template or source path owns the complete target path, while anything outside that target is outside the resource. A live-side difference is reported as drift and is never reverse-synced or silently adopted. Field ownership, baselines, merge decisions, and provider values require a later contract.

## Semantic observation

For templates, the desired side is always the rendered template. Exact byte equality is `in_sync`. A narrow text normalization identifies incidental line-ending, trailing-space, and final-newline differences as `formatting`; formatting is informational and produces no write action. For symbolic resources, the desired side is the source path resolved within the manifest root. All other readable differences are `drifted`. Missing targets and unsafe target boundaries are separate states so the next action is visible.

This deliberately avoids pretending that a generic formatter or parser can safely preserve every configuration language. Structured comparison and field-level reverse sync are later experiments with their own contracts.

## Safety boundaries

The normative manifest, CLI, error, and write contract is owned by
[reference.md](reference.md); this section does not repeat its field and state
tables. The design consequences are deliberately narrow: M1 remains
single-resource and forward-only, keeps the plan in memory, and defers
providers, secret persistence, reverse sync, and multi-file transaction
semantics until they have their own contracts. The descriptor-relative,
no-follow implementation is the mechanism that preserves the target boundary
described by the reference contract.
