# Luwu Public Reference

Status: current manifest, CLI, JSON, error, and compatibility contract.

This document owns the manifest, CLI, JSON, error, and compatibility contracts. The [changelog](changelog.md) maps delivery names to capabilities. Product direction belongs in [product.md](product.md); implementation rationale belongs in [design.md](design.md); verified scope belongs in [status.md](status.md).

## Manifest and profile contract

Luwu has one manifest contract, `version = 8`. Earlier manifest versions were
retired; a manifest with any other version is rejected with
`manifest_version`. There is no migration. Current implementation status is
owned by [status.md](status.md) and open constraints by the
[roadmap](roadmap.md).

### Manifest, profiles, and package closure

A manifest has `version = 8` and declares [roots] with source, inputs,
and target paths; named [profiles.NAME]; named [packages.NAME]; named inputs;
and named resources. Source and inputs are relative to the manifest directory;
target may be relative to it or an explicit absolute path. All three roots must
already exist as safe directories.
Source and inputs roots must be distinct and non-overlapping. The target root
may be separate or may contain either authority root, but cannot alias or lie
inside either. The loader rejects unsafe aliases and conflicting declared
manifest, source, input, target, baseline, and directory paths, including
ancestor conflicts across resources. Each directory leaf remains within its
declared directory scope. Only declared source files are validated at load
time. A live target, baseline, or input file that is a symlink, a hard link, or
otherwise unsafe does not abort loading: the plan reports that one resource as
`blocked` with the fixed code of the failed boundary, and the other resources
are still observed. No undeclared path gains authority from a profile or
from a configuration value.

Every version 8 command requires an explicit --profile NAME. A profile
declares package names and an exclude list, which may be empty. A package
declares its dependencies and resource names. Luwu computes the transitive
dependency closure, orders included packages by a stable topological order,
and expands their resources deterministically. Exclusions take precedence
over selection;
if an included package depends on an excluded package, the profile is invalid
and planning stops. Exclusion is applied before any stat, open, or directory
walk of excluded resources or inputs used only by them; validating their
declarations does not grant permission to inspect their content. Only inputs
referenced by selected resources are read. Plans identify included and
excluded packages. Selection never depends on implicit platform detection;
platform-specific non-ownership is expressed by the selected profile's
exclusions.

### Declared inputs and overlays

Each named input binding declares an ordered layers list. A layer contains a
file path relative to the inputs root and under its global or local directory,
a literal path expressed as a list of string segments, a format (json, jsonc,
or toml), and an optional flag. The path selects one scalar value. A missing
layer or value is ignored only when that layer is optional; otherwise it blocks
the binding. Among present layers, the later declared value takes precedence
and the result retains metadata-only provenance for the winning layer. This
supports explicit global/local overlays without scanning for files or
inferring aliases.

Bindings may share one input file only when they declare the same format and
distinct, non-overlapping paths. Cross-role aliases between input, target,
and baseline paths are invalid. A reverse destination must be one of the
binding's layers and must win after the selected write: it may be the current
winner, or a higher-precedence layer that is currently absent or lacks the
value. Reverse sync cannot write a layer that a higher layer would still
shadow.

A binding may declare one reverse_to destination as a non-optional input
layer. Reverse sync can write only that declared file and literal path for a
field that names the binding. Luwu may create the selected declared destination value or file as required by
the supported format patch. Missing parent directories of the destination are
created only when the resource declares `parents = "create"`, using its
`parent_mode`; with `parents = "require"` the result is `parent_required`
and nothing is written. Luwu never writes other keys. Before writing, reverse sync simulates the change across all
consumers in the selected profile. It blocks if an unselected consumer's
desired value would change semantically, shared consumers require inconsistent
values, or paths overlap. It groups edits to each file into one patch, then
verifies every consumer and preserved unselected content after writing.
Source-owned fields may omit an input binding. Live- and merge-owned fields
require one; ignored fields do not create a write destination. Version 8
inputs are public scalar data and have no provider or secret-value path.

For a shared input, if resources A and B consume the same leaf `x = 1` and
both have live value `2`, reversing either one alone is blocked because
changing `x` to `2` would change the other, unselected consumer's desired
value. Matching live values do not authorize a joint change. The operator can
split the binding or input leaf into separate declared destinations, or
manually change the shared public input and run a new plan.
When this simulation blocks reverse sync because the proposed write would
change another declared consumer, the fixed error code is
`reverse_consumer_change` with the message `reverse sync would change another declared consumer`.

### Resources and declared scope

A resource declares kind, source and target relative to their respective roots,
baseline relative to the inputs root, content_sensitivity = public, parents,
and parent_mode. Template resources additionally declare a supported
structured format, scope = fields,
and a non-empty field map. Each field alias maps to a literal path, an owner
(source, live, merge, or ignore), and, where required, a named input binding.
Mapped paths may not overlap. Dots, slashes, and array positions have no
special path syntax; a path is the exact ordered tuple of its declared string
segments. There are no built-in application schemas or inferred field
mappings; application configuration is handled only through these declarations.

Copy and directory resources declare scope = whole-file and owner = source;
neither has a structured field map or format. Copy resources own their complete
regular-file bytes. An existing copy target is not authorized by observation
or by apply: it requires
an explicit adopt operation before source deployment. The copy baseline uses
its closed payload variant to store the digest algorithm and accepted public
byte digest as content evidence; accepted file mode is represented separately.
It does not persist file bytes. Explicit public admission does not redact
secrets. Subsequent source/live disagreement requires review; there is no
force or implicit overwrite rule.

Directory resources explicitly enumerate a source directory into a
deterministic set of copy leaves. Recursive traversal is enabled by default
and may be narrowed by declared relative exclusion globs. Symlinks are not
followed, undeclared entries are not silently added, and an empty directory
does not create a target directory. The expanded leaf set and its identities
are part of the observed manifest selection and are rechecked before a write;
an added, removed, or rebound leaf makes the plan stale or blocked. Each leaf
has a deterministic, collision-free baseline identity formed from the
directory declaration identity and relative leaf path, never enumeration
order.

Every resource explicitly declares whether target and baseline parents must
already exist or may be created, and supplies parent_mode for directories
created under that resource. The same policy governs the
parents of a reverse-sync destination. parent_mode
and mode values are plain permission bits from 0o000 through 0o777; setuid,
setgid, and sticky bits are invalid. Final modes are exact and do not inherit
the process umask. A new parent_mode with group-write or world-write bits is
rejected during full preflight before any write. Existing directory modes are
never changed. The optional file mode applies to a new or replaced regular
file; when omitted, an existing target's mode is preserved and a new file
uses mode 0644. Each resource projection reports `mode` as the declared mode
string or `null`. It reports `effective_mode` as the same mode string when an
explicit mode is declared; when mode is omitted, it reports `0o0644` for a
missing target or the observed mode for an existing target. Both fields use
octal strings such as `0o0644`; `effective_mode` is `null` when no explicit
mode is declared and the target state or existing mode cannot be observed. The
projection also reports every required parent creation.

For version 8 writes, each actual namespace root used for a target or
input/baseline destination, and every existing directory beneath it through
that destination, must be owned by the current user or root and must not be
group- or world-writable. This rule does not apply to ancestors above the
selected root, so a private root below a sticky `/tmp` remains usable. The
write namespace must not be controlled by untrusted users. Symlink components
and hard-linked files remain refused. Cooperating Luwu writers must honor the
same advisory lock. These checks do not protect against a malicious process
running as the same user, privileged actors, or a malicious filesystem; Luwu
makes no race-free guarantee against them.

Plans bind each existing directory ancestor by identity and record every
missing parent in order. Shared parent creation is deduplicated; conflicting
requested modes for one parent block the batch before its first write. Writes
recheck the complete recorded ancestry and missing chain before staging and
immediately before publication. A parent-creation result carries the observed
and rechecked identity at its exact planned path; that observation does not
prove exclusive creation provenance. Matching path or mode alone does not
authorize adopting a replacement. A detected substitution is refused, or
reported as uncertain when the namespace may already have changed. If a
parent has been created and a later verification, recheck, or sync fails, the
creation remains committed and its result is uncertain, with the observed
identity when available. The [design](design.md#module-responsibilities)
owns the syscall and descriptor-check sequence.

### Structured formats and field comparison

The supported structured formats are strict JSON, JSONC, and nested TOML.
Luwu parses the complete document before comparing or patching it. Duplicate
keys, non-finite numbers, invalid encoding, unsupported syntax, and decoded
sensitive key names anywhere in the document block the operation. JSONC
supports comments and trailing commas. TOML uses its full nested table
structure. Arrays can be observed and retained, but array members cannot be
selected or edited.

Declared paths select scalar leaves only: strings, booleans, integers,
Decimal numbers, and null for JSON formats. Missing values remain distinct
from present null; a path selecting a container is rejected. Equality is
recursive and type-strict, so booleans and integers are not interchangeable.
Ancestors needed to reach selected leaves do not create undeclared drift.
Version 8 leaf paths contain one to 256 literal string segments. Numeric
literals emitted by a structured patch are limited to 4096 characters. TOML
multiline values may be observed but cannot be selected for editing.

A structured patch changes only selected scalar spans and the syntax required
to add or remove those selected values. It may create mapping/object or TOML
table ancestors only when a selected path is missing; an existing scalar or
array ancestor blocks insertion. Selected edit paths may not overlap, including
update/delete combinations. It preserves unselected bytes and unknown
containers, reparses the result, and verifies the postcondition. Comparison
masks selected leaves and only mapping ancestors that the selected insertion
necessarily creates because they were absent. Existing empty containers,
arrays, and type changes remain observable. Accept and adopt may resolve the
selected baseline state but never accept undeclared content; unknown mismatch
blocks until the declaration or content is explicitly corrected. If a safe
patch or its unchanged document regions cannot be proven, mutation stops before
writing. No general configuration-tree rewrite or format plugin is implied.
When undeclared content changes for a resource, its plan projection has
`status = "review"`, `action = "blocked"`, `undeclared_changed = true`, and the
`review_required` error code. Its fixed `reason` is `undeclared content changed; inspect declared scope or correct it manually, then re-plan`.
This message gives an action without disclosing undeclared keys or values.

### Fixed-version application-shaped mapping targets

The following fixed-version shapes are explicit version 8 acceptance targets.
They are ordinary declared literal paths, not built-in schemas or inferred
mappings. Every field still requires an explicit resource declaration and
ownership decision.
The listed values describe the fixed application versions' shapes; generic
Luwu declarations do not include application-specific enum validation, so
these lists do not imply that Luwu enforces those value domains.

- **OpenCode v1.19.0**: the config is JSONC-capable. The supported declared
  paths are `["model"]`, `["server", "port"]`, `["server", "hostname"]`,
  and `["autoupdate"]`. The fixed-version parser and config reference are
  [parse.ts](https://github.com/anomalyco/opencode/blob/v1.19.0/packages/opencode/src/config/parse.ts)
  and [config.mdx](https://github.com/anomalyco/opencode/blob/v1.19.0/packages/web/src/content/docs/config.mdx).
- **Codex v0.162.1**: in `config.toml`, `projects` is keyed by the literal
  project-path string. The declared trust path is
  `["projects", "<literal project path>", "trust_level"]`, where the
  placeholder is one key segment and the accepted values are `trusted` and
  `untrusted`. Hook-state paths use the full hook ID as one key segment:
  `["hooks", "state", "<full hook ID>", "enabled"]` and
  `["hooks", "state", "<full hook ID>", "trusted_hash"]`. The latter is an
  opaque, live-owned value that Luwu preserves without calculating or writing
  it to grant trust. Codex determines whether a hook definition changed and
  whether its trust remains valid. See the fixed-version
  [project config](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/config/src/config_toml.rs),
  [hook config](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/config/src/hook_config.rs),
  and [hook discovery](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/hooks/src/engine/discovery.rs#L768).
- **Droid 0.240.0**: for `~/.factory/settings.json`, the declared paths are
  `["sessionDefaultSettings", "interactionMode"]` with values `auto` or
  `spec`, and `["sessionDefaultSettings", "autonomyLevel"]` with values
  `off`, `low`, `medium`, or `high`. See the official
  [settings reference](https://docs.factory.com/droid-cli/settings). This
  target covers settings only. No stable public file schema for Droid
  workspace trust is claimed; SDK methods `workspace.checkTrust` and
  `workspace.trust` do not define file compatibility.

These shapes define compatibility targets, not evidence that an application
runtime loaded them. Current implementation and runtime verification status
remain in [status.md](status.md) and the [roadmap](roadmap.md).

### Baselines, adoption, and explicit mutation

Baselines use schema version 2 with closed structured-field and
whole-file-copy payload variants. A structured baseline stores only declared
accepted public scalar values. A copy baseline stores its digest algorithm and
explicitly accepted public byte digest as content evidence, with accepted file
mode represented separately; it never stores the copied file bytes. These
persisted digest bytes are not metadata-only, but are explicit public baseline
content evidence. Baselines never contain complete source, target, or input
documents. Only explicit accept and adopt operations establish or advance
baselines; planning, reverse sync, and apply never accept state implicitly.

Durable baseline identity binds the profile, resource identity and kind, the
resolved paths of all three roots, source and target paths, and format. For a
template it also binds every field alias, tuple path, owner, and input binding,
plus each referenced binding's ordered layer descriptions (file, tuple path,
format, and optionality) and reverse destination. Directory leaf identity
uses its declaration and relative path. Root device/inode identities are
execution-freshness checks, not durable identity. A binding-identity mismatch
requires explicit reacceptance; there is no baseline migration. The plan reports
such a resource as `unbased` (or `adopt_required` for a copy) with error code
`baseline_stale` and a reason naming the changed declaration. Accepting or
adopting again replaces the old baseline; the old values are never reused. An accepted
copy digest and its separate mode evidence are stored only in that explicitly
written baseline and never included in any CLI output, repr, or log.

Inspect and plan are read-only. Accept requires an explicit profile, exactly
one resource, field selection, and --from desired|live baseline side.
Reverse-sync requires an explicit profile, exactly one resource, and selected
eligible live/merge fields; it writes only their declared reverse_to
destinations. Adopt requires an explicit profile and exactly one whole-file
copy resource. These are per-resource mutations; there is no joint
cross-resource selection for accept, adopt, or reverse-sync. Apply alone
mutates the complete selected profile as a batch. Every mutation has a
metadata-only preview and requires explicit --yes; confirmation
recalculates and rechecks the manifest, selected profile and package
expansion, source/input/baseline/target observations, directory snapshots,
path identities, and modes. At mutation entry, Luwu verifies the complete
caller-supplied plan projection against its issued evidence. It then verifies
the manifest, freshly observes the selected profile, verifies the manifest
again, and compares the newly derived complete plan projection with that same
evidence. Both checks must pass before a writer begins. Execution uses values
derived from this fresh authorized state, never mutable derived data from the
caller-supplied plan. Mutating the original plan's derived objects afterward
does not change authorized writes and is not a cancellation API. Shared
declaration objects and actual file dependencies remain subject to the
finite-batch and per-action checks below.

### Finite-batch freshness and verification

A confirmed profile mutation fixes its finite batch from the verified
manifest, selected profile and package closure, explicit resource selection,
and directory expansion. Before the first write, Luwu completes full-batch
preflight across every selected resource and expanded directory leaf,
including no-op resources. The blocked outcome includes all blockers found in
that observation; any blocker prevents every write from starting.

Before staging or creating each action, Luwu revalidates the common batch
authority by rereading and revalidating the manifest bytes, selected
profile/package closure, all three root identities, and directory expansion.
It also rereads every file dependency of the current action and compares
complete bytes and presence, along with applicable path identities and modes.
Modification-time-only or other metadata-only freshness checks are insufficient.
For a staged file write,
the writer callback repeats the common and current-action checks immediately
before publication or replacement. Dependencies include the selected
resource's source, ordered overlay layers (including optional-layer
presence), accepted baseline, target, and any other declared content used to
authorize or produce that action.

For reverse sync, these checks also recalculate every declared consumer in the
selected profile that shares an affected input destination, using complete
current bytes. No consumer may be omitted from the simulation or verification.

After action processing finishes or stops at its first failure, Luwu performs
a fresh whole-batch observation of every selected resource, including no-op
resources and actions already written. Any mismatch in this final observation, including
a changed no-op, fails final verification; the batch must not be reported as
verified. Known commits and uncertain writes remain represented in the
partial outcome, with no rollback.

These checks define a finite rolling freshness boundary, not one simultaneous
snapshot for every resource at each write. An unrelated later resource may
change after initial preflight and be discovered only at its own action
boundary. Earlier writes remain committed and the result is partial; Luwu does
not roll them back. The batch also has no replay, durable plan, or execution
journal. A fresh plan is required after an interruption or uncertain result.

All version 8 results use metadata-only schema_version = 8 projections and
fixed safe LuwuError messages. An inspect or plan result contains
schema_version, command, profile, the ordered included package list, the
excluded package list, and resource projections. A mutation result contains
schema_version, operation, command, outcome, ordered actions,
verification_status, and an error_code when applicable. Each action identifies
its kind, resource, relative path, state, and selected field labels.
CLI mutation commands return exit code 0 only for committed or no-change
outcomes; other outcomes return 2. Results and errors never expose values,
document bytes, diffs, content hashes, secret material, raw parser messages,
filesystem exception text, or exception payloads. Only allowlisted version 8
codes are projected; an unknown internal code is normalized to
`resource_blocked`. The version 8 error projection and the fixed meanings
defined in this contract are covered by the tests; the verified state is in
[status.md](status.md).

## Commands

Luwu has six commands: `inspect`, `plan`, `apply`, `accept`, `adopt`, and `reverse-sync`. Every command requires `--profile NAME` and accepts `--manifest PATH` (default `luwu.toml`) and `--json`. There is no provider, subprocess, journal, cache, or recovery command.

`inspect` reports the current state. `plan` reports the same observation together with the action an explicit apply could take. Neither writes the manifest, source, target, input, or baseline. Both return exit code `0` after successfully calculating a plan, including when a resource is reported as `blocked`.

`apply` always calculates a plan first. Without `--yes` it is only a preview, writes nothing, and returns exit code `2`. With `--yes` it completes a full profile preflight, rechecks every dependency, and then writes. Any blocked, review, or unbased resource in the selected profile stops the whole batch before the first write (`review_required`); resolving that resource is a separate explicit step. `apply` uses no execution record.

`accept` requires `--resource`, one or more `--field` values, and `--from desired|live`. `reverse-sync` requires `--resource` and one or more `--field` values. `adopt` requires `--resource` naming a whole-file copy. Without `--yes`, a mutation command emits a metadata-only preview and returns exit code `2`. The preview is not a persisted plan token; `--yes` recalculates the current inputs and then performs the documented stale checks. A successful mutation returns exit code `0` and includes a fresh verification result. Results never contain accepted values, rendered content, diffs, or hashes.

Writes use a temporary file in the target's existing parent followed by an atomic replacement. Existing regular-file permissions are preserved unless a mode is declared. A target symlink is refused rather than followed or replaced. Luwu uses descriptor-relative no-follow operations, records identities, and takes a non-blocking advisory lock for cooperating Luwu writers on POSIX. It fails closed when those filesystem primitives are unavailable. An advisory lock does not control unrelated writers that ignore it.

Expected failures use exit code `2`. With `--json` they are emitted as:

```json
{
  "error": {
    "code": "stale_plan",
    "message": "plan inputs changed; build a new plan"
  }
}
```

Error messages identify the failing boundary but never print rendered content or input values.

### Dotter handoff

A target must be explicitly handed off to Luwu one target at a time before
Luwu manages it. Dotter and Luwu must not both manage the same target. The
handoff and any return to Dotter are operator actions; the pilot does not edit
Dotter configuration, remove or create links, or adopt targets implicitly.
The repeatable handoff and return procedure belongs in
[maintenance.md](maintenance.md).

## Compatibility

The manifest `version` and JSON `schema_version` are independent explicit contracts; both are currently `8`. A future incompatible change must introduce a new version or a deliberate migration. There is no migration from an earlier manifest and no automatic conversion of an existing Dotter configuration.
