# Luwu Public Reference

Status: current versioned contracts, including the complete version 8 public contract.

This document owns the manifest, CLI, JSON, error, and compatibility contracts for the developer-confidence preview and its versions 2 through 8: read-only observation, ownership observation, single-resource selective mutation, explicit execution, the provider extension, the additive version 7 pilot extension, and the additive version 8 extension. The [changelog](changelog.md) maps delivery names to versions. Product direction belongs in [product.md](product.md); implementation rationale belongs in [design.md](design.md); verified scope belongs in [status.md](status.md).

## Manifest

The manifest is a UTF-8 TOML file. Its root is the directory that scopes every declared source, target, and baseline. M1 uses manifest version `1`; M2 supports versions `1` and `2`; M3a adds version `3`; M3b adds version `4`; the M3c execution slice adds version `5`; M4 adds the independent provider execution version `6`; the M6 pilot adds the opt-in version `7` contract; M7 defines the additive version `8` contract below.

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

### Manifest version 2

Version `2` is the read-only observation contract. It accepts multiple declared resources, orders them by resource name, and rejects duplicate targets, source/target path conflicts, and ancestor-overlapping declared paths before planning. It adds the explicitly observed `kind = "copy"`, which reads a regular source file as literal bytes; copy resources are not template-rendered and are not applyable in M2. A version 2 resource supports the same ownership, scope, source, target, and public-variable rules as M1, plus an optional explicit `comparison`:

- `comparison = "exact-bytes"` (default): the conservative byte comparison used by M1;
- `comparison = "json"`: a strict UTF-8 JSON comparison for template resources only.

Version 2 does not expand ownership, field scope, baseline, reverse sync, or provider capabilities. It is an observation experiment: `inspect` and `plan` are supported, while `apply` is rejected with `m2_read_only` before any target write. Version 1 remains the only applyable manifest contract in M2.

### Manifest version 3

Version `3` is a read-only field ownership experiment. It accepts multiple explicitly declared template resources, requires `comparison = "json"`, `owner = "fields"`, `scope = "fields"`, and `content_sensitivity = "public"`, and rejects template variables and implicit resource kinds. Its `[resources.NAME.fields]` table maps non-empty literal top-level JSON keys to `source`, `live`, `merge`, or `ignore`; dots and slashes have no path syntax, and nested objects and arrays are compared as one complete field. A resource may name one root-relative `baseline` file, but Luwu never creates or updates it.

The baseline file is a strict JSON envelope with exactly `schema_version`, `resource`, `source`, `target`, `owners`, and `values`. `schema_version` is numeric `1`; `owners` must exactly match all declared fields, including ignored fields; `values` is an object containing only non-ignored declared fields. An absent value means that field was absent from the accepted comparison input, while a JSON `null` is a value. Baselines are read through the declared path with no-follow descriptor operations; symlinks, non-regular files, invalid envelopes, identity mismatches, and missing files block that resource. Desired and live JSON documents must both be objects, and live must already exist.

For a supplied baseline, each field is classified as `unchanged`, `converged`, `source_changed`, `live_changed`, `conflict`, or `ignored`. Ownership determines whether a one-sided change is a `forward_candidate` or `reverse_candidate`; unsupported directions and two-sided changes remain `review`. Without a baseline, non-ignored fields are `unbased` and produce no candidate. Undeclared desired/live changes are reported by a boolean signal without exposing their names or values. Candidates are observations only. Version 3 `apply` is rejected with `m3_read_only` before any write, and M3a does not accept, reverse-sync, persist, or roll back configuration.

### Manifest version 4

Version `4` preserves the v3 field contract while admitting two explicit, single-resource public mutations. It still requires an explicit template kind, strict JSON comparison, field scope, and `content_sensitivity = "public"`. The optional `reverse_sync` table must use `format = "literal-json"` and identity-map declared live/merge fields to their same-named top-level literal keys in the source JSON; alias mappings are rejected in this frozen slice. Dynamic Jinja expressions, nested path syntax, ignored/source-owned mappings, and provider or secret inputs are rejected.

`accept` writes only explicitly selected declared fields to the baseline envelope, from either the current desired or live object. `reverse-sync` writes only selected `live_changed`/`reverse_candidate` fields through their explicit source mapping. For literal JSON, an existing selected member is replaced in place; a selected member added or deleted by the live object changes only the required local separator. Preview patch metadata reports each selected key's `replace`, `add`, or `delete` operation and whether a separator was adjusted, without values, source text, or diffs. All unselected and undeclared source bytes remain unchanged; an input whose decoded top-level member spans cannot be proven safe is rejected before writing. Neither command copies a complete live file into a Jinja template, updates the baseline implicitly, or accepts undeclared content. Version 4 `apply` remains read-only; version 5 is the separate current multi-resource execution capability.

Reverse-sync rejects the resource with `review_required` when any declared field requires review, even if that field is not selected. This includes a two-sided conflict or a change against its declared ownership direction. The check applies to both preview and confirmation before building a patch. A compatible forward candidate on an unselected field does not block a selected reverse candidate. Explicit `accept` remains a separate choice of desired/live baseline values and can be used while the observation contains conflicts.

Reverse-sync authorization remains bound to the baseline actually used for field classification. Confirmed execution rechecks that baseline and the manifest under the source writer lock, immediately before replacement, and after replacement alongside the existing source/live checks. A changed or unavailable authorization input blocks before replacement with `stale_plan`; a change detected after replacement preserves the committed/unknown or committed-but-verification-failed outcome. Internal baseline stale-check evidence is never emitted or persisted.

### Manifest version 5 execution

Version `5` is an independent execution capability, not an upgrade of versions `1`–`4`. It accepts multiple resources only when each resource explicitly declares `kind = "template"` or `kind = "symbolic"`, `owner = "source"`, `scope = "whole-file"`, and `content_sensitivity = "public"`. It rejects comparison, fields, baseline, reverse-sync, provider, and secret fields. Resources are ordered by name, and duplicate or overlapping source/target paths are rejected before planning.

Version 5 `apply` uses a durable metadata-only journal. Without `--yes`, it is a pure preview and does not create the journal. With `--yes`, it requires an explicit `--record PATH`, completes full preflight for every resource before the first writer, and processes resources in stable order. Failure stops at the first failing resource; rollback is never attempted. Each resource is recorded as `unchanged`, `committed`, `unknown`, or `not-attempted`. A committed write whose durability or cleanup cannot be confirmed is reported as `recovery_required` or `unknown`, never as a clean success.

The journal and its persistent sibling lock file `.<journal-name>.luwu-lock` must not overlap the manifest or any declared source/target, including resolved aliases and ancestor relationships. A conflict returns `record_path_conflict` before creating either file or changing any target. The lock coordinates journal updates and is retained after execution. A writer that raises at the `os.replace` boundary is classified from no-follow staged-entry identity, not from equal target bytes: a confirmed staged identity means the target is known changed but durability may be unknown; an identity mismatch or unobservable boundary is `unknown` and requires recovery, without adding the target to `changed_targets`.
During a target write, a rebinding of the locked parent or a changed temporary entry is reported by the writer as `concurrent_change`; the executor preserves the failure boundary and does not treat it as a committed target. This code is distinct from a stale plan detected before the writer and from an uncertain replacement detected at `os.replace`.

The journal has a closed schema and stores only execution/mutation contracts, manifest identity, relative resource/path metadata, non-content file conditions, and state transitions. It does not store configuration values, bytes, rendered output, diffs, patches, provider payloads, secrets, or source/target content hashes. `record-inspect` (also accepted as `inspect-record`) accepts only the version-5 execution contract, reads that journal without replaying inputs or writing files, and rejects legacy records. `recover --record PATH` (also accepted as `record-reobserve`) re-observes the recorded manifest and paths without writing; it returns `confirmed` only when the recorded boundary is still observed and otherwise returns `recovery_required`. There is no automatic replay, rollback, or recovery mutation command in this slice.

For a recorded `committed` or `unchanged` resource, recovery confirmation requires both matching non-content path conditions and a freshly calculated `in_sync` observation. An `unknown` resource meeting those checks is only `matches_postcondition`, and still requires recovery; `not-attempted` remains unattempted. Path-level `matches_postcondition` reports only the non-content metadata comparison; the resource result also accounts for current drift. Confirmation is evidence of current convergence and matching metadata, not proof that historical bytes are unchanged: coordinated source/live edits that preserve all recorded metadata cannot be distinguished without persisted content evidence. Recovery remains an observation, with no guarantee against unrelated concurrent writers.

The overall recovery outcome is `confirmed` only when every resource is confirmed. A journal containing only `not-attempted` resources still returns `recovery_required`, including when their current configurations happen to be in sync.

### Manifest version 6 provider execution

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

## Pilot extension (manifest version 7)

This section defines the bounded public contract implemented for the Pi/Codex
public-configuration pilot. It does not change the meaning or capability of
manifest or JSON schema versions `1` through `6`. The pilot remains a separate
version `7` contract and is not a general profile or package system.

### Scope and roots

The initial pilot examples use the following declared public fields:

- Pi: `defaultModel`, `defaultProvider`, and `lastChangelogVersion` in
  `.pi/agent/settings.json`.
- Codex: `model` and `model_provider` in `.codex/config.toml`.

The manifest field table is the allowlist: only names declared there can be
observed or changed. The current implementation treats these as top-level
JSON or bounded TOML scalar fields and does not infer application-specific
schemas. A configuration value selecting a provider is application data and does
not invoke Luwu's provider capability. Project trust and hook trust are
outside this pilot.

A version `7` manifest has exactly three root paths under `[roots]`:
`source`, `inputs`, and `target`. Source templates are relative to the source
root, public inputs and baselines are relative to the inputs root, and live
targets are relative to the target root; the target root may also be an
explicit absolute path for an isolated HOME. The roots must already exist and
be regular directories without symlinked components. `source` and `inputs`
must be distinct and non-overlapping. The target root may contain those two
roots so that an isolated target can be a normal HOME directory, but it must
not be equal to or below either of them. Each declared target file must still
remain outside the source and input roots. Every target is named by a resource
declaration; the extension does not discover resources by walking a root.

Each resource may map a declared field to exactly one input file below
`global/` or `local/`. The file must contain that field as a public scalar;
the value is exposed to templates through `global_inputs` or `local_inputs`
and retains its provenance in memory. There is no implicit overlay or
inheritance. Reverse sync updates only the explicitly mapped input file and
recalculates provenance after the write. Secret-shaped field names, non-scalar
values, missing fields, unsupported encodings, and unsafe input paths block
the operation.

The concrete version `7` manifest shape is:

```toml
version = 7

[roots]
source = "repo"
inputs = "inputs"
target = "home"

[resources.pi]
kind = "template"
source = "pi.json.j2"
target = ".pi/agent/settings.json"
owner = "fields"
scope = "fields"
comparison = "json" # or "toml"
content_sensitivity = "public"
baseline = "baselines/pi.json"
inputs = { defaultModel = "global/pi.json", defaultProvider = "local/pi.json", lastChangelogVersion = "local/pi.json" }

[resources.pi.fields]
defaultModel = "source"
defaultProvider = "live"
lastChangelogVersion = "source"
```

Version `7` resources require an explicit template kind, relative source and
target names, `owner = "fields"`, `scope = "fields"`, public content
sensitivity, a non-empty field table, and `comparison = "json"` or `"toml"`.
The optional baseline is relative to the inputs root. The optional `inputs`
table maps field names to the input files described above.

### Field and format behavior

The pilot uses the existing declared-field ownership and comparison model.
Forward changes may update only declared source-owned fields in the live
target. Reverse changes may update only selected, declared live-owned input
fields. Unselected fields and bytes outside the selected patch remain
unchanged. `merge` and `ignore` retain their existing meanings; no whole-file
replacement may be used to implement a field-level change. An absent target
may be created only after a separate create preview and confirmation. The
create plan must contain no undeclared rendered keys and its baseline must
cover every non-ignored declared field; accepting a baseline does not by
itself authorize creation. Changes to an existing target are field-level
patches.

JSON uses the existing strict UTF-8 JSON comparison rules. A missing member is
distinct from a member whose value is `null`; deleting a selected member means
it becomes absent. Duplicate keys, comments, trailing commas, invalid UTF-8,
non-finite numbers, and other unsupported JSON syntax block mutation. A patch
must preserve all unselected and undeclared source bytes; if a safe patch cannot
be proven, the operation stops before writing.

A source-owned field removed from the rendered desired object may be deleted
from an existing target. Reverse deletion of a mapped input field is rejected
with `m6_reverse_delete`, including during preview: removing that required
input would make the next render undefined. A JSON null remains a present
value and may be reverse-synchronized when the declared template preserves
its type. Successful reverse verification requires both the mapped input and
the recalculated desired field to match the live value with strict semantics.

TOML input is UTF-8 and limited to a top-level table of bare-key, single-line
scalar assignments in the existing public scalar subset. M7a accepts inline
comments after a scalar; `#` inside a quoted string remains part of the value.
Updating a selected value replaces only its token and preserves assignment
spacing, whitespace before the comment, the comment, line endings, full-line
comments, and all unselected bytes. Deleting a selected field removes its
complete physical assignment line, including a same-line comment and line
terminator, where that deletion is otherwise allowed. Reverse deletion of a
mapped input field remains rejected. M7a keeps manifest version `7` and the
JSON output schema unchanged. Tables, arrays, quoted or dotted keys, multiline
strings, duplicate fields, unsupported scalar values, and ambiguous syntax
remain rejected. TOML absence is distinct from a present value; TOML has no
`null` value.

Any newly observed undeclared live key or undeclared live-side change in a
pilot target is an actionable review stop before writing that target. The
write does not adopt, delete, or silently accept that content. The result
identifies the affected resource and the need to review its declared scope
without exposing configuration values.

### Baselines and mutation consent

An absent baseline leaves declared fields unbased and produces no forward or
reverse candidate. Establishing or advancing a baseline requires a separate
explicitly confirmed acceptance of selected declared fields from the desired
or live side. Preview, input patching, target deployment, and successful
verification never create or advance a baseline implicitly. The existing
baseline field and acceptance semantics remain authoritative where they
apply.

Each state-changing step requires its own read-only preview and explicit
confirmation: baseline acceptance, reverse input patching, and forward target
deployment are separate actions. Library forward apply and reverse sync
revalidate the supplied in-memory plan. Library baseline acceptance takes a
manifest and recalculates its plan in each call, including a confirmed call;
it does not bind that call to an earlier preview. Selected fields and the
baseline side are explicit arguments to the confirming call. The CLI likewise
has no persisted cross-process preview token: `--yes` confirms a freshly
calculated plan. Within a mutation, the manifest, complete resource
declaration, templates, global/local inputs, baseline, roots, allowlist,
target, and relevant parent boundary are rechecked before writing. Selected
fields must remain eligible under that observation. A stale result requires
a new preview and confirmation.

After each confirmed write, Luwu re-reads the affected state and recalculates
the field observation. Results distinguish a write that was not attempted, a
write known not to have replaced its target, a verified replacement, a
replacement followed by failed verification, and an uncertain replacement or
durability state. Existing v5 execution outcome meanings apply where the same
write boundary is involved. No uncertain or failed verification is reported as
a clean success.

Recovery is read-only re-observation of the relevant inputs, baseline, roots,
and targets. It does not replay a write, roll back a prior write, or advance a
baseline. An uncertain result remains unresolved until a fresh observation and
explicit user decision establish the next action.

Version 7 has no durable execution journal. After process termination, a fresh
inspection observes current state; it cannot reconstruct which input writes
completed before the process stopped. Multi-file reverse sync is not a
transaction. Public hardlinked files are replaced at the declared pathname;
other links retain their previous inode and content. Version 7 does not make
the version 6 secret-target single-link guarantee.

Pilot previews, errors, records, and machine-readable results are
metadata-only: they may identify allowlisted resources, declared public field
labels, provenance labels, safe states, and actionable reasons. They do not
contain configuration values, rendered/source/target bytes, diffs, content
hashes, secrets, raw parser excerpts, or exception payloads. Declared public
input, target, and baseline files may persist the public values required by
their contracts. Version 8 copy adoption is an explicit public-content
admission: its baseline may persist the accepted byte digest as content
evidence, but the digest never appears in a preview, output, repr, or log.
Declaring content public does not redact or prove the absence of secrets; a
complete public-only admission check is required before a file or baseline is
staged.

### Dotter handoff

A target must be explicitly handed off to Luwu one target at a time before
Luwu manages it. Dotter and Luwu must not both manage the same target. The
handoff and any return to Dotter are operator actions; the pilot does not edit
Dotter configuration, remove or create links, or adopt targets implicitly.
The repeatable handoff and return procedure belongs in
[maintenance.md](maintenance.md).

## Full dotfiles coverage (manifest version 8)

This additive contract preserves versions 1 through 7, including the
inline-comment extension of version 7. It defines the complete authorized
scope as one contract, not feedback-gated slices. Current implementation
status is owned by [status.md](status.md) and open constraints by the
[roadmap](roadmap.md).

### Manifest, profiles, and package closure

A version 8 manifest has version = 8 and declares [roots] with source, inputs,
and target paths; named [profiles.NAME]; named [packages.NAME]; named inputs;
and named resources. Source and inputs are relative to the manifest directory;
target may be relative to it or an explicit absolute path. All three roots must
already exist as safe directories.
Source and inputs roots must be distinct and non-overlapping. The target root
may be separate or may contain either authority root, but cannot alias or lie
inside either. The loader rejects unsafe aliases and conflicting declared
manifest, source, input, target, baseline, and directory paths, including
ancestor conflicts across resources. Each directory leaf remains within its
declared directory scope. No undeclared path gains authority from a profile or
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
and baseline paths are invalid. A reverse destination must be the winning
layer for its binding after the selected write; reverse sync cannot write a
shadowed layer and rely on another layer to keep taking precedence.

A binding may declare one reverse_to destination as a non-optional input
layer. Reverse sync can write only that declared file and literal path for a
field that names the binding. The destination's filesystem parent must exist.
Luwu may create the selected declared destination value or file as required by
the supported format patch; it does not create arbitrary input directories or
write other keys. Before writing, reverse sync simulates the change across all
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
`m7_reverse_consumer_change` with the message `M7 reverse sync would change another declared consumer`.

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
created under that resource. This policy does not create reverse-input
destination parents; those filesystem parents must already exist. parent_mode
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
identity when available. The [design](design.md#version-8-full-coverage-mechanism)
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
existing `m7_review_required` error code. Its fixed `reason` is `undeclared content changed; inspect declared scope or correct it manually, then re-plan`.
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

Version 8 baselines use schema version 2 with closed structured-field and
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
requires explicit reacceptance; there is no baseline migration. An accepted
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
`m7_resource_blocked`. The version 8 error projection and the fixed meanings
defined in this contract are covered by the tests; the verified state is in
[status.md](status.md).

## Commands

All manifest commands accept `--manifest PATH` (default `luwu.toml`) and `--json`. Version-6 manifest commands additionally accept `--allow-subprocess` and `--rbw-executable ABSOLUTE_PATH`; both are required to run the provider. `inspect` and `plan` never write the manifest, source, target, or any state file. `record-inspect --record PATH` and `recover --record PATH` read only the named execution journal; neither requires a manifest argument or writes state. `cache-inspect`, `cache-refresh`, and `platform-check` are separate diagnostic commands; only `cache-refresh` writes, and it writes a metadata-only owner-only cache.

`inspect` reports the current state. `plan` reports the same observation together with the action an explicit apply could take. Both commands return exit code `0` after successfully calculating a plan, including when a resource is reported as `blocked`.

`apply` always calculates a plan first. In human output, a confirmed version 1 apply prints that plan before writing. Without `--yes`, it is only a preview, writes nothing, and returns exit code `2`. Version 1 with `--yes` rechecks every target against the calculated state, writes only `create` or `replace` actions, and recalculates the current plan after writing. Versions 5 and 6 use their independent execution journal contracts and require `--record PATH` for a confirmed apply. Version 7 uses no execution record: it writes only a declared source-owned field patch or an absent target created from the rendered declared content, then recalculates the field plan. Version 8 requires `--profile NAME`, performs complete profile preflight, and uses no execution record. A version 7 no-op returns `no_changes`; a blocked, stale, unsupported, or uncertain result returns exit code `2`. Version 6 additionally requires explicit subprocess authority and writes only the external secret target after owner-only preflight. JSON mode keeps stdout as one result document and excludes values, bytes, diffs, and hashes. A blocked, stale, or version 2/3/4 read-only plan returns exit code `2` and does not begin a write. A version 2 `apply` preview reports `m2_read_only`; a version 3 or 4 preview reports `m3_read_only`; a plan containing an unsafe resource reports `plan_blocked`.

`accept` and `reverse-sync` require `--resource`, one or more `--field` values, and explicit `--yes` confirmation for mutation. For version 7, `accept` establishes or advances selected baseline fields from the desired or live object, while `reverse-sync` patches the selected live-owned input destinations. Version 8 also requires `--profile NAME`; `accept` selects the baseline side and explicit fields, `reverse-sync` selects eligible fields and their declared input bindings, and `adopt` selects whole-file copy resources. Without `--yes`, a mutation command returns exit code `2` and emits a metadata-only preview. The preview is explanatory and is not a persisted plan token; `--yes` recalculates the current inputs and then performs the documented stale checks. Successful mutation returns exit code `0` and includes a fresh verification plan. The result never contains accepted values, rendered content, diffs, or hashes.

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

The manifest `version` and JSON `schema_version` are independent explicit contracts. M1 uses version `1` for both; M2 uses version `2`; M3a uses version `3`; M3b uses manifest and mutation result version `4`; M3c execution and re-observation use version `5`; M4 provider execution and re-observation use version `6`; the M6 pilot uses version `7` for manifests and pilot mutation results; M7 uses version `8` for manifests and metadata results. A future incompatible change must introduce a new version or a deliberate migration. There is no migration or automatic recovery mutation command.

The M6 pilot and M7 full-coverage contract are additive, opt-in extensions
alongside those contracts. Version `7` is selected only by `version = 7`; its
commands use the existing CLI command names and its metadata results use
`schema_version = 7`. Version `8` is selected only by `version = 8`, requires
an explicit profile, and uses metadata results with `schema_version = 8`.
Neither version broadens or reinterprets earlier contracts. There is no
migration from an earlier manifest and no automatic conversion of an existing
Dotter or v1-v6 configuration.
