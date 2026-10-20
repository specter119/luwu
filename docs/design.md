# Luwu Current Implementation Design

Status: current implementation design

The public manifest, CLI, JSON, error, and write promises are owned by
[reference.md](reference.md). This document explains the current call graph,
module responsibilities, and implementation mechanisms. It must not become a
second public contract or a historical status log.

## End-to-end flow

```text
CLI input
  -> manifest and profile validation
  -> per-resource observation (source, target, baseline, inputs)
  -> in-memory plan with immutable evidence
  -> explicit confirmation
  -> fresh verify-observe-verify authorization
  -> staged atomic file replacement
  -> fresh whole-batch observation
```

The loader validates a declared manifest and expands the selected profile into
resource objects. The planner observes every resource in deterministic order
and returns metadata only. Mutation paths consume a current plan, revalidate
the inputs they rely on, perform bounded writes, and observe the result again.
A blocked or uncertain boundary remains visible instead of being converted into
a best-effort success.

## Shared primitives

Each primitive has one implementation, and every caller imports it. Two
single-call exceptions are recorded in the ceilings of
`tests/tooling/test_primitive_ownership.py`: the JSONC reader decodes one string
token with `json.loads`, and the manifest loader parses its own TOML with
`tomllib.loads`. The ceilings stop these from growing.

- `luwu/filesystem.py` is the only place that opens, reads, lists, stages,
  replaces, locks, or creates files and directories. Inputs, baselines, and
  targets all write through `write_file` and `create_parent`. Callers that
  walk a tree (the manifest loader and the engine) use the descriptor
  primitives `open_boundary`, `open_child_directory`, `open_child_file`,
  `read_stable_file`, `list_directory`, and `verify_directory_binding`. They
  raise `FileNotFoundError`, `UnsafeEntryError`, `FileChangedError`, or
  `RootChangedError`, and each caller maps those to its own error code.
- `luwu/sensitivity.py` holds the only predicate that decides whether a public
  name looks secret-bearing. The manifest, input layers, and document formats
  all call it. An exact-name `public_names` set flows from the resource or
  input declaration into the predicate, so the default stays fail-closed.
- `luwu/semantic.py` holds strict JSON parsing (`parse_strict_json`), the
  public scalar boundary (`is_public_scalar`), and value equality. Baselines,
  input layers, and ownership use them.

A second copy of one of these is a defect, not a local convenience.
`tests/tooling/test_primitive_ownership.py` enforces the owner of each
primitive call. The maintainer workflow is in
[maintenance](maintenance.md#preventing-duplicate-implementations).

## Observation and semantic comparison

The desired side is rendered or read from the declared source before it is
compared with live state. The field classifier in `luwu/ownership.py` compares
only declared fields, preserves missing values as a private sentinel, and
returns metadata without values. Baselines are read through their declared
path and are never silently created or updated by observation. A baseline
recorded for a different declaration is treated as absent for classification,
while its file observation still guards the replacement, so an explicit
re-accept recovers without deleting files by hand.

## Module responsibilities

The manifest, input, format, baseline, filesystem, plan-evidence, and engine
modules carry the mechanism:

- `luwu/manifest.py` loads the version 8 declaration, validates roots and
  path relationships, resolves the explicitly selected profile, computes
  stable package dependency closure and exclusions, and expands declared
  directory resources. It infers a resource's kind from its source (a `.j2`
  suffix, a directory, or a plain file), applies `[defaults]`, and resolves the
  `deploy` choice into the internal kinds `template`, `copy`, and `symlink`; a
  whole-file template is a copy whose rendered bytes are the desired side. It
  retains input-layer and expansion provenance so a
  later confirmation can reload and compare the same selection. Excluded
  resources and inputs used only by them are removed before payload metadata
  observation; declaration validation alone does not inspect their contents.
  Only source files are stat-checked at load time. Live targets, baselines, and
  input files are observed per resource by the engine, so one unsafe entry
  blocks that resource instead of aborting the profile.
- `luwu/inputs.py` resolves only the selected resource's declared input
  bindings, applies ordered layers, and keeps value provenance and file
  observations in memory for rendering and freshness checks. Reverse mutation
  must confirm that its destination layer is not shadowed by a higher layer
  after the write, and simulate the patch across every consumer in the selected
  profile before grouping writes by file.
- `luwu/formats.py` parses complete JSON, JSONC, and TOML documents while
  retaining their original bytes. Literal tuple paths identify scalar leaves
  across formats. The format layer owns duplicate/sensitive-key checks,
  semantic equality, selected-span edits, reparsing, and verification that
  unselected document regions retain their meaning and bytes. It masks only
  selected leaves and absent mapping/table ancestors required by those edits;
  existing unknown containers, arrays, and type changes stay observable.
- `luwu/baseline.py` validates schema-2 baselines against the declared
  roots, profile, resource paths, field mapping, input bindings, and directory
  expansion. Its closed payload variants persist selected public field values
  or an explicitly accepted copy digest as public content evidence, never the
  complete source or target document. The target copy payload records the
  digest algorithm and accepted digest, with mode evidence separately. Durable
  binding identity uses resolved root paths; root device/inode identity belongs
  to execution freshness, not baseline identity.
- `luwu/filesystem.py` also owns the symlink primitives `observe_link` and
  `create_link`. A link is created with `os.symlink` relative to the held
  parent descriptor, so `EEXIST` is the only create-if-absent check, and the
  content is read back with `os.readlink` before the result is reported. Link
  resources keep no baseline because the link target is fully determined by
  the declaration.
- `luwu/filesystem.py` provides no-follow regular-file observations,
  missing-parent inspection, one-directory-at-a-time parent creation, and
  staged file replacement; content and mode-only changes use that same staged
  file path. The selected parent-creation flow creates the final
  child name with exclusive `mkdir` at initial mode 0o700, then immediately
  opens it with no-follow directory flags and holds the descriptor. It applies
  the requested exact mode with `fchmod`, verifies descriptor and named-entry
  type, identity, and mode, syncs the child and parent, and rechecks the full
  ancestor chain. `FileExistsError` is handled at the `mkdir` call; errors
  after successful creation preserve committed/uncertain evidence. It returns
  an observed identity, not proof of creation provenance. This path does not
  use staged-directory publication, `ctypes`, or `renameat2`. The public
  mode, identity, outcome, and actor guarantees are in the [version 8 resource
  contract](reference.md#resources-and-declared-scope); current verification
  evidence is summarized in [status](status.md).
- `luwu/plan_evidence.py` captures the engine's one complete plan projection
  into detached immutable evidence records and compares later projections
  against every recorded edge and scalar. It preserves exact scalar types and
  Decimal representation, rejects unsupported and cyclic graphs, and keeps
  no mutable plan references or persistent identity cache. Equivalent graphs
  may split or merge shared subnodes; topology alone is not authorization.
  Changed values, types, Decimal representation, document spans, or bytes do
  not match. The public authorization behavior is owned by the
  [version 8 contract](reference.md#manifest-and-profile-contract).
- `luwu/engine.py` composes read-only plan, explicit baseline accept,
  copy and whole-file template adopt, selected reverse sync, and profile-wide apply. It
  uses a finite-batch flow: complete preflight, shared and current-action
  complete-byte freshness checks before staging and again in the publication
  callback, then a fresh whole-batch observation. Reverse sync rechecks all
  shared consumers; per-action results retain earlier commits if a later
  resource or parent creation fails. Apply decides per resource: a resource
  that needs review is reported as a skipped action and the rest are written,
  so the outcome is `partial`. After a resource's target write verifies, apply
  writes that resource's baseline through the same staged-write path, from the
  freshly observed live values, and a failure there is its own outcome
  because the target write is already committed. A converged template, or a
  copy whose live bytes already equal the source, with a stale baseline gets a
  baseline-only write, so an interrupted apply or a reverse sync repairs itself
  on the next apply. `reverse_undeclared` is the
  one path that writes the source root: it patches only the leaves the
  operator named in a literal template, checks that the template tokens are
  unchanged, renders the result in memory, and writes only when the render
  reproduces the live value at those leaves and leaves the declared fields
  and the other undeclared differences as they were. Structured
  comparison of undeclared leaves is in `luwu/formats.py`. This flow creates no simultaneous
  snapshot, generic transaction, replay layer, or durable execution journal. The
  [version 8 finite-batch contract](reference.md#finite-batch-freshness-and-verification)
  owns the exact checks and outcomes. At mutation entry, the caller plan is
  checked against its captured evidence; a fresh verify-observe-verify cycle
  then rederives the selected profile and must match the same evidence before
  execution. Freshly derived resources, parsed documents, inputs, observations,
  baselines, ownership, and write bytes drive the authorized operation. The
  engine no longer retains caller-origin pointers or repeats the origin-graph
  check after authorization; shared declarations still pass the independent
  registry check at each action. Complete current-action dependencies are
  re-read before staging and in the publication callback, with whole-batch
  preflight, shared-consumer verification, expected own-write identities,
  final no-op observation, and honest partial outcomes retained. Expected
  filesystem facts advance only from identities returned by successful Luwu
  actions. Current acceptance evidence and remaining scope limits are tracked in
  [status](status.md).

Smaller modules keep their responsibilities narrow: `luwu/rendering.py` renders
only declared sources with a sandboxed Jinja environment, strict undefined
variables, no loader, and no external capability; `luwu/ownership.py` and
`luwu/semantic.py` are pure comparison and classification code that never
write; and `luwu/cli.py` translates arguments and projects approved metadata
into human-readable or machine-readable output.

The CLI integrates these capabilities behind explicit profile and mutation
selection. Directory enumeration is rechecked at the write boundary so a new
or rebound leaf cannot silently enter the authorized set. The release evidence
for the current implementation is summarized in [status](status.md); the
remaining closure constraint is tracked in the [roadmap](roadmap.md).
Application-shaped configuration uses the same declared-path and ownership
mechanism. The fixed-version targets and opaque live-owned Codex hook state
are defined in [reference.md](reference.md#manifest-and-profile-contract);
the implementation does not infer mappings or grant application trust.
Droid workspace-trust SDK calls do not provide a file-schema mechanism, so no
workspace-trust file mapping is implied.

## Rejected alternatives

These designs were evaluated against concrete counterexamples and rejected
because the accepted behavior did not need them. Reopening one needs a new
owner decision and evidence, not a convenience argument.

- A generic field-path language, storage framework, or merge engine for
  ownership. Declared literal paths and an explicit baseline were sufficient.
- A generic JSON editor or schema registry for reverse sync. Selected scalar
  spans are patched; unsupported or ambiguous spans are rejected before
  writing.
- A transaction engine, second ledger, durable execution journal, or automatic
  replay and rollback. A fresh plan is the recovery step.
- Treating equal content as proof of a Luwu replacement. Write results
  distinguish not replaced, replaced, and uncertain from re-observed identity.
- An AST or adapter framework for TOML. A bounded lexical patcher with
  byte-level negative controls covers the declared shapes.
- Requiring plan-graph alias topology to be equal for authorization. Splitting
  a shared subnode into equal copies, or merging equal copies, does not change
  what is written. Authorization compares every projected edge and value,
  exact scalar type, Decimal representation, parser span, and source bytes.
- Retaining caller-origin references and repeating an origin-graph check after
  authorization. The fresh verify-observe-verify cycle and per-action
  dependency checks already bound the write.
- Parallel writers as a performance optimization. Concurrency must not obscure
  ordering and partial-success boundaries.
- Hardlink and reflink deployment. Luwu refuses a target whose link count is
  not one, and a hardlink would let an application write silently change the
  source file; configuration files are small enough that a symlink or an
  atomic copy covers the need.
- Writing undeclared changes back through templates that use Jinja blocks or
  rewritten values. Only a literal subtree whose render is proven equal is
  written; everything else is refused instead of guessed.
- Writing every undeclared difference with one flag. The plan keeps no record
  of the undeclared state, so it cannot tell a live change from a source
  change, and a preview cannot list the keys. The operator names each leaf, so
  the write is declared at invocation. A baseline digest of the undeclared
  projection would remove the direction ambiguity and stays an option if
  trials show the claim is too easy to get wrong.
- Keeping retired manifest versions alive beside the current one. Three
  engines with separate copies of the filesystem, baseline, and format
  primitives let the copies drift; the older versions were retired instead.

The counterexample probe for the current boundaries is
`experiments/profile_ablation.py`.

## Deliberate limits

The current implementation does not provide automatic replay, rollback,
providers or secret handling, exact reviewed-plan tokens, or strong
consistency against writers that ignore Luwu's advisory locks. These limits
are current status and contract facts, not invitations to silently broaden
this design. Future changes need a new owner decision, evidence, and updated
reference/status records.
