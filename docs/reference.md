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
named resources; optional named [providers.NAME] tables; and an optional
[defaults] table. Source and inputs are relative to the manifest directory;
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
inputs are public scalar data and have no provider or secret-value path; a
provider value reaches a document only through a provider-owned field (see
[Provider-owned fields](#provider-owned-fields)).
Each binding parses the whole layer file, so a sensitive-looking key name
anywhere in a shared file blocks every binding that does not list that name in
its `public_names`. The consumers are blocked with the fixed error code
`sensitive_input`; any other unreadable layer uses `input_invalid`. Neither
code names the key, the value, or the file.

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

A resource is one `[resources.NAME]` table and one target file; nested tables
are its fields. It declares `source` and `target` relative to their respective
roots. It may also declare `baseline` relative to the inputs root, `deploy`,
`mode`, `parents`, `parent_mode`, `content_sensitivity = "public"`, and the
keys named below. There is no copy or directory kind: Luwu infers what a
resource is from its source.

- A source name ending in `.j2` is a template. Any other regular file is a
  plain file. A source that is a directory expands recursively into one
  resource per file leaf. The optional `kind = "template"` or `kind = "file"`
  overrides the inference for a file source; there is no other kind value.
- An optional top-level `[defaults]` table sets `deploy`, `parents`, and
  `parent_mode` for every resource. A resource key overrides it. Without a
  declaration, `deploy = "symlink"`, `parents = "require"`, and
  `parent_mode = 0o700`.
- `format` (`json`, `jsonc`, or `toml`) is inferred from the source name after
  removing `.j2`, or declared explicitly. A template with `fields` requires one.

**Plain files with `deploy = "symlink"`** (the default) own a symlink at the
target whose content is the absolute path of the source file. A symlink
resource has no baseline, no `mode`, and no adopt step. A missing target is
planned as `create`. A target that is already a symlink to that source is in
sync. A regular file, a symlink elsewhere, or a directory at the target
blocks the resource with a fixed reason; Luwu never follows or replaces it.
The link is created only when the name does not exist, and its content is read
back and checked after creation. A plain file with `deploy = "copy"` is a
copy resource.

**Copy resources** own the complete regular-file bytes of their source. They
require a `baseline`. An existing copy target is not authorized by observation
or by apply: it requires an explicit adopt operation before source
deployment. An absent target with no baseline file is planned as `create`:
apply writes the file only if the name is still absent, then records the
accepted digest and mode as the baseline. An unusable baseline file never
authorizes a create. The copy baseline uses its closed payload variant to store the
digest algorithm and accepted public byte digest as content evidence; accepted
file mode is represented separately. It does not persist file bytes. Explicit
public admission does not redact secrets. Subsequent source/live disagreement
requires review; there is no force or implicit overwrite rule.

**Whole-file templates** are `.j2` sources without `fields`. The template is
rendered in a sandbox with the input bindings listed in the resource's
`inputs`, read as `inputs.NAME` (the effective value), `global_inputs.NAME`,
and `local_inputs.NAME`, and the rendered bytes are owned by the source. They use
the copy baseline, adopt, and review mechanisms on the rendered bytes. A
template is written, never deployed, so `deploy` is invalid on it. The target
name of a directory leaf drops its `.j2` suffix.

**Structured templates** declare `fields`, a non-empty map. Each field alias
maps to a literal path, an owner (`source`, `live`, `merge`, `ignore`, or
`provider`), and, where required, a named input binding or provider. A structured template takes its inputs
from its field bindings and does not declare `inputs`. Mapped paths may not
overlap. Dots, slashes, and array positions have no special path syntax; a
path is the exact ordered tuple of its declared string segments. There are no
built-in application schemas or inferred field mappings; application
configuration is handled only through these declarations.

A structured template may declare two optional keys. `undeclared = "block"`
is the default: a change to undeclared content makes the resource `review`,
as described under structured formats. `undeclared = "preserve"` still
reports `undeclared_changed = true` but does not block. Apply then writes only
declared fields and keeps every other key as the application left it. Preserve
never adopts those keys. `public_names` is a list of exact key names that
sensitive-name detection would otherwise reject; a resource lists the names
that its fields or document may carry, and an input binding lists the names
that its layers may carry. Matching is by exact key name, the default stays
fail-closed, and the list does not redact secrets.

Directory expansion walks a source directory into a deterministic set of
leaves. Recursive traversal is enabled by default (`recursive = false` turns
it off) and may be narrowed by declared relative exclusion globs (`exclude`).
Each leaf is a plain file or a whole-file template, and takes the directory's
`deploy`. Symlinks are not followed, undeclared entries are not silently
added, and an empty directory does not create a target directory. The expanded
leaf set and its identities are part of the observed manifest selection and
are rechecked before a write; an added, removed, or rebound leaf makes the
plan stale or blocked. Each leaf has a deterministic, collision-free baseline
identity formed from the directory declaration identity and relative leaf
path, never enumeration order.
Every resource has a parents policy, declared or taken from `[defaults]`,
that says whether target and baseline parents must already exist or may be
created, and a parent_mode for directories created under that resource. The
same policy governs the parents of a reverse-sync destination. parent_mode
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
A resource with `undeclared = "preserve"` keeps `undeclared_changed = true`
but is not blocked on that account.

### Provider-owned fields

A structured template field may take its value from a provider instead of the
template or an input. Version 8 supports one provider type, `rbw` (the
Bitwarden command-line client), and scalar string leaves of JSON, JSONC, and
TOML documents. Whole-file text resources, copy resources, and directory leaves
cannot declare one.

**Declaration.** A top-level `[providers.NAME]` table has `type` (must be
`rbw`), `item`, and optional `field`. `item` and `field` are each one safe
argument: non-empty, not starting with `-`, at most
256 UTF-8 bytes, and without control characters. A field declares `owner = "provider"` and `provider = NAME` naming a declared provider, and no `input`.
A resource with a provider field must declare `mode`, and the mode must grant
nothing to group or others (`mode & 0o077 == 0`). A provider declared but not
consumed by a selected resource is never contacted. The key at exactly a
provider field's full path is exempt from the sensitive-name check, so the
document may carry `apiKey` there; every other sensitive key still blocks, and
`public_names` is not involved.

**Value and comparison.** The value is written into the rendered document at
the declared path after rendering, by the same selected-span patch as other
writes, and never passes through the template engine. A provider field is
forward-only and independent of any baseline: a live value equal to the
provider value is `unchanged`; a different or missing live value is
`source_changed` with a forward decision; for a provider field that status
means only that the live value differs from the provider value, so a hand edit
of the live file and a rotation at the provider look the same and neither is a
conflict. It is never a reverse candidate,
conflict, or `unbased`, and a resource whose only fields are provider fields
needs no baseline. A baseline lists a provider field among its owners and never
among its values; a baseline that holds a value for one is invalid. When
every declared field is otherwise unchanged, an existing target whose mode
differs from the declared mode plans as `drifted` with a mode-only write, so a
wide mode does not survive. While another field of the resource is a reverse
candidate or in conflict the mode is not repaired; the plan shows the effective
mode, and the repair follows once those fields settle.

**Operations.** `accept` and `reverse-sync --field` refuse a provider field
(`provider_field_refused`), and `reverse-sync --undeclared PATH` refuses a path
equal to or below a provider path (`undeclared_unsafe`). `reverse-sync` never
contacts a provider, so its consumer simulation needs no special case for
provider paths.

**Authority and fetching.** Only `inspect`, `plan`, and `apply` contact a
provider, and only when given `--provider-executable rbw=/absolute/path`. Any
other key or a relative path is a usage error. Without the option, each
resource that consumes a provider is `blocked` with
`provider_authority_required` and the other resources are observed as usual.
`accept`, `adopt`, and `reverse-sync` never contact a provider and do not accept
the option; for them a provider field is treated as unchanged, so the public
fields of the same resource stay usable. A plan built that way cannot be
applied. Values are fetched once per command, while the plan is built, and are
reused by every later check of that plan. Each CLI invocation builds its own
plan, so `apply` fetches the value itself and a value rotated between a `plan`
command and an `apply` command is written as the newer value.

The executable must be an absolute path to a regular file that is not a
symlink, owned by root or the current user, and executable by its owner. Neither
the file nor any ancestor directory may be writable by group or others, and
every ancestor must be owned by root or the current user; the walk stops at a
root-owned sticky directory such as `/tmp`, and the directory just below it is
checked for owner as well. Its signature (device, inode, size, mode,
modification and change times, link count) is checked before the first call and
again after the last, and a change fails the batch with
`provider_executable_changed`.

Luwu does this, in order:

1. Check by `lstat` that the agent socket `$XDG_RUNTIME_DIR/rbw/socket` (or
   `$TMPDIR` or `/tmp`, then `rbw-UID/socket` when `XDG_RUNTIME_DIR` is unset or
   relative) is a socket owned by the current user. Otherwise the result is
   `provider_agent_unavailable`. Luwu never connects to it.
1. Run `rbw unlocked`. A nonzero exit whose stderr contains `agent is locked`
   is `provider_locked`; any other nonzero exit is `provider_command_failed`.
1. Run `rbw get --raw -- ITEM` once per provider and select the value from the
   JSON: with no `field`, `data.password`; `password`, `username`, or `notes` for
   those builtins; any other `field` must match exactly one `fields[].name`
   exactly and case-sensitively. The value must be non-empty text without NUL.

Luwu never runs `rbw unlock`, `login`, or `sync`, and never starts an agent. The
child runs without a shell, with standard input closed, in `/`, in its own
session, with a 5 second timeout and 1 MiB per output stream, and is killed as a
process group on timeout or oversize output. Its environment holds only `HOME`,
`XDG_RUNTIME_DIR`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, and
`TMPDIR` when set, `PATH=/bin:/usr/bin`, and `RBW_AGENT=/bin/false`. Before the first call Luwu sets `RLIMIT_CORE` to 0 and
clears the process dumpable flag, and fails every provider if it cannot.
The child's standard error is discarded.

**Failures and output.** A provider failure blocks only the resources that
consume it, with one fixed code: `provider_authority_required`,
`provider_executable_invalid`, `provider_executable_changed`,
`provider_agent_unavailable`, `provider_locked`, `provider_timeout`,
`provider_output_invalid`, or `provider_command_failed`. A value that cannot be
written at its path, including a path whose rendered leaf is not a string,
blocks the resource with `provider_inject_failed`. Apply
skips a blocked resource like any other (`resource_skip`, outcome `partial`);
when the skips come from a provider, the outcome's error code is that
provider's code. A resource that consumes providers carries a `providers` list
in its plan projection, sorted by provider name, with `provider`, `item`,
`field` (or `null`), and `state`: `ok`, the fixed code of the failure, or
`not_fetched`. It never carries a value, a hash, or rbw's output. Resources
without provider fields keep an empty list.

**Limits.** The target file holds the secret by design; Luwu does not protect
it after the write beyond the declared mode. A write to a resource with a
provider field stages its bytes in an unnamed file (`O_TMPFILE`) in the target's
directory and names it through `/proc/self/fd`, so a kill during writing leaves
no entry; where the filesystem or a missing `/proc` does not support it the
write fails with `anonymous_staging_unsupported` and the target is unchanged. The unnamed file is given a name only for the moment before
it replaces the target, and a SIGKILL inside that window can leave a private
`.luwu-tmp-*` file with the secret; Luwu does not sweep it, so delete it by
hand. `rbw get --raw` reads the whole vault item into Luwu's memory. Luwu names
a provider's item in plan output because the manifest already does. Which rbw
behaviors the contract relies on without a recorded real run is a status fact;
see [status](status.md#provider-fields-awaiting-real-rbw-acceptance).

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
documents. Accept and adopt establish a baseline. A confirmed apply also advances the
baseline of every resource it wrote, and only for what it wrote: the declared
fields that it forwarded or that already converged for a structured template,
or the accepted digest and mode of a copy or whole-file template. Apply also repairs a converged
structured template, copy, or whole-file template whose baseline still records
an older value, which a reverse sync, an interrupted apply, or an application
that rewrote the file to match the source leaves behind: with no target to
write, the apply preview lists only a `baseline_write`. The plan itself does
not list that work: a structured template whose declared fields already match
reports `status = "converged"` with `action = "noop"`, and a copy whose bytes
match the source reports `in_sync`. Apply creates a baseline only for a copy or whole-file template whose absent
target it just created; it never creates one for a structured template, and it
never accepts a field that is not converged after the write. The preview lists each `baseline_write` action, so the
advance is part of what `--yes` authorizes. Planning and reverse sync never
change a baseline.

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
Reverse-sync requires an explicit profile, exactly one resource, and either
selected eligible live/merge fields or `--undeclared PATH`. Field mode writes only
the declared reverse_to destinations. Undeclared mode is described below. Adopt requires an explicit profile and one
or more copy or whole-file template resources (see Commands). When the source
bytes differ from the live target, the adopt preview marks its baseline action
with `replaces_live_on_apply = true`: the next apply will replace those live
bytes. Accept and reverse-sync are per-resource mutations with no joint
cross-resource selection, and an adopt batch only repeats the per-resource
adopt. Apply alone
mutates the complete selected profile as a batch. Every mutation has a
metadata-only preview and requires explicit --yes; confirmation
recalculates and rechecks the manifest, selected profile and package
expansion, source/input/baseline/target observations, directory snapshots,
path identities, and modes. At mutation entry, Luwu verifies the complete
caller-supplied plan projection against its issued evidence. It then verifies
the manifest, freshly observes the selected profile, verifies the manifest
again, and compares the newly derived complete plan projection with that same
evidence. Both checks must pass before a writer begins. A command that
confirms with `--yes` observes the profile once, as the entry observation: it
uses the plan it built a moment earlier in the same call, and that plan is
rebuilt and rechecked as above when it waits longer than a fraction of a second
or is used a second time. A change that lands between that observation and the
first write is then caught by the per-action and final whole-batch checks below
instead of before the first write. Execution uses values
derived from this fresh authorized state, never mutable derived data from the
caller-supplied plan. Mutating the original plan's derived objects afterward
does not change authorized writes and is not a cancellation API. Shared
declaration objects and actual file dependencies remain subject to the
finite-batch and per-action checks below.

### Writing undeclared changes back to a template source

`reverse-sync --resource NAME --undeclared PATH` is the only operation that
writes the source root. It copies a scalar change that the application made
outside the declared fields into the structured template's source, so the next
apply reproduces it. It applies only to a structured template and is separate
from `--field`; the two cannot be combined.

Each `PATH` is a JSON array of literal key names, for example
`'["ui", "font"]'`, and the option repeats to name several leaves. The
operator declares what is written at invocation. Luwu writes only the named
leaves, refuses a named leaf that does not currently differ, and leaves every
other undeclared difference unchanged. The plan cannot tell whether the
application or the source changed an undeclared value, so naming a path is the
operator's claim that the live value is the one to keep; writing it back
overwrites a different value in the source. Values and keys are not printed in
the preview, and Luwu does not inspect values for secrets, so the operator
checks the named leaf before confirming.

Luwu writes only when it can prove every one of these conditions, and
otherwise refuses with `undeclared_unsafe` and writes nothing:

- The source parses in its declared format and contains no Jinja block or
  comment syntax (`{%`, `{#`), and the patched source has exactly the same
  template tokens (expressions and strings inside them) as the original, so
  replacing text cannot reshape an expression.
- Every changed path, and each of its ancestors, is a literal mapping in the
  source and in the render, and neither the keys nor the values contain
  template markup (`{{`, `{%`, `{#`).
- The source value at that path equals the rendered value, so Jinja did not
  rewrite it.
- The changes are scalars: no array member, no multiline value, and no
  sensitive key name unless it is listed in `public_names`.
- Re-rendering the patched source in memory reproduces the live value at each
  named path, leaves every other undeclared difference as it was, and leaves
  every declared field's desired value unchanged.

The write uses the same span patch and atomic replacement as other writes,
rechecks the source, inputs, baseline, and target, and verifies after the
write that undeclared content no longer differs and declared fields are
unchanged. A resource that is blocked, in conflict, unbased, or not a
structured template is refused (`review_required` or `operation_invalid`).
Without `--yes`, the command prints a preview whose action is
`source_write` and returns exit code `2`.

### Finite-batch freshness and verification

A confirmed profile mutation fixes its finite batch from the verified
manifest, selected profile and package closure, explicit resource selection,
and directory expansion. Before the first write, Luwu completes full-batch
preflight across every selected resource and expanded directory leaf,
including no-op resources. A batch-level blocker, such as conflicting parent
modes or a stale manifest, includes all blockers found in that observation
and prevents every write from starting.

A resource that needs review does not stop the batch. Apply skips each
resource whose status is `blocked`, `conflict`, `unbased`, or
`adopt_required`, or whose undeclared content changed under
`undeclared = "block"`, and deploys the rest. Each skipped resource appears
as a `resource_skip` action with state `skipped` and a reason code (the
status, or `undeclared_changed`). When a skip accompanies at least one write the
outcome is `partial` with error code `review_required`, even if every write
succeeds. When nothing is written the outcome is `not_attempted`. Both exit
with code `2`. Skipped resources are excluded
from the per-action freshness checks and from the final whole-batch
verification; a resource that was not skipped at plan time but needs review at
write time still stops the batch with `review_required`.

Before staging or creating each action, Luwu revalidates the common batch
authority by rereading and revalidating the manifest bytes, selected
profile/package closure, all three root identities, and the expansion of the
source directories that produced the current action's resources. The expansion
of every other declared directory is verified in full before the first write
and again in the final whole-batch observation, so a change to an unrelated
directory is found at that directory's own action or at the final check. It
also rereads every file dependency of the current action and compares
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
its kind (`target_write`, `link_create`, `baseline_write`, `input_write`,
`source_write`, `resource_skip`, or a parent creation), resource, relative
path, state, and selected field labels. A skipped action also carries its
fixed `reason`. A copy or whole-file template resource whose plan action is
`adopt` or `review` (a conflict that adopting would settle) while the live bytes
differ from the source reports
`replaces_live_on_apply = true` in its plan projection, and the adopt preview
marks its `baseline_write` action the same way. Apply may end as
`committed_but_baseline_not_advanced` when the targets were written but a
baseline could not be advanced. The targets are not rolled back; the old
baseline is left in place, so the next plan classifies the resource against it, and an explicit
accept or adopt can move it.
CLI mutation commands return exit code 0 only for committed or no-change
outcomes; other outcomes return 2. Results and errors never expose values,
document bytes, diffs, content hashes, secret material, raw parser messages,
filesystem exception text, or exception payloads. Only allowlisted version 8
codes are projected; an unknown internal code is normalized to
`resource_blocked`. The version 8 error projection and the fixed meanings
defined in this contract are covered by the tests; the verified state is in
[status.md](status.md).

## Commands

Luwu has six commands: `inspect`, `plan`, `apply`, `accept`, `adopt`, and `reverse-sync`. Every command requires `--profile NAME` and accepts `--manifest PATH` (default `luwu.toml`) and `--json`. There is no provider, journal, cache, or recovery command. `inspect`, `plan`, and `apply` also accept `--provider-executable rbw=/absolute/path`, the only subprocess authority; see [Provider-owned fields](#provider-owned-fields).

`inspect` reports the current state. `plan` reports the same observation together with the action an explicit apply could take. Neither writes the manifest, source, target, input, or baseline. Both return exit code `0` after successfully calculating a plan, including when a resource is reported as `blocked`.

`apply` always calculates a plan first. Without `--yes` it is only a preview, writes nothing, and returns exit code `2`. With `--yes` it completes a full profile preflight, rechecks every dependency, and then writes. A blocked, review, conflict, unbased, or adopt-required resource is skipped and reported, the remaining resources are deployed, and the outcome is `partial` with exit code `2`; resolving a skipped resource is a separate explicit step. After the targets verify, `apply` advances the baselines of the resources it wrote, and the preview lists those writes. `apply` uses no execution record.

`accept` requires `--resource`, one or more `--field` values, and `--from desired|live`. `reverse-sync` requires `--resource` and either one or more `--field` values or one or more `--undeclared PATH` values, never both. `adopt` requires `--resource` naming a copy or whole-file template; repeating it adopts several from one observation, in the order given, and a name that is repeated or cannot be adopted refuses the whole batch before any write. The first failing resource stops the batch, earlier baselines stay committed, and the outcome is `partial`. Without `--yes`, a mutation command emits a metadata-only preview and returns exit code `2`. The preview is not a persisted plan token; `--yes` recalculates the current inputs and then performs the documented stale checks. A successful mutation returns exit code `0` and includes a fresh verification result. Results never contain accepted values, rendered content, diffs, or hashes.

Writes use a temporary file in the target's existing parent followed by an atomic replacement. Existing regular-file permissions are preserved unless a mode is declared. A target symlink is refused rather than followed or replaced; a symlink resource only creates its own link where nothing exists, and leaves an existing link alone. Luwu uses descriptor-relative no-follow operations, records identities, and takes a non-blocking advisory lock for cooperating Luwu writers on POSIX. It fails closed when those filesystem primitives are unavailable. An advisory lock does not control unrelated writers that ignore it.

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
